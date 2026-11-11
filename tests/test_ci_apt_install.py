"""Guard shared apt acquisition and main-only state cache writes."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WRITER = "github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'schedule')"
ACTION = "./.github/actions/apt-deps"


def test_publication_dependency_install_timeout():
    workflow = yaml.safe_load((ROOT / ".github/workflows/quality-gates.yml").read_text())
    steps = workflow["jobs"]["publication-scan"]["steps"]
    install = next(step for step in steps if step.get("uses") == ACTION)
    assert install["timeout-minutes"] == 8


def test_workflow_apt_and_cache_contract():
    expected = {
        ("quality-gates.yml", "publication-scan"): ("publication-scan", "ffmpeg"),
        ("quality-gates.yml", "application-shards"): (
            "application", "ffmpeg imagemagick poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa"),
        ("quality-gates.yml", "transcription"): ("transcription", "ffmpeg"),
        ("quality-gates.yml", "deployment-contract"): ("deployment-contract", "restic"),
        ("quality-gates.yml", "synthetic-browser"): ("synthetic-browser", ""),
        ("corpus.yml", "full"): (
            "corpus", "ffmpeg poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa"),
    }
    found = {}
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_id, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run", "")
                assert "apt-get" not in run, (path, step)
                assert "scripts/install-ci-browser-deps.py" not in run, (path, step)
                uses = step.get("uses", "")
                assert not uses.startswith("actions/cache"), (path, step)
                if uses == ACTION:
                    site = (path.name, job_id)
                    assert site not in found
                    found[site] = (step["with"]["name"], step["with"].get("packages", ""))
                    assert step.get("timeout-minutes", 0) > 0
                    if job_id == "application-shards":
                        assert step["if"] == "${{ needs.change-scope.outputs.docs_only != 'true' }}"
    assert found == expected
    action = yaml.safe_load((ROOT / ".github/actions/apt-deps/action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    restore, install, save = action["runs"]["steps"]
    assert restore["uses"].startswith("actions/cache/restore@")
    assert save["uses"].startswith("actions/cache/save@")
    assert "if" not in restore
    assert restore["with"]["path"] == save["with"]["path"] == "${{ runner.temp }}/apt-state"
    prefix = "apt-${{ inputs.name }}-${{ runner.os }}-${{ runner.arch }}-"
    assert restore["with"]["restore-keys"] == prefix
    assert restore["with"]["key"] == save["with"]["key"] == prefix + "${{ github.run_id }}"
    assert save["if"] == "${{ " + WRITER + " && steps.install.outputs.online == 'true' }}"
    assert install["env"]["REFRESH"] == "${{ github.ref == 'refs/heads/main' && github.event_name == 'schedule' }}"
    assert '--state "$RUNNER_TEMP/apt-state"' in install["run"]
    assert 'if [[ "$REFRESH" == true ]]; then options+=(--refresh); fi' in install["run"]
    assert 'if [[ -f "$RUNNER_TEMP/apt-state/online" ]]; then' in install["run"]
    assert "echo 'online=true'" in install["run"]
