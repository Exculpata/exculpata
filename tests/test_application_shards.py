"""Keep the application file partition complete and its required check blocking."""

from collections import Counter
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/application-test-shard.py"


def workflow_jobs():
    return yaml.safe_load((ROOT / ".github/workflows/quality-gates.yml").read_text())["jobs"]


def test_shards_cover_every_application_test_file_exactly_once():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert config["testpaths"] == ["tests"]
    assert config.get("python_files", ["test_*.py", "*_test.py"]) == ["test_*.py", "*_test.py"]
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("*.py")
        if path.name.startswith("test_") or path.name.endswith("_test.py")
    }
    indices = workflow_jobs()["application-shards"]["strategy"]["matrix"]["shard"]
    assert indices == [0, 1]
    shards = []
    for index in indices:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(index)], cwd=ROOT,
            text=True, capture_output=True, check=True,
        )
        shards.append(result.stdout.splitlines())
    assert all(shards)
    assert Counter(path for shard in shards for path in shard) == Counter(expected)
    assert abs(len(shards[0]) - len(shards[1])) <= 1


@pytest.mark.parametrize("index", ["-1", "2", "invalid"])
def test_invalid_shard_cannot_select_the_full_suite(index):
    result = subprocess.run([sys.executable, str(SCRIPT), index], capture_output=True, text=True)
    assert result.returncode == 2
    assert "invalid" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("scope", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("shards", ["success", "failure", "cancelled", "skipped"])
def test_aggregate_requires_successful_scope_and_both_shards(scope, shards):
    job = workflow_jobs()["application"]
    assert job.get("name", "application") == "application"
    assert job["needs"] == ["change-scope", "application-shards"]
    assert job["if"] == "${{ always() }}"
    assert "strategy" not in job
    assert "continue-on-error" not in job
    assert len(job["steps"]) == 1
    step = job["steps"][0]
    assert step["env"] == {
        "SCOPE_RESULT": "${{ needs.change-scope.result }}",
        "SHARD_RESULT": "${{ needs.application-shards.result }}",
    }
    result = subprocess.run(["bash", "-e", "-c", step["run"]],
                            env={**os.environ, "SCOPE_RESULT": scope, "SHARD_RESULT": shards})
    assert (result.returncode == 0) is (scope == shards == "success")


def test_matrix_runs_selected_files_and_retains_separate_reports():
    job = workflow_jobs()["application-shards"]
    assert job["needs"] == ["change-scope"]
    assert job["if"] == "${{ always() }}"
    assert job["strategy"]["fail-fast"] is False
    assert "continue-on-error" not in job
    steps = {step.get("name"): step for step in job["steps"]}
    selection = steps["Select application test shard"]
    assert selection["if"] == "${{ needs.change-scope.outputs.docs_only != 'true' }}"
    assert 'scripts/application-test-shard.py ${{ matrix.shard }}' in selection["run"]
    assert '> "$RUNNER_TEMP/application-test-files.txt"' in selection["run"]
    run = steps["Run application suite"]
    assert run["if"] == selection["if"]
    assert run["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "1"
    assert '@"${{ runner.temp }}/application-test-files.txt"' in run["run"]
    artifact = steps["Retain application test report"]
    assert "always()" in artifact["if"]
    assert "${{ matrix.shard }}" in artifact["with"]["name"]
    assert "${{ github.run_id }}" in artifact["with"]["name"]
    assert "${{ github.run_attempt }}" in artifact["with"]["name"]
