"""The diagnostic proof must preserve failed child-suite exits, never retry them."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

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
    assert report['active']['test'] == 'test_probe.py::test_synthetic_probe'
    if crash:
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
    (tmp_path / '10/task/110/children').write_text('11')
    assert process_tree(10, tmp_path) == {
        'rss_bytes': 10240, 'threads': 4, 'fds': 0, 'processes': 2, 'truncated': False}
    assert process_tree(99, tmp_path) is None
