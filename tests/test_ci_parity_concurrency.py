"""Synthetic scheduling contracts; no external services or real data."""

from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time

import pytest

from scripts import ci_parity_plan as plan
from scripts import ci_parity_runner as execution
from tests.test_ci_parity import document, repository, run_repository
from tests.test_application_shards import ROOT, SCRIPT, SERIAL_TESTS, workflow_jobs


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
@pytest.mark.parametrize('independent_first', [False, True])
def test_serial_tests_wait_for_shards_and_run_alone(tmp_path, monkeypatch, independent_first, serial):
    monkeypatch.setenv('CI_PARITY_SERIAL', serial)
    monkeypatch.setattr(execution, 'cpu_budget', lambda: 16)
    data = document()
    jobs = workflow_jobs()
    shards = {'strategy': jobs['application-shards']['strategy'], 'steps': [
        step for step in jobs['application-shards']['steps']
        if step.get('name') in ('Select application test shard', 'Run application suite')]}
    isolated = {'needs': 'application-shards', 'steps': [
        step for step in jobs['application-row-budget']['steps']
        if '-m pytest' in step.get('run', '')]}
    assert len(isolated['steps']) == 1
    assert [token for token in shlex.split(isolated['steps'][0]['run'])
            if token.startswith('tests/')] == SERIAL_TESTS
    independent = {'steps': [{'run': 'true'}]}
    data['jobs'] = {'application-shards': shards}
    if independent_first:
        data['jobs']['independent'] = independent
    data['jobs']['application-row-budget'] = isolated
    if not independent_first:
        data['jobs']['independent'] = {**independent, 'needs': 'application-shards'}
    lock = threading.Lock()
    independent_started = threading.Event()
    shards_finished = threading.Event()
    row_started = threading.Event()
    finished, intervals = set(), {}

    def cell(self, workflow, job, matrix, context, ancestor_failure=False):
        key = (job.name, matrix.get('shard'))
        start = time.monotonic()
        if job.name == 'application-shards':
            arguments = subprocess.check_output(
                [sys.executable, str(SCRIPT), str(matrix['shard'])], cwd=ROOT, text=True,
            ).splitlines()
            assert [arg for arg in arguments if arg.startswith('--')] == [
                f'--deselect={node}' for node in SERIAL_TESTS]
            if independent_first and serial == '0':
                assert independent_started.wait(2)
            with lock:
                finished.add(matrix['shard'])
                if len(finished) == 2:
                    shards_finished.set()
        elif job.name == 'independent':
            independent_started.set()
            assert shards_finished.wait(2)
            if independent_first and serial == '0':
                row_started.wait(0.1)
            time.sleep(0.03)
        else:
            assert finished == {0, 1}
            row_started.set()
            time.sleep(0.03)
        with lock:
            intervals[key] = (start, time.monotonic())
        return 'success', {}

    monkeypatch.setattr(execution.Runner, 'cell', cell)
    _, messages, _ = run_repository(tmp_path, data)
    row_start, row_end = intervals.pop(('application-row-budget', None))
    for (name, _), (start, end) in intervals.items():
        if name == 'application-shards':
            assert end <= row_start
        assert end <= row_start or start >= row_end, f'{name} overlapped serial tests'

    schedule = [line for line in messages if line.startswith('SCHEDULE ')]
    if serial == '1':
        assert list(intervals)[:2] == [('application-shards', 0), ('application-shards', 1)]
        assert not schedule
        return
    assert len(schedule) == 8
    for key, (start, end) in {**intervals, ('application-row-budget', None): (row_start, row_end)}.items():
        name, shard = key
        matrix = '{}' if shard is None else f'{{"shard": {shard}}}'
        tag = f'synthetic:{name} {matrix}'
        boundaries = [line for line in schedule if tag in line]
        assert len(boundaries) == 2
        assert boundaries[0].startswith('SCHEDULE START ')
        assert boundaries[1].startswith('SCHEDULE END ')
        assert float(boundaries[0].split('monotonic=')[1]) <= start
        assert float(boundaries[1].split('monotonic=')[1]) >= end


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

    def shell(command, cwd, env, explicit_bash, timeout, log, cancelled=None):
        assert 'PYTEST_ADDOPTS' not in env if command == 'first' else env['PYTEST_ADDOPTS'] == str(cwd)
        paths = {Path(env[key]) for key in (
            'HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
            'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'HF_HOME', 'TORCH_HOME',
            'MPLCONFIGDIR', 'NUMBA_CACHE_DIR',
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


@pytest.mark.parametrize('serial', ['0', '1'])
@pytest.mark.parametrize('inherited_prefix', [False, True])
def test_real_cells_preserve_source_adjacent_bytecode_lookup(tmp_path, monkeypatch, serial, inherited_prefix):
    monkeypatch.setenv('CI_PARITY_SERIAL', serial)
    if inherited_prefix:
        monkeypatch.setenv('PYTHONPYCACHEPREFIX', str(tmp_path / 'caller-cache'))
    else:
        monkeypatch.delenv('PYTHONPYCACHEPREFIX', raising=False)
    command = """python - <<'PY'
import importlib.util
import os
from pathlib import Path
import py_compile
import subprocess
import sys

assert 'PYTHONPYCACHEPREFIX' not in os.environ
assert sys.pycache_prefix is None
source = Path('synthetic_cache.py')
source.write_text('VALUE = "cached"\\n')
stamp = source.stat()
py_compile.compile(str(source), doraise=True)
source.write_text('VALUE = "source"\\n')
os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c',
    'import sys; sys.path.insert(0, sys.argv[1]); import synthetic_cache; print(synthetic_cache.VALUE)',
    str(Path.cwd())], capture_output=True, text=True, check=True)
assert result.stdout.strip() == 'cached', (result.stdout, sys.pycache_prefix)
assert Path(importlib.util.cache_from_source(str(source))).resolve().parent == Path.cwd() / '__pycache__'
PY
"""
    data = document([{'run': command}],
                    strategy={'fail-fast': False, 'matrix': {'cell': [0, 1]}})
    runner, messages, _ = run_repository(tmp_path, data)
    assert runner.ran == 2 and runner.failed == 0, '\n'.join(messages)
    if inherited_prefix:
        assert execution.os.environ['PYTHONPYCACHEPREFIX'] == str(tmp_path / 'caller-cache')


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


def test_interrupt_drains_cells_and_terminates_descendants(tmp_path, monkeypatch):
    monkeypatch.delenv('CI_PARITY_SERIAL', raising=False)
    data = document([{'run': 'sleep 3 & echo $!; wait'}],
                    strategy={'fail-fast': False, 'matrix': {'cell': [0, 1]}})
    pids = []

    def interrupt_when_started(*args, **kwargs):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            values = [path.read_text().strip() for path in tmp_path.glob('parity-*.log')]
            if len(values) == 2 and all(value.isdecimal() for value in values):
                pids.extend(int(value) for value in values)
                raise KeyboardInterrupt
            time.sleep(0.01)
        pytest.fail('both shell processes must start')

    monkeypatch.setattr(execution, 'wait', interrupt_when_started)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_repository(tmp_path, data)
    assert time.monotonic() - started < 2
    assert len(pids) == 2
    for pid in pids:
        status = Path('/proc') / str(pid) / 'stat'
        assert not status.exists() or status.read_text().split()[2] == 'Z'
    assert not list(tmp_path.glob('ci-parity-*'))
