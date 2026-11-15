"""Synthetic scheduling contracts; no external services or real data."""

from pathlib import Path
import re
import threading
import time

import pytest

from scripts import ci_parity_plan as plan
from scripts import ci_parity_runner as execution
from tests.test_ci_parity import document, repository, run_repository


def test_independent_cells_overlap_with_bounded_workers(tmp_path, monkeypatch):
    monkeypatch.delenv('CI_PARITY_SERIAL', raising=False)
    monkeypatch.setattr(execution.os, 'cpu_count', lambda: 8)
    data = document([{'run': 'python -m pytest -n 4'}],
                    strategy={'fail-fast': False, 'matrix': {'cell': [0, 1, 2, 3]}})
    lock, paired = threading.Lock(), threading.Event()
    active = peak = 0
    intervals = []

    def cell(self, workflow, job, matrix, context, ancestor_failure=False):
        nonlocal active, peak
        with lock:
            active += 4
            peak = max(peak, active)
            if active == 8:
                paired.set()
            start = time.monotonic()
        assert paired.wait(2), 'independent cells must overlap'
        time.sleep(0.03)
        with lock:
            intervals.append((start, time.monotonic()))
            active -= 4
        self.ran += 1
        return 'success', {}

    monkeypatch.setattr(execution.Runner, 'cell', cell)
    runner, _, _ = run_repository(tmp_path, data)
    assert runner.ran == 4 and peak == 8
    events = sorted([(a, 4) for a, _ in intervals] + [(b, -4) for _, b in intervals])
    workers = 0
    for _, delta in events:
        workers += delta
        assert workers <= 8


def normalized(messages):
    return [re.sub(r' seconds=\S+| log=\S+', '', line)
            for line in messages if line.startswith(('PASS ', 'FAIL ', 'SKIP ', 'NOT RUN ', 'JOB '))]


@pytest.mark.parametrize('failing', [False, True])
def test_serial_and_concurrent_outcomes_conditions_and_logs(tmp_path, monkeypatch, failing):
    data = document([
        {'name': 'first', 'id': 'first', 'run': 'echo first; ' + ('false' if failing else 'true')},
        {'name': 'ordinary', 'run': 'echo ordinary'},
        {'name': 'recovery', 'if': 'failure()', 'run': 'echo recovery'},
        {'name': 'tolerated', 'if': 'always()', 'continue-on-error': True, 'run': 'echo tolerated; false'},
        {'name': 'cleanup', 'if': 'always()', 'run': 'echo cleanup'},
    ], strategy={'fail-fast': False, 'matrix': {'cell': [0, 1]}})
    data['jobs']['dependent'] = {'needs': 'build', 'steps': [{'run': 'echo dependent'}]}
    data['jobs']['independent'] = {'steps': [{'run': 'echo independent'}]}
    outcomes = []
    for serial in ('1', '0'):
        monkeypatch.setenv('CI_PARITY_SERIAL', serial)
        directory = tmp_path / serial
        directory.mkdir()
        runner, messages, _ = run_repository(directory, data)
        outcomes.append((runner.results, runner.ran, runner.failed, runner.not_run, normalized(messages)))
        assert runner.failed == (2 if failing else 0)
        assert runner.results['synthetic']['dependent']['result'] == ('skipped' if failing else 'success')
        cells = [line for line in messages if line.startswith('CELL ')]
        assert '"cell": 0' in cells[0] and '"cell": 1' in cells[1]
        assert len(list(directory.glob('parity-*.log'))) == runner.ran
    assert outcomes[0] == outcomes[1]


def test_needs_waits_for_all_matrix_cells_and_carries_outputs(tmp_path, monkeypatch):
    monkeypatch.delenv('CI_PARITY_SERIAL', raising=False)
    data = document([{'run': 'true'}],
                    strategy={'fail-fast': False, 'matrix': {'cell': [0, 1]}})
    data['jobs']['dependent'] = {'needs': 'build', 'steps': [{'run': 'true'}]}
    finished = []

    def cell(self, workflow, job, matrix, context, ancestor_failure=False):
        if job.name == 'build':
            time.sleep(0.06 if matrix['cell'] == 0 else 0.01)
            finished.append(matrix['cell'])
            self.emit(f'BEGIN {matrix["cell"]}')
            self.emit(f'END {matrix["cell"]}')
            return 'success', {'value': str(matrix['cell'])}
        assert sorted(finished) == [0, 1]
        assert context['needs']['build'] == {'result': 'success', 'outputs': {'value': '1'}}
        return 'success', {}

    monkeypatch.setattr(execution.Runner, 'cell', cell)
    _, messages, _ = run_repository(tmp_path, data)
    assert [line for line in messages if line.startswith(('BEGIN', 'END'))] == [
        'BEGIN 0', 'END 0', 'BEGIN 1', 'END 1']


@pytest.mark.parametrize('serial', ['0', '1'])
def test_matrix_fail_fast_retains_serial_cancellation(tmp_path, monkeypatch, serial):
    monkeypatch.setenv('CI_PARITY_SERIAL', serial)
    data = document([{'run': 'false'}], strategy={'matrix': {'cell': [0, 1, 2]}})
    runner, messages, _ = run_repository(tmp_path, data)
    assert runner.failed == runner.ran == 1
    assert sum(line.startswith('SKIP (matrix fail-fast)') for line in messages) == 2


def test_max_parallel_and_light_cell_budget(tmp_path, monkeypatch):
    monkeypatch.delenv('CI_PARITY_SERIAL', raising=False)
    monkeypatch.setattr(execution.os, 'cpu_count', lambda: 2)
    data = document([{'run': 'true'}], strategy={
        'fail-fast': False, 'max-parallel': 1, 'matrix': {'cell': [0, 1, 2]}})
    data['jobs']['other'] = {'steps': [{'run': 'true'}]}
    active, peak = {}, 0
    lock = threading.Lock()
    overlap = threading.Event()

    def cell(self, workflow, job, matrix, context, ancestor_failure=False):
        nonlocal peak
        with lock:
            active[job.name] = active.get(job.name, 0) + 1
            assert active[job.name] == 1
            total = sum(active.values())
            assert total <= 2
            peak = max(peak, total)
            if total == 2:
                overlap.set()
        assert overlap.wait(2)
        time.sleep(0.03)
        with lock:
            active[job.name] -= 1
        return 'success', {}

    monkeypatch.setattr(execution.Runner, 'cell', cell)
    run_repository(tmp_path, data)
    assert peak == 2


@pytest.mark.parametrize(('command', 'env', 'expected'), [
    ('true', {}, 1),
    ('python -m pytest -n auto', {}, 4),
    ('python -m pytest -n4', {}, 4),
    ('python -m pytest --numprocesses=3', {}, 3),
    ('python -m pytest', {'PYTEST_ADDOPTS': '-n 2'}, 2),
    ('true', {'PYTEST_XDIST_AUTO_NUM_WORKERS': '6'}, 4),
])
def test_worker_reservation(tmp_path, command, env, expected):
    root, _ = repository(tmp_path, document([{'run': command, 'env': env}]))
    workflow = plan.build(root).workflows[0]
    assert execution.cell_workers(workflow, workflow.jobs[0], 8) == expected


def test_oversized_worker_demand_fails_before_any_cell(tmp_path, monkeypatch):
    monkeypatch.delenv('CI_PARITY_SERIAL', raising=False)
    monkeypatch.setattr(execution.os, 'cpu_count', lambda: 2)
    with pytest.raises(execution.Refused, match='workers exceed CPU budget'):
        run_repository(tmp_path, document([{'run': 'python -m pytest -n 4'}]))


def test_real_cells_own_paths_caches_and_environment(tmp_path, monkeypatch):
    monkeypatch.delenv('CI_PARITY_SERIAL', raising=False)
    monkeypatch.setenv('PYTEST_ADDOPTS', 'inherited-unwanted-option')
    data = document([
        {'name': 'first', 'run': 'first'}, {'name': 'second', 'run': 'second'},
    ], strategy={'fail-fast': False, 'matrix': {'cell': [0, 1]}})
    environments, owned = {}, []
    gate = threading.Barrier(2)
    mutex = threading.Lock()

    def shell(command, cwd, env, explicit_bash, timeout, log):
        assert 'PYTEST_ADDOPTS' not in env if command == 'first' else env['PYTEST_ADDOPTS'] == str(cwd)
        paths = {Path(env[key]) for key in (
            'HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
            'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'HF_HOME', 'TORCH_HOME',
            'MPLCONFIGDIR', 'NUMBA_CACHE_DIR', 'PYTHONPYCACHEPREFIX',
            'RUNNER_TEMP', 'TMPDIR', 'GITHUB_WORKSPACE', 'GITHUB_EVENT_PATH',
            'GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STEP_SUMMARY')}
        if command == 'first':
            assert not (cwd / 'synthetic.sqlite').exists()
            (cwd / 'synthetic.sqlite').touch()
            Path(env['GITHUB_ENV']).write_text(f'PYTEST_ADDOPTS={cwd}\n')
            with mutex:
                owned.append(paths)
                environments[str(cwd)] = env.copy()
            gate.wait(timeout=5)
        else:
            assert (cwd / 'synthetic.sqlite').exists()
        log.write_text(command + '\n' + str(cwd) + '\n')
        return 0, False

    monkeypatch.setattr(execution, 'run_shell', shell)
    runner, messages, _ = run_repository(tmp_path, data)
    assert runner.ran == 4 and runner.failed == 0
    assert len(environments) == 2 and owned[0].isdisjoint(owned[1])
    assert all(not path.exists() for paths in owned for path in paths)
    assert execution.os.environ['PYTEST_ADDOPTS'] == 'inherited-unwanted-option'
    assert len(set(re.findall(r' log=(\S+)', '\n'.join(messages)))) == 4


def test_cli_green_overall_matches_serial(tmp_path, monkeypatch):
    from tests.test_ci_parity import cli, git_commit, skip_entry, write_skips

    data = document([{'name': 'Install synthetic', 'run': 'python -m pip install synthetic'},
                     {'name': 'check', 'run': 'true'}],
                    strategy={'fail-fast': False, 'matrix': {'cell': [0, 1]}})
    root, _ = repository(tmp_path, data)
    write_skips(root, [skip_entry()])
    git_commit(root)
    reports = []
    for serial in ('1', '0'):
        monkeypatch.setenv('CI_PARITY_SERIAL', serial)
        output = tmp_path / serial
        output.mkdir()
        result, report = cli(root, output)
        assert result.returncode == 0, report
        assert 'GREEN OVERALL' in report and 'not_run_unexpected=0' in report
        reports.append((normalized(report.splitlines()), report.splitlines()[-1]))
    assert reports[0] == reports[1]


def test_unknown_worker_expression_fails_closed(tmp_path):
    root, _ = repository(tmp_path, document([{'run': 'python -m pytest -n "$COUNT"'}]))
    workflow = plan.build(root).workflows[0]
    with pytest.raises(execution.expr.Unmapped, match='use a literal count or -n auto'):
        execution.cell_workers(workflow, workflow.jobs[0], 8)
