#!/usr/bin/env python3
"""Prove three synthetic regressions against pinned pre-fix source snapshots.

Requires local Git history and the ordinary development Python dependencies.
Never fetches, checks out, installs packages, starts services, or runs models.
Each subprocess has a timeout; source archives are temporary and size limited.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
CASES = (
    {"id": "transcript-edit-restart", "fix": "f656f132548e2f1f281df485fb99d86b67d92998",
     "test": "test_crash_after_edit_before_projection_is_repaired_from_durable_revision",
     "file": "tests/test_transcript_search_freshness.py", "expected_tests": 1, "historical_failures": 1,
     "failure_marker": "orange unicycle"},
    {"id": "missing-text-chunk", "fix": "00922fcb3d6dad198b3d8fbbdffc2adf78287f8f",
     "test": "test_missing_middle_production_text_chunk_invalidates_exact_total",
     "file": "tests/test_exact_search_results.py", "expected_tests": 2, "historical_failures": 2,
     "failure_marker": "DID NOT RAISE"},
    {"id": "ask-routing", "fix": "b0cae56bac07b3e6c5ec47afa02397ee567f6ea4",
     "test": "test_router_benchmark_accuracy", "file": "tests/test_ask_router_benchmark.py",
     "expected_tests": 4, "historical_failures": 3, "failure_marker": "expected question, got exact",
     "failure_marker_by_test": {
         "test_router_benchmark_accuracy[every_source]": "expected every_source, got question"}},
)


def definitions(path: Path, names: set[str]) -> str:
    """Reuse current synthetic fixtures without importing unrelated test modules."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    found = {node.name for node in nodes}
    if found != names:
        raise ValueError(f"Missing regression definitions: {sorted(names - found)}")
    return "\n\n".join(ast.unparse(node) for node in nodes) + "\n"


def write_probe(root: Path, destination: Path, case: dict) -> Path:
    tests = destination / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "__init__.py").write_text("")
    source = root / case["file"]
    if case["id"] == "transcript-edit-restart":
        (tests / "fixture_write.py").write_bytes((root / "tests/fixture_write.py").read_bytes())
        header = ("import hashlib, io, wave\nimport pytest\n"
                  "from fastapi.testclient import TestClient\n"
                  "from tests.fixture_write import fixture_write\n"
                  "from case_intelligence import workbench as module\n"
                  "from case_intelligence.generation import UnavailableGenerator\n"
                  "from case_intelligence.pilot_uploads import PilotDocument\n"
                  "ACTOR = 'development-taylor-morgan'\n")
        body = definitions(source, {"SearchProjection", "projection", "app", "seed", case["test"]})
    elif case["id"] == "missing-text-chunk":
        header = ("import json\nimport pytest\n"
                  "from case_intelligence.exact_search_results import ExactSearchUnavailable, search_documents\n")
        body = definitions(source, {"scan", case["test"]})
        scanner = "from case_intelligence.malware_scan import MalwareScanResult\n"
        scanner += definitions(root / "tests/test_review_tools.py", {"CleanScanner"})
        (tests / "test_review_tools.py").write_text(scanner, encoding="utf-8")
    else:
        header, body = "", source.read_text(encoding="utf-8")
        (destination / "benchmarks").mkdir()
        benchmark = root / "benchmarks/ask-router-v1.json"
        (destination / "benchmarks/ask-router-v1.json").write_bytes(benchmark.read_bytes())
    probe = tests / "test_history_probe.py"
    probe.write_text(header + body, encoding="utf-8")
    return probe


def archive_source(root: Path, revision: str, destination: Path) -> str:
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=root,
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()
    archive = destination / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", resolved, "src"], cwd=root,
                       stdout=stream, stderr=subprocess.PIPE, check=True, timeout=30)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Historical source archive exceeds the 64 MiB limit")
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if any(not (m.isfile() or m.isdir()) or Path(m.name).is_absolute()
               or ".." in Path(m.name).parts for m in members):
            raise ValueError("Historical archive contains an unsafe member")
        if sum(m.size for m in members) > MAX_ARCHIVE_BYTES:
            raise ValueError("Historical expanded source exceeds the 64 MiB limit")
        bundle.extractall(destination, filter="data")
    archive.unlink()
    return resolved


def evaluate_report(report: Path, case: dict, returncode: int, historical: bool) -> dict:
    if not report.is_file():
        return {"accepted": False, "reason": "pytest did not produce a report"}
    suite = ET.parse(report).getroot()
    tests = list(suite.iter("testcase"))
    errors = list(suite.iter("error"))
    skips = list(suite.iter("skipped"))
    failures = list(suite.iter("failure"))
    # A marker in one failure cannot justify another failure in the same run.
    # The routing benchmark's question and every-source populations have
    # different known defects, so bind the latter marker to its exact test name.
    markers = {node: case.get("failure_marker_by_test", {}).get(test.get("name"), case["failure_marker"])
               for test in tests for node in test.findall("failure")}
    intended_failures = all(node in markers and markers[node] in
                            (node.get("message", "") + "\n" + (node.text or ""))
                            for node in failures)
    only_assertions = all(node.get("message", "").startswith(("AssertionError", "Failed: DID NOT RAISE"))
                          for node in failures)
    correct_tests = (len(tests) == case["expected_tests"] and
                     all(node.get("name", "").split("[")[0] == case["test"] for node in tests))
    accepted = correct_tests and not errors and not skips
    if historical:
        accepted = accepted and returncode == 1 and len(failures) == case["historical_failures"] and only_assertions
        accepted = accepted and intended_failures
    else:
        accepted = accepted and returncode == 0 and not failures
    return {"accepted": bool(accepted), "tests": len(tests), "failures": len(failures),
            "errors": len(errors), "skipped": len(skips), "returncode": returncode}


def run_probe(root: Path, case: dict, *, historical: bool, timeout: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="exculpata-corpus-history-") as temporary:
        destination = Path(temporary)
        revision = "working-tree"
        source = root / "src"
        if historical:
            revision = archive_source(root, case["fix"] + "^", destination)
            source = destination / "src"
        probe = write_probe(root, destination, case)
        report = destination / "results.xml"
        env = os.environ.copy()
        for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONHOME"):
            env.pop(key, None)
        env.update(PYTHONPATH=os.pathsep.join((str(source), str(destination))),
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", CASE_INTELLIGENCE_TEST_MODE="1",
                   CASE_INTELLIGENCE_STORAGE_RESERVE_GIB="0", PYTHONDONTWRITEBYTECODE="1")
        # Fail before pytest if an editable install shadows the selected snapshot.
        bootstrap = ("import pathlib,sys,case_intelligence,pytest; "
                     "assert pathlib.Path(case_intelligence.__file__).resolve().parent == "
                     "pathlib.Path(sys.argv[1]).resolve() / 'case_intelligence'; "
                     "raise SystemExit(pytest.main(sys.argv[2:]))")
        command = [sys.executable, "-c", bootstrap, str(source), "-q", "-o", "addopts=",
                   "--confcutdir=" + str(destination), "--junitxml=" + str(report),
                   str(probe) + "::" + case["test"]]
        try:
            process = subprocess.run(command, cwd=destination, env=env, capture_output=True,
                                     text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"case": case["id"], "revision": revision, "accepted": False, "reason": "timeout"}
        outcome = evaluate_report(report, case, process.returncode, historical)
        outcome.update(case=case["id"], revision=revision,
                       expectation="regression-fails" if historical else "passes")
        if not outcome["accepted"]:
            # Bounded diagnostics; probes contain only repository synthetic material.
            outcome["diagnostic"] = (process.stdout + process.stderr)[-6000:]
        return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-only", action="store_true", help="Run seven current assertions without Git history")
    parser.add_argument("--timeout", type=int, default=90, help="Per-probe seconds (1–300; default 90)")
    parser.add_argument("--output", type=Path, help="Write the synthetic JSON evidence report")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300")
    results = []
    for case in CASES:
        for historical in ((False,) if args.current_only else (False, True)):
            try:
                outcome = run_probe(ROOT, case, historical=historical, timeout=args.timeout)
            except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError) as exc:
                outcome = {"case": case["id"], "accepted": False, "reason": type(exc).__name__}
            results.append(outcome)
            print(json.dumps(outcome, sort_keys=True), flush=True)
    report = {"schema_version": 1, "synthetic": True, "passed": all(row["accepted"] for row in results),
              "results": results}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
