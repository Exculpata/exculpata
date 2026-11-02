"""Exercise CI summaries using hand-written synthetic pytest logs only."""

import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci_test_summary.py"
FIXTURES = ROOT / "tests/fixtures/ci_test_summary"
FALLBACK = "could not parse the test log"


def summarize(path, status="failure"):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), status],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    return result.stdout


def test_summary_source_has_no_outbound_inventory():
    source_inventory = runpy.run_path(str(ROOT / "scripts/network-contract.py"))["source_inventory"]
    assert not [entry for entry in source_inventory(ROOT) if entry[0] == "scripts/ci_test_summary.py"]


@pytest.mark.parametrize("fixture, status, expected", [
    ("timeout.txt", "failure",
     "### Application test summary\n\nCancelled or partial run.\n\n"
     "Last started test: `tests/test_synthetic_wait.py::test_wait`\n\n"
     "Timed out in:\n- `tests/test_synthetic_wait.py::test_wait`\n\n"),
    ("cancelled.txt", "failure",
     "### Application test summary\n\nCancelled or partial run.\n\n"
     "Last started test: `tests/test_synthetic_summary.py::test_pending`\n\n"),
    ("cancelled.txt", "cancelled",
     "### Application test summary\n\nCancelled or partial run.\n\n"
     "Last started test: `tests/test_synthetic_summary.py::test_pending`\n\n"),
])
def test_diagnostic_output_is_byte_identical(fixture, status, expected):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(FIXTURES / fixture), status],
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout == expected.encode("utf-8")


def test_failed_test_ids_from_short_summary():
    summary = summarize(FIXTURES / "failed.txt")
    assert "Failed tests" in summary
    assert "tests/test_synthetic_summary.py::test_alpha" in summary
    assert "tests/test_synthetic_summary.py::test_beta[small]" in summary
    assert "cancelled" not in summary.lower()
    assert "AssertionError" not in summary


def test_faulthandler_names_timed_out_test():
    summary = summarize(FIXTURES / "timeout.txt")
    assert "Timed out in" in summary
    assert "tests/test_synthetic_wait.py::test_wait" in summary
    assert "Thread 0x" not in summary


@pytest.mark.parametrize("status", ["failure", "cancelled"])
def test_partial_run_names_last_started_test(status):
    summary = summarize(FIXTURES / "cancelled.txt", status)
    assert "cancelled" in summary.lower()
    assert "Last started test: `tests/test_synthetic_summary.py::test_pending`" in summary
    assert "test_alpha" not in summary


@pytest.mark.parametrize("content", ["", "unrecognized synthetic output\n", b"\xff\xfe"])
def test_unparseable_log_is_nonfatal(tmp_path, content):
    log = tmp_path / "synthetic.txt"
    # fixture-write: Construct synthetic malformed log input for the CLI boundary.
    log.write_bytes(content.encode() if isinstance(content, str) else content)
    assert FALLBACK in summarize(log)


@pytest.mark.parametrize("status", ["failure", "cancelled"])
def test_missing_log_is_nonfatal(tmp_path, status):
    summary = summarize(tmp_path / "missing.txt", status)
    assert FALLBACK in summary
    if status == "cancelled":
        assert "cancelled" in summary.lower()


@pytest.mark.parametrize("status", ["failure", "cancelled"])
def test_cancelled_before_any_test_started(tmp_path, status):
    log = tmp_path / "synthetic.txt"
    # fixture-write: Construct an interrupted synthetic session with no test node.
    log.write_text("collecting ...\n", encoding="utf-8")
    summary = summarize(log, status)
    assert "cancelled" in summary.lower()
    assert "No started test identified" in summary


@pytest.mark.parametrize("content, expected", [
    ("[gw0] [ 50%] PASSED tests/test_synthetic.py::test_done\n",
     "Last reported test (may have completed): `tests/test_synthetic.py::test_done`"),
    ("ERROR tests/test_synthetic.py::test_setup - RuntimeError: synthetic\n1 error in 0.1s\n",
     "tests/test_synthetic.py::test_setup"),
    ("\x1b[31mFAILED tests/test_synthetic.py::test_bad\x1b[0m - synthetic\n1 failed in 0.1s\n",
     "tests/test_synthetic.py::test_bad"),
    ("1 passed in 0.1s\n", "No failed tests identified"),
])
def test_other_supported_pytest_output(tmp_path, content, expected):
    log = tmp_path / "synthetic.txt"
    # fixture-write: Construct supported synthetic pytest variations.
    log.write_text(content, encoding="utf-8")
    assert expected in summarize(log)


def test_cancellation_is_reported_even_after_failure_summary():
    summary = summarize(FIXTURES / "failed.txt", "cancelled")
    assert "cancelled" in summary.lower()
    assert "tests/test_synthetic_summary.py::test_beta[small]" in summary


def test_cli_usage_problem_is_nonfatal():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0
    assert FALLBACK in result.stdout
    assert not result.stderr


def test_markdown_cannot_be_injected_by_test_parameters(tmp_path):
    log = tmp_path / "synthetic.txt"
    # fixture-write: Use synthetic markup in a pytest parameter to test rendering.
    log.write_text("FAILED tests/test_synthetic.py::test_bad[`<tag>`] - synthetic\n1 failed in 0.1s\n")
    summary = summarize(log)
    assert "<tag>" not in summary
    assert "test_bad[&#96;&lt;tag&gt;&#96;]" in summary


def test_workflow_preserves_pytest_exit_and_summarizes_afterwards(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/quality-gates.yml").read_text())
    steps = workflow["jobs"]["application-shards"]["steps"]
    run = next(step for step in steps if step.get("name") == "Run application suite")
    summary = next(step for step in steps if "scripts/ci_test_summary.py" in step.get("run", ""))
    assert steps.index(summary) == steps.index(run) + 1
    assert "failure()" in summary["if"] and "cancelled()" in summary["if"]
    assert '>> "$GITHUB_STEP_SUMMARY"' in summary["run"]
    assert summary["env"]["TEST_RUN_STATUS"] == "${{ job.status }}"
    assert summary["continue-on-error"] is True
    command = run["run"]
    assert "set -o pipefail" in command
    pytest_command = command.split("set -o pipefail\n", 1)[1].split("2>&1", 1)[0]
    assert pytest_command.startswith(".venv/bin/python -u -m pytest ")
    for code in (0, 7):
        synthetic = command.replace(pytest_command, f"(echo synthetic-out; echo synthetic-err >&2; exit {code}) ")
        synthetic = synthetic.replace("${{ runner.temp }}", str(tmp_path))
        # fixture-write: The workflow tee writes only synthetic process output here.
        result = subprocess.run(["bash", "-e", "-c", synthetic], capture_output=True, text=True,
                                env={**os.environ, "RUNNER_TEMP": str(tmp_path)})
        assert result.returncode == code
        log = tmp_path / "application-tests.log"
        assert log.read_text() == "synthetic-out\nsynthetic-err\n"
    report = tmp_path / "summary.md"
    # fixture-write: The actual summary step appends synthetic diagnostic Markdown here.
    result = subprocess.run(["bash", "-e", "-c", summary["run"]], cwd=ROOT,
                            capture_output=True, text=True,
                            env={**os.environ, "RUNNER_TEMP": str(tmp_path),
                                 "GITHUB_STEP_SUMMARY": str(report), "TEST_RUN_STATUS": "cancelled"})
    assert result.returncode == 0
    assert "cancelled" in report.read_text().lower()
