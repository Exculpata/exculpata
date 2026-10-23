"""The diagnostic proof must preserve failed child-suite exits, never retry them."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tests import worker_diagnostics as diagnostics
from tests.worker_diagnostics import cgroup_memory, exit_details, oom_delta, process_tree

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('body, expected, code, sig', [
    ('os._exit(23)', 1, 23, None),
    ('os.kill(os.getpid(), signal.SIGTERM)', 1, None, 'SIGTERM'),
    ('time.sleep(10)', 1, 1, None),
    ('assert False, "synthetic assertion"', 1, None, None),
    ('assert True', 0, None, None),
])
def test_bounded_worker_exit_proof(tmp_path, body, expected, code, sig):
    (tmp_path / 'pytest.ini').write_text(
        '[pytest]\nfaulthandler_timeout = 1\nfaulthandler_exit_on_timeout = true\n')
    (tmp_path / 'test_probe.py').write_text(
        'import os, signal, time\ndef test_synthetic_probe():\n    ' + body + '\n')
    directory = tmp_path / 'diagnostics'
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_ADDOPTS': ''}
    # A nested run has its own controller; never inherit an enclosing worker ID.
    for key in tuple(env):
        if key.startswith('PYTEST_XDIST_'):
            env.pop(key)
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'tests.worker_diagnostics',
        '-n', '1', '--max-worker-restart=0', '-v', '--show-capture=no',
        '--worker-diagnostics-dir', str(directory)], cwd=tmp_path,
        env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr
    reports = list(directory.glob('*-exit.json'))
    assert len(reports) == 1, 'must not replace the failed worker'
    report = json.loads(reports[0].read_text())
    crash = body not in ('assert True', 'assert False, "synthetic assertion"')
    if crash:
        assert report['exit_code'] == code
    else:
        assert report['pytest_exitstatus'] == expected
    assert report['signal'] == sig
    assert report['crashed'] == crash
    assert ('WORKER EXIT DIAGNOSTICS' in result.stdout) == crash
    if not crash:
        assert report['active'] is None
    if crash:
        assert report['active']['test'] == 'test_probe.py::test_synthetic_probe'
        assert report['active']['phase'] == 'call'
        assert 'worker restarting disabled' in result.stdout
    if body == 'time.sleep(10)':
        assert 'Timeout (0:00:01)!' in result.stderr
        assert report['active']['phase_seconds'] >= 1
    assert 'limits' in json.loads((directory / 'runner-start.json').read_text())
    assert (directory / 'runner-end.json').is_file()


def test_signal_is_not_an_oom_diagnosis():
    assert exit_details(-signal.SIGKILL) == {
        'exit_code': None, 'signal': 'SIGKILL', 'returncode': -signal.SIGKILL}
    assert exit_details(None) == {'exit_code': None, 'signal': None, 'returncode': None}
    assert oom_delta(None, None) is None
    assert oom_delta([{'events': None}], [{'events': None}]) == [None]
    assert oom_delta([{'events': {'oom_kill': 5}}], [{'events': {'oom_kill': 6}}]) == [{'oom_kill': 1}]


def test_cgroup_limits_and_ancestor_oom_counters(tmp_path):
    proc = tmp_path / 'proc'
    mount = tmp_path / 'cgroup'
    (proc / 'self').mkdir(parents=True)
    (mount / 'job').mkdir(parents=True)
    (proc / 'self/cgroup').write_text('0::/job\n')
    (proc / 'self/mountinfo').write_text(f'1 0 0:1 / {mount} rw - cgroup2 cgroup rw\n')
    for folder, limit, kills in ((mount, 'max', 2), (mount / 'job', '1024', 1)):
        (folder / 'memory.max').write_text(limit)
        (folder / 'memory.events').write_text(f'oom 3\noom_kill {kills}\n')
    rows = cgroup_memory(proc, mount)
    assert [row['memory.max'] for row in rows] == [1024, 'max']
    assert [row['events']['oom_kill'] for row in rows] == [1, 2]
    assert rows[0]['memory.current'] is None
    (proc / 'self/cgroup').write_text('0::/../../outside\n')
    assert cgroup_memory(proc, mount) is None


def test_process_sampler_includes_children_started_by_other_threads(tmp_path):
    for pid in (10, 11):
        folder = tmp_path / str(pid)
        (folder / 'task' / str(pid + 100)).mkdir(parents=True)
        (folder / 'fd').mkdir()
        (folder / 'status').write_text('VmRSS:\t5 kB\nThreads:\t2\n')
        (folder / 'task' / str(pid + 100) / 'children').write_text('')
    (tmp_path / '10/task/110/children').write_text('11')
    metrics = {'rss_bytes': 10240, 'threads': 4, 'fds': 0, 'processes': 2}
    assert process_tree(10, tmp_path) == {
        **metrics, 'observed': metrics, 'partial': {key: False for key in metrics},
        'tree_complete': True, 'truncated': False}
    assert process_tree(99, tmp_path) is None


def synthetic_process(proc, pid):
    folder = proc / str(pid)
    (folder / 'task' / str(pid)).mkdir(parents=True)
    (folder / 'fd').mkdir()
    (folder / 'status').write_text('VmRSS: 5 kB\nThreads: 2\n')
    (folder / 'task' / str(pid) / 'children').write_text('')
    return folder


@pytest.mark.parametrize('missing, metric', [('VmRSS', 'rss_bytes'), ('Threads', 'threads')])
def test_missing_process_field_is_unavailable_not_zero(tmp_path, missing, metric):
    folder = synthetic_process(tmp_path, 10)
    status = folder / 'status'
    status.write_text('\n'.join(line for line in status.read_text().splitlines()
                               if not line.startswith(missing)))
    sample = process_tree(10, tmp_path)
    assert sample[metric] is None
    assert sample['partial'][metric]
    assert sample['observed'][metric] == 0  # explicitly a partial observation
    assert sample['tree_complete']
    assert sample['processes'] == 1


@pytest.mark.parametrize('source', ['fd', 'task', 'children', 'descendant-status'])
def test_unreadable_process_sources_are_explicit(tmp_path, monkeypatch, source):
    folder = synthetic_process(tmp_path, 10)
    child = synthetic_process(tmp_path, 11)
    (folder / 'task/10/children').write_text('11')
    scandir, open_file = os.scandir, Path.open
    target = {'fd': folder / 'fd', 'task': folder / 'task',
              'children': folder / 'task/10/children',
              'descendant-status': child / 'status'}[source]

    def denied_directory(path):
        if Path(path) == target:
            raise PermissionError('synthetic unreadable source')
        return scandir(path)

    def denied_file(path, *args, **kwargs):
        if path == target:
            raise PermissionError('synthetic unreadable source')
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(os, 'scandir', denied_directory)
    monkeypatch.setattr(Path, 'open', denied_file)
    sample = process_tree(10, tmp_path)
    assert sample['fds'] is None
    assert sample['partial']['fds']
    assert not sample['truncated']  # unavailable is distinct from hitting a cap
    if source == 'fd':
        assert sample['tree_complete'] and sample['rss_bytes'] == 10240
    else:
        assert not sample['tree_complete']
        assert all(sample[key] is None for key in sample['partial'])
        assert all(sample['partial'].values())
        assert sample['observed']['processes'] == 1


@pytest.mark.parametrize('boundary', ['tasks', 'processes'])
def test_process_tree_collection_caps_mark_totals_incomplete(tmp_path, boundary):
    folder = synthetic_process(tmp_path, 10)
    if boundary == 'tasks':
        for tid in range(100, 612):
            task = folder / 'task' / str(tid)
            task.mkdir()
            (task / 'children').write_text('')
    else:
        (folder / 'task/10/children').write_text(' '.join(str(pid) for pid in range(11, 141)))
    sample = process_tree(10, tmp_path)
    assert sample['truncated']
    assert not sample['tree_complete']
    assert all(sample['partial'].values())
    assert all(sample[key] is None for key in sample['partial'])


def test_partial_and_unavailable_samples_do_not_become_healthy_peaks(tmp_path):
    folder = synthetic_process(tmp_path, 10)
    metrics = ('rss_bytes', 'threads', 'fds', 'processes')
    row = {'samples': 0, 'unavailable_samples': 0, 'latest': None,
           'partial_samples': {key: 0 for key in metrics}, 'peak': {key: None for key in metrics}}
    diagnostics.accumulate_sample(row, None)
    assert all(value is None for value in row['peak'].values())
    (folder / 'status').write_text('Threads: 2\n')
    sample = process_tree(10, tmp_path)
    diagnostics.accumulate_sample(row, sample)
    assert row['peak']['rss_bytes'] is None and row['peak']['threads'] == 2
    assert row['partial_samples']['rss_bytes'] == 1
    assert row['unavailable_samples'] == 1
    diagnostics.accumulate_sample(row, None)
    assert row['latest'] is None  # do not present a stale sample as current
    assert row['unavailable_samples'] == 2


def test_task_enumeration_stops_before_materializing_all_entries(tmp_path, monkeypatch):
    from types import SimpleNamespace
    folder = synthetic_process(tmp_path, 10)
    original = os.scandir
    consumed = []

    class TaskEntries:
        def __enter__(self):
            def entries():
                for index in range(514):
                    assert index < 513, 'task collection exceeded its bounded lookahead'
                    consumed.append(index)
                    yield SimpleNamespace(path=str(folder / 'task/10'))
            return entries()

        def __exit__(self, *args):
            pass

    def scan_tasks(path):
        return TaskEntries() if Path(path) == folder / 'task' else original(path)

    monkeypatch.setattr(os, 'scandir', scan_tasks)
    sample = process_tree(10, tmp_path)
    assert len(consumed) == 513
    assert sample['truncated'] and not sample['tree_complete']


@pytest.mark.parametrize('membership, host_root, suffix', [
    ('/', '/host/subtree', ''),
    ('/job', '/host/subtree', 'job'),
    ('/host/subtree/job', '/host/subtree', 'job'),
])
def test_cgroup_namespace_membership_keeps_available_oom_evidence(tmp_path, membership, host_root, suffix):
    proc, mount = tmp_path / 'proc', tmp_path / 'cgroup'
    (proc / 'self').mkdir(parents=True)
    group = mount / suffix
    group.mkdir(parents=True)
    (proc / 'self/cgroup').write_text(f'0::{membership}\n')
    (proc / 'self/mountinfo').write_text(f'1 0 0:1 {host_root} {mount} rw - cgroup2 cgroup rw\n')
    (group / 'memory.max').write_text('4096')
    (group / 'memory.events').write_text('oom 2\noom_kill 1\n')
    rows = cgroup_memory(proc, mount)
    assert rows[0]['memory.max'] == 4096
    assert rows[0]['events'] == {'oom': 2, 'oom_kill': 1}


@pytest.mark.parametrize('crash_during', ['teardown', 'sessionfinish'])
def test_crash_marker_survives_teardown_but_not_completed_protocol(tmp_path, crash_during):
    (tmp_path / 'test_probe.py').write_text('def test_synthetic_completed():\n    pass\n')
    if crash_during == 'teardown':
        hook = 'def pytest_runtest_teardown(item):\n    os._exit(19)\n'
    else:
        hook = ('def pytest_sessionfinish(session):\n'
                '    if hasattr(session.config, "workerinput"):\n        os._exit(19)\n')
    (tmp_path / 'conftest.py').write_text('''import os
import pytest
@pytest.hookimpl(trylast=True)
''' + hook)
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_ADDOPTS': ''}
    for key in tuple(env):
        if key.startswith('PYTEST_XDIST_'):
            env.pop(key)
    directory = tmp_path / 'diagnostics'
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'tests.worker_diagnostics',
        '-n', '1', '--max-worker-restart=0', '-q', '--worker-diagnostics-dir', str(directory)],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 1
    report = json.loads((directory / 'gw0-exit.json').read_text())
    assert report['crashed'] and report['exit_code'] == 19
    if crash_during == 'teardown':
        assert report['active']['phase'] == 'teardown'
        assert report['active']['test'] == 'test_probe.py::test_synthetic_completed'
    else:
        assert report['active'] is None
