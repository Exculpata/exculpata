"""Guard workflow apt acquisition and main-only archive cache writes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ("scripts/ci-apt-install.sh", "scripts/install-ci-browser-deps.py")
WRITER = "github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'schedule')"
DOCS = " && needs.change-scope.outputs.docs_only != 'true'"


def test_workflow_apt_and_cache_contract():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                run = step.get("run", "")
                helper = any(script in run for script in HELPERS)
                assert "apt-get" not in run or helper, (path, step)
                if helper:
                    assert step.get("timeout-minutes", 0) > 0, (path, step)
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


def test_script_acquisition_options():
    script = (ROOT / HELPERS[0]).read_text()
    for required in ("set -euo pipefail", "Acquire::Retries=3", "Acquire::http::Timeout=20",
                     "Acquire::https::Timeout=20", "timeout", "--no-download"):
        assert required in script


@pytest.mark.parametrize("failures,success,phases", [
    ([], True, ["update", "download", "install", "chown"]),
    ([1], True, ["update", "update", "download", "install", "chown"]),
    ([2], True, ["update", "download", "update", "download", "install", "chown"]),
    ([1, 2], False, ["update", "update"]),
    ([2, 4], False, ["update", "download", "update", "download"]),
    ([3], False, ["update", "download", "install"]),
])
def test_script_retries_acquisition_only(tmp_path, monkeypatch, failures, success, phases):
    log = tmp_path / "commands.jsonl"
    sudo = tmp_path / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
log = pathlib.Path(os.environ["APT_TEST_LOG"])
calls = log.read_text().splitlines() if log.exists() else []
with log.open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(100 if len(calls) + 1 in json.loads(os.environ["APT_TEST_FAILURES"]) else 0)
''')
    sudo.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + os.environ["PATH"])
    monkeypatch.setenv("APT_TEST_LOG", str(log))
    monkeypatch.setenv("APT_TEST_FAILURES", json.dumps(failures))
    archives = tmp_path / "apt archives"
    monkeypatch.setenv("APT_ARCHIVES", str(archives))
    result = subprocess.run([str(ROOT / HELPERS[0]), "synthetic-package"], capture_output=True, text=True)
    assert (result.returncode == 0) is success
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    actual = ["chown" if call[0] == "chown" else "update" if "update" in call
              else "download" if "--download-only" in call else "install" for call in calls]
    assert actual == phases
    assert (archives / "partial").is_dir()
    for phase, call in zip(actual, calls):
        if phase in {"update", "download"}:
            assert call[:5] == ["timeout", "--signal=TERM", "--kill-after=10s",
                                "60s" if phase == "update" else "180s", "apt-get"]
        if phase == "install":
            assert call[0] == "apt-get" and "--no-download" in call and "timeout" not in call
