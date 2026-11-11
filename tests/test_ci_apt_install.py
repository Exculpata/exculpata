"""Guard shared APT acquisition and main-only state cache writes."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = "./.github/actions/apt-deps"
WRITER = "github.ref == 'refs/heads/main' && (github.event_name == 'push' || github.event_name == 'schedule')"
EXPECTED = {
    ("quality-gates.yml", "publication-scan"): ("publication-scan", "ffmpeg"),
    ("quality-gates.yml", "application-shards"): (
        "application", "ffmpeg imagemagick poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa"),
    ("quality-gates.yml", "transcription"): ("transcription", "ffmpeg"),
    ("quality-gates.yml", "deployment-contract"): ("deployment-contract", "restic"),
    ("quality-gates.yml", "synthetic-browser"): ("synthetic-browser", ""),
    ("corpus.yml", "full"): (
        "corpus", "ffmpeg poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa"),
}


def test_publication_dependency_install_timeout():
    workflow = yaml.safe_load((ROOT / ".github/workflows/quality-gates.yml").read_text())
    steps = workflow["jobs"]["publication-scan"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install publication inspection dependencies")
    assert install["timeout-minutes"] == 8
    assert "pip install --disable-pip-version-check 'pypdf>=6,<7'" in install["run"]


def test_workflow_apt_and_cache_contract():
    action = yaml.safe_load((ROOT / ACTION / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    restore, install, save = action["runs"]["steps"]
    cache_pin = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
    assert restore["uses"] == f"actions/cache/restore@{cache_pin}"
    assert save["uses"] == f"actions/cache/save@{cache_pin}"
    assert "if" not in restore
    assert restore["with"]["path"] == save["with"]["path"] == "${{ runner.temp }}/apt-state"
    prefix = "apt-${{ inputs.name }}-${{ runner.os }}-${{ runner.arch }}-"
    assert restore["with"]["key"] == save["with"]["key"] == prefix + "${{ github.run_id }}"
    assert restore["with"]["restore-keys"] == prefix
    assert save["if"] == "${{ " + WRITER + " && steps.install.outputs.online == 'true' }}"
    assert install["id"] == "install" and install["shell"] == "bash"
    assert install["env"]["REFRESH"] == "${{ github.ref == 'refs/heads/main' && github.event_name == 'schedule' }}"
    assert '--state "$RUNNER_TEMP/apt-state"' in install["run"]
    assert 'if [[ "$REFRESH" == true ]]; then options+=(--refresh); fi' in install["run"]
    assert 'if [[ -f "$RUNNER_TEMP/apt-state/online" ]]; then' in install["run"]
    assert "echo 'online=true' >> \"$GITHUB_OUTPUT\"" in install["run"]
    assert 'python3 scripts/install-ci-browser-deps.py "${options[@]}" "${packages[@]}"' in install["run"]
    seen = set()
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for job_id, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                run, uses = step.get("run", ""), step.get("uses", "")
                assert "apt-get" not in run and "install-ci-browser-deps.py" not in run, (path, step)
                assert not uses.startswith("actions/cache"), (path, step)
                if uses == ACTION:
                    site = (path.name, job_id)
                    assert site not in seen
                    seen.add(site)
                    name, packages = EXPECTED[site]
                    assert step["with"]["name"] == name
                    assert step["with"].get("packages", "") == packages
                    assert step.get("timeout-minutes", 0) > 0
                    if job_id == "application-shards":
                        assert step["if"] == "${{ needs.change-scope.outputs.docs_only != 'true' }}"
                    else:
                        assert "if" not in step
    assert seen == set(EXPECTED)
