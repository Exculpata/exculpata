"""Execute the hosted restore report gate against synthetic outcomes."""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def restore_step():
    workflow = (ROOT / ".github/workflows/quality-gates.yml").read_text()
    job = workflow.split("  deployment-contract:\n", 1)[1].split("\n  synthetic-browser:", 1)[0]
    step = job.split("      - name: Run both encrypted restore naming generations without skips\n", 1)[1]
    return step.split("      - name:", 1)[0]


def run_report_gate(tmp_path, names=("recordbench", "exculpata"), outcome="", content=None):
    report = tmp_path / "restore-results.xml"
    if content is None:
        content = "<testsuites><testsuite>" + "".join(
            f'<testcase name="test_encrypted_split_snapshot_and_postgres_restore[{name}]">'
            f'{outcome}</testcase>' for name in names
        ) + "</testsuite></testsuites>"
    report.write_text(content)
    source = restore_step().split("<<'PYCODE'\n", 1)[1].split("          PYCODE", 1)[0]
    return subprocess.run([sys.executable, "-", str(report)], input=textwrap.dedent(source),
                          capture_output=True, text=True)


def test_hosted_restore_command_enables_integration_and_selects_both_names():
    step = restore_step()
    assert 'RECORDBENCH_BACKUP_INTEGRATION: "1"' in step
    assert "tests/test_backup_consistency.py::test_encrypted_split_snapshot_and_postgres_restore \\" in step
    assert '--junitxml="$RUNNER_TEMP/restore-results.xml"' in step
    assert 'python - "$RUNNER_TEMP/restore-results.xml"' in step
    assert "continue-on-error" not in step


def test_restore_report_requires_both_passes(tmp_path):
    result = run_report_gate(tmp_path)
    assert result.returncode == 0
    assert "2 passed, zero skipped" in result.stdout


@pytest.mark.parametrize("outcome", ["<skipped/>", "<failure/>", "<error/>"])
def test_restore_report_rejects_nonpassing_cases(tmp_path, outcome):
    result = run_report_gate(tmp_path, outcome=outcome)
    assert result.returncode != 0
    assert "must pass without skips" in result.stderr


@pytest.mark.parametrize("names", [(), ("recordbench",), ("exculpata",),
                                   ("recordbench", "recordbench"), ("recordbench", "unknown")])
def test_restore_report_rejects_missing_or_wrong_generation(tmp_path, names):
    result = run_report_gate(tmp_path, names=names)
    assert result.returncode != 0
    assert "requires both naming generations" in result.stderr


def test_restore_report_rejects_malformed_xml(tmp_path):
    assert run_report_gate(tmp_path, content="invalid XML").returncode != 0
