"""Contracts for the scheduled stale and dependency hygiene reports."""
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "stale-report.yml": "47 3 * * 1",
    "deps-audit.yml": "57 3 * * 1",
}
PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_scheduled_hygiene_workflow_contracts():
    for name, cron in WORKFLOWS.items():
        workflow = load(name)
        events = workflow[True]
        assert set(events) == {"schedule", "workflow_dispatch"}
        assert events["schedule"] == [{"cron": cron}]
        assert workflow["permissions"] == {}
        measure = workflow["jobs"]["measure"]
        expected = {"contents": "read"}
        if name == "stale-report.yml":
            expected["actions"] = "read"
        assert measure["permissions"] == expected
        filing = workflow["jobs"]["file"]
        assert filing["needs"] == "measure"
        assert filing["permissions"] == {"issues": "write"}
        filing_text = '\n'.join(step.get('run', '') for step in filing['steps'])
        assert all(f'gh issue {verb}' in filing_text for verb in ('list', 'create', 'edit'))
        assert all('--' + flag in filing_text for flag in
                   ('report', 'issues', 'kind', 'run', 'scanner', 'deny-file', 'body', 'decision', 'summary'))
        assert '--run "$GITHUB_SERVER_URL/$GH_REPO/actions/runs/$GITHUB_RUN_ID"' in filing_text
        assert '--json number,title,body,labels,state' in filing_text
        assert 'exit 0' not in filing_text and '[ ! -f scripts/hygiene_issue.py ]' not in filing_text
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert PIN.fullmatch(step["uses"]), step["uses"]
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False


def test_measurement_is_report_only_and_retained():
    for name in WORKFLOWS:
        measure = load(name)["jobs"]["measure"]
        run_steps = [step for step in measure["steps"] if "run" in step]
        assert any(step.get("continue-on-error") is True for step in run_steps)
        upload = next(step for step in measure["steps"]
                      if step.get("uses", "").startswith("actions/upload-artifact@"))
        assert upload["if"] == "always()"
        assert upload["with"]["retention-days"] == 14
        assert any("GITHUB_STEP_SUMMARY" in step.get("run", "") for step in measure["steps"])


def test_stale_measurement_uses_local_api_input_and_has_no_mutations():
    text = (ROOT / ".github/workflows/stale-report.yml").read_text()
    assert "gh api" in text
    assert "--input" in text and "--output" in text
    assert "gh api --method DELETE" not in text
    assert "disable" not in text.lower()


def test_dependency_install_is_hash_locked():
    workflow = load("deps-audit.yml")
    install = next(step for step in workflow["jobs"]["measure"]["steps"]
                   if step.get("name") == "Install dependency auditor")
    assert install["run"] == (
        "python -m pip install --require-hashes -r requirements/deps-audit.lock"
    )
