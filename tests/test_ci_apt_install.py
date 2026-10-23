"""Guard workflow apt acquisition and main-only archive cache writes."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ("scripts/install-ci-browser-deps.py",)
WRITER = "github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'schedule')"
DOCS = " && needs.change-scope.outputs.docs_only != 'true'"


def test_workflow_apt_and_cache_contract():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_id, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run", "")
                helper = any(script in run for script in HELPERS)
                assert "apt-get" not in run or helper, (path, step)
                if helper:
                    assert step.get("timeout-minutes", 0) > 0, (path, step)
                    if job_id != "synthetic-browser":
                        assert '--archives "$RUNNER_TEMP/apt-archives"' in run, (path, step)
                uses = step.get("uses", "")
                assert not uses.startswith("actions/cache@"), (path, step)
                if uses.startswith("actions/cache/save@"):
                    assert step.get("if") in {
                        "${{ " + WRITER + " }}", "${{ (" + WRITER + ")" + DOCS + " }}",
                    }, (path, step)
                if uses.startswith("actions/cache/restore@"):
                    assert step.get("if") in {
                        "${{ !(" + WRITER + ") }}", "${{ (!(" + WRITER + "))" + DOCS + " }}",
                    }, (path, step)
