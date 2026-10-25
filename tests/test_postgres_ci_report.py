from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-postgres-test-report.py"
spec = importlib.util.spec_from_file_location("postgres_ci_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def write_report(tmp_path, count=7, outcome=""):
    path = tmp_path / "results.xml"
    # fixture-write: create a synthetic CI report for the report checker.
    path.write_text("<testsuites><testsuite>" + "".join(
        f'<testcase name="synthetic_{i}">{outcome if i == 0 else ""}</testcase>'
        for i in range(count)
    ) + "</testsuite></testsuites>")
    return path


@pytest.mark.parametrize("count", [7, 8])
def test_accepts_complete_passing_suite(tmp_path, count):
    assert report.check_report(write_report(tmp_path, count)) == count


@pytest.mark.parametrize("outcome", ["<skipped/>", "<failure/>", "<error/>"])
def test_rejects_nonpassing_case(tmp_path, outcome):
    with pytest.raises(ValueError, match="without skips"):
        report.check_report(write_report(tmp_path, outcome=outcome))


@pytest.mark.parametrize("count", [0, 6])
def test_rejects_empty_or_incomplete_suite(tmp_path, count):
    with pytest.raises(ValueError, match="at least seven"):
        report.check_report(write_report(tmp_path, count))


@pytest.mark.parametrize("content", [None, "not XML"])
def test_cli_fails_for_missing_or_malformed_report(tmp_path, content):
    path = tmp_path / "results.xml"
    if content is not None:
        # fixture-write: create a deliberately malformed synthetic CI report.
        path.write_text(content)
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "acceptance blocked" in result.stderr


@pytest.mark.parametrize("entrypoint", ["Makefile", ".github/workflows/quality-gates.yml"])
def test_postgres_acceptance_includes_case_theory(entrypoint):
    content = (SCRIPT.parent.parent / entrypoint).read_text()
    if entrypoint == "Makefile":
        acceptance = content.split("\ntest-postgres:\n", 1)[1].split("\ndeployment-check:", 1)[0]
    else:
        import yaml

        steps = yaml.safe_load(content)["jobs"]["postgres-integration"]["steps"]
        acceptance = next(step["run"] for step in steps if "-m pytest" in step.get("run", ""))
    command = acceptance.replace("\\\n", " ").split("-m pytest", 1)[1].splitlines()[0]
    assert "tests/test_case_theory.py" in command.split()


@pytest.mark.parametrize("binary", ["pg_dump", "psql", "pg_restore"])
@pytest.mark.parametrize("prefix", [None, "", 'synthetic-client -i "synthetic container"'])
def test_postgres_client_command_prefix(monkeypatch, binary, prefix):
    from tests.test_case_theory import postgres_client_command

    if prefix is None:
        monkeypatch.delenv("CASE_REVIEW_POSTGRES_CLIENT_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CASE_REVIEW_POSTGRES_CLIENT_PREFIX", prefix)
    expected = ["synthetic-client", "-i", "synthetic container"] if prefix else []
    assert postgres_client_command(binary, "--dbname", "synthetic-dsn") == [
        *expected, binary, "--dbname", "synthetic-dsn",
    ]


def test_postgres_acceptance_uses_service_container_clients():
    import yaml

    workflow = SCRIPT.parent.parent / ".github/workflows/quality-gates.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["postgres-integration"]
    step = next(step for step in job["steps"] if "-m pytest" in step.get("run", ""))
    assert step.get("env", {}).get("CASE_REVIEW_POSTGRES_CLIENT_PREFIX") == (
        "docker exec -i ${{ job.services.postgres.id }}"
    )
    assert "CASE_REVIEW_POSTGRES_CLIENT_PREFIX" not in job["env"]
