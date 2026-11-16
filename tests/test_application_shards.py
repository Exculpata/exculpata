"""Keep the application file partition complete and its required check blocking."""

from collections import Counter
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/application-test-shard.py"
ROW_BUDGET = "tests/test_discovery_review_index.py::test_rebuild_step_row_budget[10000]"


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
        arguments = result.stdout.splitlines()
        assert [arg for arg in arguments if arg.startswith("--")] == [f"--deselect={ROW_BUDGET}"]
        shards.append([arg for arg in arguments if not arg.startswith("--")])
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
@pytest.mark.parametrize("serial", ["success", "failure", "cancelled", "skipped"])
def test_aggregate_requires_successful_scope_shards_and_serial_test(scope, shards, serial):
    job = workflow_jobs()["application"]
    assert job.get("name", "application") == "application"
    assert job["needs"] == ["change-scope", "application-shards", "application-row-budget"]
    assert job["if"] == "${{ always() }}"
    assert "strategy" not in job
    assert "continue-on-error" not in job
    assert len(job["steps"]) == 1
    step = job["steps"][0]
    assert step["env"] == {
        "SCOPE_RESULT": "${{ needs.change-scope.result }}",
        "SHARD_RESULT": "${{ needs.application-shards.result }}",
        "SERIAL_RESULT": "${{ needs.application-row-budget.result }}",
    }
    result = subprocess.run(["bash", "-e", "-c", step["run"]],
                            env={**os.environ, "SCOPE_RESULT": scope, "SHARD_RESULT": shards,
                                 "SERIAL_RESULT": serial})
    assert (result.returncode == 0) is (scope == shards == serial == "success")


def test_row_budget_runs_alone_after_shards():
    job = workflow_jobs()["application-row-budget"]
    assert job["needs"] == ["change-scope", "application-shards"]
    assert job["if"] == "${{ always() }}"
    assert "strategy" not in job
    assert "continue-on-error" not in job
    steps = job["steps"]
    commands = [step for step in steps if "-m pytest" in step.get("run", "")]
    assert len(commands) == 1
    step = commands[0]
    assert step["name"] == "Run isolated row budget test"
    assert step["if"] == "${{ needs.change-scope.outputs.docs_only != 'true' }}"
    assert "continue-on-error" not in step
    tokens = shlex.split(step["run"])
    assert tokens[:7] == ["python", "-u", "-m", "pytest", "-n", "0", "-v"]
    assert [token for token in tokens if token.startswith("tests/")] == [ROW_BUDGET]
    assert "--timeout=360" in tokens
    assert "--durations=0" in tokens
    assert not any(token in tokens for token in ("&", ";", "&&", "||", "--deselect", "--lf"))
    assert step["env"]["CASE_INTELLIGENCE_TEST_MODE"] == "1"
    assert step["env"]["CASE_INTELLIGENCE_STORAGE_RESERVE_GIB"] == "0"
    guard = [step.get("run") for step in steps if "corpus-write-guard.sh" in step.get("run", "")]
    assert guard == ["bash scripts/corpus-write-guard.sh prepare", "bash scripts/corpus-write-guard.sh check"]


def test_shard_collection_excludes_only_serial_parameter(tmp_path):
    test_file = ROW_BUDGET.split("::")[0]
    owning_shards = []
    for index in (0, 1):
        arguments = subprocess.check_output([sys.executable, str(SCRIPT), str(index)], text=True).splitlines()
        if test_file in arguments:
            owning_shards.append(arguments)
    assert len(owning_shards) == 1
    arguments = owning_shards[0]
    argument_file = tmp_path / "shard.txt"
    argument_file.write_text("\n".join(arg for arg in arguments if arg == test_file or arg.startswith("--")))

    def collect(*args):
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
                                cwd=ROOT, env={**os.environ, "PYTEST_ADDOPTS": "",
                                               "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT)))},
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return {line for line in result.stdout.splitlines() if line.startswith(test_file + "::")}

    all_nodes = collect(test_file)
    shard_nodes = collect("@" + str(argument_file))
    assert ROW_BUDGET in all_nodes
    assert ROW_BUDGET not in shard_nodes
    assert shard_nodes == all_nodes - {ROW_BUDGET}
    assert any("test_rebuild_step_row_budget[" in node for node in shard_nodes)


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
    assert run["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "6"
    assert '@"${{ runner.temp }}/application-test-files.txt"' in run["run"]
    artifact = steps["Retain application test report"]
    assert "always()" in artifact["if"]
    assert "${{ matrix.shard }}" in artifact["with"]["name"]
    assert "${{ github.run_id }}" in artifact["with"]["name"]
    assert "${{ github.run_attempt }}" in artifact["with"]["name"]
