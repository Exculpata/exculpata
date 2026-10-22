"""CI scorecards compare product checkouts with one shared evaluator and suite."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "defense-eval-ci.py"
EVALUATOR = ROOT / "src" / "case_intelligence" / "defense_eval.py"


def test_workflow_runs_stub_cards_on_every_pr_and_retains_failures():
    workflow = yaml.safe_load((ROOT / ".github/workflows/quality-gates.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" in triggers and triggers["pull_request"] is None
    assert "pull_request_target" not in triggers
    jobs = workflow["jobs"]
    assert "defense-eval" in jobs, "Every PR, including docs-only, needs automatic scorecards"
    job = jobs["defense-eval"]
    assert not job.get("needs")
    assert job.get("if", "${{ always() }}") == "${{ always() }}"
    assert job["permissions"] == {"contents": "read"}
    assert job["services"] == jobs["postgres-integration"]["services"]
    assert job["env"] == jobs["postgres-integration"]["env"]
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
    assert setup["uses"] == "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "git rev-parse HEAD" in commands
    assert any(step.get("env", {}).get("EXPECTED_INTEGRATION_SHA") == "${{ github.sha }}" for step in steps)
    assert "python -m pip install --disable-pip-version-check -e '.[postgres]'" in commands
    assert "python scripts/defense-eval-ci.py" in commands
    assert '--output-dir "$RUNNER_TEMP/defense-eval-artifacts"' in commands
    assert '--work-root "$RUNNER_TEMP"' in commands
    artifact = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert artifact["uses"] == "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"
    assert artifact["if"] == "${{ always() }}"
    assert artifact["with"]["path"] == "${{ runner.temp }}/defense-eval-artifacts"
    assert artifact["with"]["if-no-files-found"] == "error"
    assert all("if" not in step for step in steps if step is not artifact)
    assert "secrets." not in json.dumps(job)
    assert "continue-on-error" not in json.dumps(job)
    assert "models]" not in commands and "[dev" not in commands


def product_checkout(root: Path) -> Path:
    package = root / "src" / "case_intelligence"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "import os\n"
        "assert not any(k.startswith(('CASE_INTELLIGENCE_GENERATOR_', "
        "'CASE_INTELLIGENCE_RETRIEVAL_')) for k in os.environ)\n"
    )
    (package / "generation.py").write_text(
        "import re\n"
        "_TRANSCRIPT_ATTRIBUTION = re.compile(r'(?!)')\n"
        "class EvidenceItem: pass\n"
        "class GenerationRejected(Exception): pass\n"
        "class GenerationUnavailable(Exception): pass\n"
        "class GroundedGenerationService: pass\n"
        "def generator_from_environment():\n"
        "    raise AssertionError('CI must not configure a generator')\n"
    )
    (package / "service_endpoints.py").write_text(
        "def open_service_request(*args, **kwargs):\n"
        "    raise AssertionError('CI must not contact a model service')\n"
    )
    return root


def invoke(*args: str | Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    assert SCRIPT.is_file(), "The automatic CI scorecard runner is missing"
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *map(str, args)],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_child_imports_baseline_product_not_candidate_editable_install(tmp_path):
    baseline = product_checkout(tmp_path / "main")
    git(baseline, "init")
    git(baseline, "add", ".")
    git(baseline, "commit", "-m", "Synthetic product checkout")
    output = tmp_path / "card"
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                       CASE_INTELLIGENCE_GENERATOR_BACKEND="must-not-run",
                       CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL="must-not-run")
    result = invoke("--product-root", baseline, "--evaluator", EVALUATOR,
                    "--retriever", "lexical", "--output-dir", output, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (baseline / "src/case_intelligence/defense_eval.py").exists()
    card = json.loads((output / "scorecard.json").read_text())
    receipt = json.loads((output / "provenance.json").read_text())
    assert card["model"]["mode"] == "stub"
    assert card["aggregates"]["errors"] == 0
    assert receipt["product_root"] == str(baseline)
    assert receipt["evaluator_sha256"] == hashlib.sha256(EVALUATOR.read_bytes()).hexdigest()
    assert receipt["fingerprint"] == card["fingerprint"]
    assert receipt["imports"]["case_intelligence"] == str(baseline / "src/case_intelligence/__init__.py")
    assert receipt["imports"]["case_intelligence.generation"] == str(baseline / "src/case_intelligence/generation.py")
    assert receipt["imports"]["case_intelligence.defense_eval"] == str(EVALUATOR)
    assert (output / "scorecard.md").is_file()


def git(root: Path, *args: str) -> str:
    # These commits exist only inside pytest's disposable synthetic repository.
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Synthetic CI", "-c",
         "user.email=synthetic@example.test", *args],
        capture_output=True, text=True, check=True, timeout=20,
    )
    return result.stdout.strip()


@pytest.fixture
def comparison_repo(tmp_path, monkeypatch):
    candidate = product_checkout(tmp_path / "candidate")
    monkeypatch.setenv("CASE_REVIEW_POSTGRES_TEST_DSN", "dbname=synthetic_admin")
    monkeypatch.setenv("CI_DATABASE_LOG", str(tmp_path / "database.log"))
    # Substitute only the external database connection; retain real SQL composition.
    (candidate / "src/case_intelligence/review_bench_v2.py").write_text('''
import json
import os
from pathlib import Path
import psycopg
from psycopg import sql

def record(event):
    with Path(os.environ["CI_DATABASE_LOG"]).open("a") as log:
        log.write(json.dumps(event) + "\\n")

class Database:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, statement):
        assert isinstance(statement, sql.Composed)
        command = statement.as_string()
        record({"sql": command})
        if command.startswith("DROP") and os.getenv("CI_DATABASE_DROP_FAIL"):
            raise psycopg.OperationalError("synthetic database cleanup failure")

def connect(dsn, *, autocommit, connect_timeout):
    assert autocommit is True
    return Database()

psycopg.connect = connect
''')
    git(candidate, "init", "-b", "main")
    retrieval = candidate / "src/case_intelligence/retrieval_probe.py"
    retrieval.write_text("def select(passages, query, top_k): return ()\n")
    git(candidate, "add", ".")
    git(candidate, "commit", "-m", "Synthetic baseline without evaluator")
    baseline_sha = git(candidate, "rev-parse", "HEAD")
    git(candidate, "update-ref", "refs/remotes/origin/main", baseline_sha)
    git(candidate, "checkout", "-b", "candidate")
    retrieval.write_text("def select(passages, query, top_k): return passages[:top_k]\n")
    evaluator = candidate / "src/case_intelligence/defense_eval.py"
    # Real evaluator/scorers/suite, with a database-free product retrieval stand-in.
    evaluator.write_text(EVALUATOR.read_text() + '''
class PostgresHybridRetriever:
    def __init__(self, suite):
        from .review_bench_v2 import record
        from psycopg.conninfo import conninfo_to_dict
        record({"database": conninfo_to_dict(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"])["dbname"]})
        self.record = {"name": "postgres-hybrid", "index": "postgresql-fts-pgvector",
                       "embedder": "DeterministicEmbeddingAdapter",
                       "reranker": "DeterministicReranker", "learned_thresholds": False}
    def __call__(self, passages, query, top_k):
        from .retrieval_probe import select
        return select(passages, query, top_k)
    def close(self): pass
''')
    suite_path = Path("benchmarks/defense-workflow/v2/suite.json")
    payload = json.loads((ROOT / suite_path).read_text())
    for relative in [suite_path, *(Path(row["path"]) for row in payload["passages"])]:
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    git(candidate, "add", ".")
    git(candidate, "commit", "-m", "Synthetic candidate with evaluator")
    return candidate, baseline_sha


def test_automatic_comparison_records_exact_revisions_and_keeps_scores_informational(comparison_repo, tmp_path):
    candidate, baseline_sha = comparison_repo
    candidate_sha = git(candidate, "rev-parse", "HEAD")
    refs = git(candidate, "show-ref")
    worktrees = git(candidate, "worktree", "list", "--porcelain")
    output, temporary = tmp_path / "artifacts", tmp_path / "owned-temp"
    summary = tmp_path / "summary.md"
    summary.write_text("Existing job summary\n")
    result = invoke("--candidate-root", candidate, "--output-dir", output,
                    "--work-root", temporary,
                    env=dict(os.environ, GITHUB_STEP_SUMMARY=str(summary)))
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((output / "provenance.json").read_text())
    assert receipt["main_sha"] == baseline_sha
    assert receipt["candidate_sha"] == candidate_sha
    assert receipt["evaluator_sha256"] == hashlib.sha256(
        (candidate / "src/case_intelligence/defense_eval.py").read_bytes()).hexdigest()
    cards = {name: json.loads((output / name / "scorecard.json").read_text())
             for name in ("candidate-lexical", "main-postgres", "candidate-postgres")}
    assert {card["fingerprint"] for card in cards.values()} == {receipt["fingerprint"]}
    assert cards["main-postgres"]["aggregates"]["answer_correct"] < cards["candidate-postgres"]["aggregates"]["answer_correct"]
    report = (output / "comparison.md").read_text()
    assert "False -> True" in report
    assert "shared candidate evaluator" in report
    assert "not real-model quality" in report
    assert baseline_sha in report and candidate_sha in report
    assert summary.read_text() == "Existing job summary\n" + report
    for name in cards:
        assert (output / name / "scorecard.md").is_file()
        assert (output / name / "provenance.json").is_file()
        assert (output / name / "run.log").is_file()
    assert git(candidate, "rev-parse", "HEAD") == candidate_sha
    assert git(candidate, "show-ref") == refs
    assert git(candidate, "worktree", "list", "--porcelain") == worktrees
    assert git(candidate, "status", "--porcelain") == ""
    assert not list(temporary.iterdir())


def test_score_regression_is_successful_measurement(comparison_repo, tmp_path):
    candidate, _ = comparison_repo
    git(candidate, "update-ref", "refs/remotes/origin/main", "HEAD")
    (candidate / "src/case_intelligence/retrieval_probe.py").write_text(
        "def select(passages, query, top_k): return ()\n")
    git(candidate, "add", ".")
    git(candidate, "commit", "-m", "Synthetic retrieval regression")
    output = tmp_path / "artifacts"
    result = invoke("--candidate-root", candidate, "--output-dir", output,
                    "--work-root", tmp_path / "owned-temp")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "True -> False" in (output / "comparison.md").read_text()


@pytest.mark.parametrize("failure", [
    "ReferenceStub.__call__ = lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic answer failure'))",
    "PostgresHybridRetriever.__call__ = lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic retrieval failure'))",
    "PostgresHybridRetriever.close = lambda *args: (_ for _ in ()).throw(DefenseEvalError('synthetic close failure'))",
])
def test_child_harness_errors_fail_instead_of_becoming_score_deltas(comparison_repo, tmp_path, failure):
    candidate, _ = comparison_repo
    evaluator = candidate / "src/case_intelligence/defense_eval.py"
    evaluator.write_text(evaluator.read_text() + "\n" + failure + "\n")
    output = tmp_path / "artifacts"
    before = git(candidate, "worktree", "list", "--porcelain")
    result = invoke("--candidate-root", candidate, "--output-dir", output,
                    "--work-root", tmp_path / "owned-temp")
    assert result.returncode == 2, result.stdout + result.stderr
    assert (output / "candidate-lexical/scorecard.json").is_file()
    assert (output / "harness-error.txt").is_file()
    assert not (output / "comparison.md").exists()
    assert git(candidate, "worktree", "list", "--porcelain") == before


def test_products_get_distinct_disposable_databases_and_pinned_imports(comparison_repo, tmp_path):
    candidate, baseline_sha = comparison_repo
    output = tmp_path / "artifacts"
    result = invoke("--candidate-root", candidate, "--output-dir", output,
                    "--work-root", tmp_path / "owned-temp")
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in (tmp_path / "database.log").read_text().splitlines()]
    databases = [event["database"] for event in events if "database" in event]
    assert len(set(databases)) == 2 and "synthetic_admin" not in databases
    assert [event.get("sql", "RUN").split()[0] for event in events] == [
        "CREATE", "RUN", "DROP", "CREATE", "RUN", "DROP",
    ]
    for index, label in enumerate(("main-postgres", "candidate-postgres")):
        receipt = json.loads((output / label / "provenance.json").read_text())
        assert receipt["database"] == databases[index]
        assert receipt["product_sha"] == (baseline_sha if index == 0 else git(candidate, "rev-parse", "HEAD"))
        for module in ("generation", "review_bench_v2"):
            path = Path(receipt["imports"][f"case_intelligence.{module}"])
            assert path == Path(receipt["product_root"]) / "src/case_intelligence" / f"{module}.py"
        assert events[index * 3]["sql"] == f'CREATE DATABASE "{databases[index]}"'
        assert events[index * 3 + 2]["sql"] == f'DROP DATABASE "{databases[index]}"'


def test_database_cleanup_failure_fails_job(comparison_repo, tmp_path, monkeypatch):
    candidate, _ = comparison_repo
    monkeypatch.setenv("CI_DATABASE_DROP_FAIL", "1")
    output = tmp_path / "artifacts"
    before = git(candidate, "worktree", "list", "--porcelain")
    result = invoke("--candidate-root", candidate, "--output-dir", output,
                    "--work-root", tmp_path / "owned-temp")
    assert result.returncode == 2, result.stdout + result.stderr
    assert (output / "main-postgres/scorecard.json").is_file()
    assert "synthetic database cleanup failure" in (output / "main-postgres/run.log").read_text()
    assert not (output / "candidate-postgres").exists()
    assert git(candidate, "worktree", "list", "--porcelain") == before


@pytest.mark.parametrize("damage", [
    "json_path.unlink()",
    "markdown_path.unlink()",
    "json_path.write_text('{broken')",
    "json_path.write_text('{}')",
    "card['fingerprint'] = 'wrong'",
    "card['questions'][0]['question_id'] = 'wrong'",
    "card['questions'][0]['question_id'] = card['questions'][1]['question_id']",
    "card['questions'][0]['type'] = 'wrong'",
    "card['questions'] = []",
    "card['aggregates']['errors'] = 1",
    "card['questions'][0]['error'] = 'captured-error'",
    "del card['questions'][0]['answer_correct']",
    "card['questions'][0]['answer_correct'] = 'true'",
    "card['aggregates']['answer_correct'] = -1",
    "card['retriever']['learned_thresholds'] = True",
    "card['model']['mode'] = 'configured'",
])
def test_invalid_child_outputs_fail_and_retain_partial_evidence(comparison_repo, tmp_path, damage):
    candidate, _ = comparison_repo
    evaluator = candidate / "src/case_intelligence/defense_eval.py"
    evaluator.write_text(evaluator.read_text() + '''
_original_write = write_scorecard
def write_scorecard(directory, scorecard, **kwargs):
    json_path, markdown_path = _original_write(directory, scorecard, **kwargs)
    if directory.name == "main-postgres":
        card = json.loads(json_path.read_text())
        DAMAGE
        if DAMAGE_IS_CARD:
            json_path.write_text(json.dumps(card))
    return json_path, markdown_path
'''.replace("DAMAGE_IS_CARD", str(damage.startswith(("card", "del card")))).replace("DAMAGE", damage))
    output = tmp_path / "artifacts"
    worktrees = git(candidate, "worktree", "list", "--porcelain")
    result = invoke("--candidate-root", candidate, "--output-dir", output,
                    "--work-root", tmp_path / "owned-temp")
    assert result.returncode == 2, result.stdout + result.stderr
    assert (output / "candidate-lexical/scorecard.json").is_file()
    assert (output / "main-postgres/run.log").is_file()
    assert (output / "provenance.json").is_file()
    assert (output / "harness-error.txt").is_file()
    assert not (output / "comparison.md").exists()
    assert git(candidate, "worktree", "list", "--porcelain") == worktrees


def test_absent_product_cannot_fall_back_to_installed_candidate(tmp_path):
    output = tmp_path / "card"
    result = invoke("--product-root", tmp_path / "missing", "--evaluator", EVALUATOR,
                    "--retriever", "lexical", "--output-dir", output)
    assert result.returncode != 0
    assert not (output / "scorecard.json").exists(), "Candidate product ran as the baseline"


@pytest.mark.parametrize("damage", ["path.unlink()", "receipt['fingerprint'] = 'wrong'",
                                      "receipt['evaluator_sha256'] = 'wrong'"])
def test_child_provenance_is_required(comparison_repo, tmp_path, damage):
    candidate, _ = comparison_repo
    evaluator = candidate / "src/case_intelligence/defense_eval.py"
    evaluator.write_text(evaluator.read_text() + '''
import atexit
def damage_receipt():
    if "--product-root" not in sys.argv:
        return
    directory = Path(sys.argv[sys.argv.index("--output-dir") + 1])
    if directory.name == "main-postgres":
        path = directory / "provenance.json"
        receipt = json.loads(path.read_text())
        DAMAGE
        if WRITES_RECEIPT:
            path.write_text(json.dumps(receipt))
atexit.register(damage_receipt)
'''.replace("WRITES_RECEIPT", str(damage.startswith("receipt"))).replace("DAMAGE", damage))
    result = invoke("--candidate-root", candidate, "--output-dir", tmp_path / "artifacts",
                    "--work-root", tmp_path / "owned-temp")
    assert result.returncode == 2, result.stdout + result.stderr
