#!/usr/bin/env python3
"""GPU-free CI scorecards using one evaluator against separate product checkouts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "case_intelligence"
EVAL_MODULE = ".".join((PACKAGE, "defense_eval"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def load_evaluator(product: Path, evaluator: Path):
    package_init = (product / "src" / PACKAGE / "__init__").with_suffix(".py")
    if not package_init.is_file():
        raise ValueError("Product checkout has no package; refusing installed fallback")
    sys.path.insert(0, str(product / "src"))
    importlib.import_module(PACKAGE)
    imported_product(product, evaluator)
    spec = importlib.util.spec_from_file_location(EVAL_MODULE, evaluator)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load the shared evaluator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[EVAL_MODULE] = module
    spec.loader.exec_module(module)
    imported_product(product, evaluator)
    return module


def imported_product(product: Path, evaluator: Path) -> dict[str, str]:
    imports = {}
    for name, module in tuple(sys.modules.items()):
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            path = Path(module.__file__).resolve()
            if name == EVAL_MODULE:
                if path != evaluator:
                    raise ValueError("The shared evaluator was not imported")
            elif not path.is_relative_to(product / "src" / PACKAGE):
                raise ValueError(f"Product import escaped its checkout: {name}")
            imports[name] = str(path)
    return imports


@contextmanager
def disposable_database():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    dsn = os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]
    name = "defense_eval_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield name, make_conninfo(dsn, dbname=name)
        finally:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def run_child(product: Path, evaluator: Path, retriever: str, output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    module = load_evaluator(product, evaluator)
    suite = module.load_suite()
    product_sha = git(product, "rev-parse", "HEAD")
    if retriever == "postgres":
        importlib.import_module(".".join((PACKAGE, "review_bench_v2")))
        imported_product(product, evaluator)
    database = None
    try:
        with disposable_database() if retriever == "postgres" else nullcontext() as database:
            if database:
                os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"] = database[1]
            result = module.main(["--mode", "stub", "--retriever", retriever,
                                  "--output-dir", str(output)])
    finally:
        write_json((output / "provenance").with_suffix(".json"), {
            "product_root": str(product),
            "product_sha": product_sha,
            "database": database[0] if database else None,
            "evaluator_sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
            "fingerprint": suite.fingerprint,
            "imports": imported_product(product, evaluator),
        })
    return result


def validate_card(module, suite, directory: Path, retriever: str) -> None:
    card = json.loads((directory / "scorecard").with_suffix(".json").read_text())
    if any(card[key] != getattr(suite, key) for key in ("suite_id", "suite_version", "fingerprint")):
        raise ValueError("Scorecard suite identity mismatch")
    rows = card["questions"]
    if [(row["question_id"], row["type"]) for row in rows] != [
        (question.question_id, question.type) for question in suite.questions
    ]:
        raise ValueError("Scorecard question IDs or types mismatch")
    if (card["model"]["mode"] != "stub" or card["llm_judge"]["enabled"] is not False
            or card["retriever"]["name"] != ("lexical" if retriever == "lexical" else "postgres-hybrid")
            or card["retriever"]["learned_thresholds"] is not False):
        raise ValueError("Scorecard must use stub answers and deterministic retrieval")
    for row in rows:
        if row["error"] != "" or any(type(row[key]) is not bool for key in (
            "answer_correct", "citation_support", "abstention_correct", "abstained", "expect_abstention",
        )):
            raise ValueError("Scorecard has an error or invalid score")
        if row["retrieval_hit"] is not None and type(row["retrieval_hit"]) is not bool:
            raise ValueError("Invalid retrieval score")
        if type(row["latency_ms"]) is not int or row["latency_ms"] < 0:
            raise ValueError("Invalid latency")
        if row["unsupported_citation_count"] != len(row["unsupported_citations"]):
            raise ValueError("Invalid citation count")
    expected = module._scorecard(suite, rows, card["model"], False, card["retriever"])
    if card["aggregates"] != expected["aggregates"] or card["aggregates"]["errors"] != 0:
        raise ValueError("Scorecard totals mismatch or evaluation errors")
    if (directory / "scorecard").with_suffix(".md").read_text() != module.render_markdown(card):
        raise ValueError("Markdown does not match the JSON scorecard")


def git(candidate: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(candidate), *args], check=True,
                          capture_output=True, text=True, timeout=60).stdout.strip()


def run_comparison(candidate: Path, output: Path, work_root: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    main_sha = git(candidate, "rev-parse", "--verify", "origin/main^{commit}")
    candidate_sha = git(candidate, "rev-parse", "HEAD")
    evaluator = (candidate / "src" / PACKAGE / "defense_eval").with_suffix(".py")
    module = load_evaluator(candidate, evaluator)
    suite = module.load_suite()
    provenance = {
        "main_sha": main_sha,
        "candidate_sha": candidate_sha,
        "evaluator_sha256": hashlib.sha256(evaluator.read_bytes()).hexdigest(),
        "fingerprint": suite.fingerprint,
    }
    write_json((output / "provenance").with_suffix(".json"), provenance)
    with tempfile.TemporaryDirectory(prefix="defense-eval-", dir=work_root) as temporary:
        baseline = Path(temporary) / "main"
        git(candidate, "worktree", "add", "--detach", str(baseline), main_sha)
        try:
            # Sequential children must finish database cleanup before proceeding.
            for label, product, retriever in (
                ("candidate-lexical", candidate, "lexical"),
                ("main-postgres", baseline, "postgres"),
                ("candidate-postgres", candidate, "postgres"),
            ):
                directory = output / label
                directory.mkdir()
                with (directory / "run").with_suffix(".log").open("w") as log:
                    subprocess.run(
                        [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                         "--product-root", str(product), "--evaluator", str(evaluator),
                         "--retriever", retriever, "--output-dir", str(directory)],
                        stdout=log, stderr=subprocess.STDOUT, check=True, timeout=300,
                    )
                validate_card(module, suite, directory, retriever)
                receipt = json.loads((directory / "provenance").with_suffix(".json").read_text())
                if (receipt["product_root"] != str(product)
                        or receipt["product_sha"] != (main_sha if product == baseline else candidate_sha)
                        or any(receipt[key] != provenance[key] for key in ("fingerprint", "evaluator_sha256"))):
                    raise ValueError("Child evaluator or product provenance mismatch")
        finally:
            git(candidate, "worktree", "remove", str(baseline))
    report = (
        "# Defense evaluation: main vs integration candidate\n\n"
        f"Main: `{main_sha}`. Candidate: `{candidate_sha}`.\n\n"
        f"Evaluator SHA-256: `{provenance['evaluator_sha256']}`.\n\n"
        "Both products use the shared candidate evaluator and candidate fixture bytes. "
        "This measures product retrieval with a reference stub, not real-model quality; "
        "it does not compare historical evaluator versions. Score deltas are informational.\n\n"
        + module.compare_scorecards(
            (output / "main-postgres" / "scorecard").with_suffix(".json"),
            (output / "candidate-postgres" / "scorecard").with_suffix(".json"),
        )
    )
    (output / "comparison").with_suffix(".md").write_text(report)
    if summary := os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as stream:
            stream.write(report)
    return 0


def main() -> int:
    sys.dont_write_bytecode = True
    for key in tuple(os.environ):
        if key.startswith(("CASE_INTELLIGENCE_GENERATOR_", "CASE_INTELLIGENCE_RETRIEVAL_")):
            del os.environ[key]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, default=ROOT)
    parser.add_argument("--work-root", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--product-root", type=Path)
    parser.add_argument("--evaluator", type=Path)
    parser.add_argument("--retriever", choices=("lexical", "postgres"), default="lexical")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.product_root:
            if args.evaluator is None:
                parser.error("--product-root requires --evaluator")
            return run_child(args.product_root.resolve(), args.evaluator.resolve(),
                             args.retriever, args.output_dir.resolve())
        return run_comparison(args.candidate_root.resolve(), args.output_dir.resolve(),
                              args.work_root.resolve())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        message = f"Defense evaluation failed: {exc}\n"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "harness-error").with_suffix(".txt").write_text(message)
        print(message, end="", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
