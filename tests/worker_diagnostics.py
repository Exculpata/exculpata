"""Bounded, content-free xdist diagnostics; worker crashes cannot report success."""
from __future__ import annotations

import json
from itertools import islice
import os
from pathlib import Path
import resource
import signal
import subprocess
import threading
# Keep the observer independent of finite clocks patched by application tests.
from time import monotonic

import pytest


def _read(path):
    try:
        return path.read_text()[:16384]
    except OSError:
        return ''


def cgroup_memory(proc=Path('/proc'), mount=Path('/sys/fs/cgroup')):
    """Inspect the unified group and its ancestors, without exporting host paths."""
    membership = next((line[3:] for line in _read(proc / 'self/cgroup').splitlines()
                       if line.startswith('0::')), None)
    if membership is None or not membership.startswith('/'):
        return None
    # Mount roots can be namespace-relative (for example inside a container).
    roots = [line.split()[3] for line in _read(proc / 'self/mountinfo').splitlines()
             if ' - cgroup2 ' in line and line.split()[4] == str(mount)]
    root = roots[0] if roots else '/'
    try:
        relative = Path(membership).relative_to(root)
        group = mount / relative
        if '..' in relative.parts:
            return None
    except ValueError:
        # A cgroup namespace can expose membership relative to its mounted
        # subtree even though mountinfo retains the host-side root.
        relative = Path(membership).relative_to('/')
        if '..' in relative.parts:
            return None
        group = mount / relative
    result = []
    for _ in range(64):
        row = {}
        for name in ('memory.current', 'memory.peak', 'memory.max', 'memory.swap.max'):
            value = _read(group / name).strip()
            row[name] = int(value) if value.isdecimal() else ('max' if value == 'max' else None)
        events = {}
        for line in _read(group / 'memory.events').splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in {'oom', 'oom_kill', 'oom_group_kill'} and parts[1].isdecimal():
                events[parts[0]] = int(parts[1])
        row['events'] = events or None
        result.append(row)
        if group == mount:
            break
        group = group.parent
    return result


def process_tree(pid, proc=Path('/proc')):
    """Report incomplete metrics as null, retaining explicitly partial observations."""
    pending, scheduled, seen = [pid], {pid}, set()
    observed = dict(rss_bytes=0, threads=0, fds=0, processes=0)
    complete = dict.fromkeys(observed, True)
    tree_complete, truncated = True, False
    while pending:
        current = pending.pop()
        status = _read(proc / str(current) / 'status')
        if not status:
            if current == pid:
                return None
            tree_complete = False
            continue
        seen.add(current)
        values = {}
        for line in status.splitlines():
            key, _, value = line.partition(':')
            fields = value.split()
            if key in {'VmRSS', 'Threads'} and fields and fields[0].isdecimal():
                values[key] = int(fields[0])
        for field, metric, factor in (('VmRSS', 'rss_bytes', 1024), ('Threads', 'threads', 1)):
            if field in values:
                observed[metric] += values[field] * factor
            else:
                complete[metric] = False
        try:
            with os.scandir(proc / str(current) / 'fd') as entries:
                count = sum(1 for _ in islice(entries, 65537))
            observed['fds'] += min(count, 65536)
            if count > 65536:
                complete['fds'], truncated = False, True
        except OSError:
            complete['fds'] = False
        # Read every thread's children: subprocesses need not belong to the leader.
        try:
            with os.scandir(proc / str(current) / 'task') as entries:
                tasks = [Path(entry.path) for entry in islice(entries, 513)]
            if not tasks:
                tree_complete = False
            if len(tasks) > 512:
                tree_complete, truncated = False, True
        except OSError:
            tasks, tree_complete = [], False
        for task in tasks[:512]:
            try:
                with (task / 'children').open() as stream:
                    children = stream.read(16385)
            except OSError:
                tree_complete = False
                continue
            if len(children) > 16384:
                tree_complete, truncated = False, True
                # Do not interpret a partially read PID at the boundary.
                children = children[:16384].rsplit(' ', 1)[0]
            for child in children.split():
                if not child.isdecimal():
                    tree_complete = False
                    continue
                child = int(child)
                if child in scheduled:
                    continue
                if len(scheduled) >= 128:
                    tree_complete, truncated = False, True
                    continue
                scheduled.add(child)
                pending.append(child)
    observed['processes'] = len(seen)
    partial = {key: not (available and tree_complete) for key, available in complete.items()}
    return {**{key: None if partial[key] else value for key, value in observed.items()},
            'observed': observed, 'partial': partial,
            'tree_complete': tree_complete, 'truncated': truncated}


def accumulate_sample(row, sample):
    """Do not turn unavailable samples into healthy peaks, or erase partial evidence."""
    row['latest'] = sample
    if sample is None:
        row['unavailable_samples'] += 1
        return
    row['samples'] += 1
    for key in ('rss_bytes', 'threads', 'fds', 'processes'):
        if sample['partial'][key]:
            row['partial_samples'][key] += 1
        if sample[key] is not None:
            previous = row['peak'][key]
            row['peak'][key] = max(previous, sample[key]) if previous is not None else sample[key]


def runner_snapshot():
    return {'cpu_count': os.cpu_count(),
            'memory_total_bytes': next((int(line.split()[1]) * 1024
                for line in _read(Path('/proc/meminfo')).splitlines()
                if line.startswith('MemTotal:')), None),
            'limits': {name: list(resource.getrlimit(getattr(resource, name)))
                       for name in ('RLIMIT_AS', 'RLIMIT_NOFILE', 'RLIMIT_NPROC')
                       if hasattr(resource, name)},
            'cgroup_memory': cgroup_memory()}


def exit_details(code):
    name = None
    if code is not None and code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = 'unknown'
    return {'exit_code': code if code is not None and code >= 0 else None,
            'signal': name, 'returncode': code}


def oom_delta(before, after):
    if not before or not after or len(before) != len(after):
        return None
    deltas = []
    for old, new in zip(before, after):
        if old['events'] is None or new['events'] is None:
            deltas.append(None)
        else:
            deltas.append({key: new['events'][key] - value
                           for key, value in old['events'].items() if key in new['events']})
    return deltas


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
    temporary.replace(path)


def pytest_addoption(parser):
    parser.addoption('--worker-diagnostics-dir', help='Fresh directory for bounded synthetic worker diagnostics')


def pytest_configure(config):
    directory = config.getoption('--worker-diagnostics-dir')
    if not directory:
        return
    config._worker_diagnostics_dir = Path(directory).absolute()
    if hasattr(config, 'workerinput'):
        return
    config._worker_diagnostics_dir.mkdir(parents=True, exist_ok=False)
    plugin = WorkerDiagnostics(config)
    config.pluginmanager.register(plugin, 'worker-resource-observer')


def _phase(item, phase):
    config = item.config
    directory = getattr(config, '_worker_diagnostics_dir', None)
    if directory is not None and hasattr(config, 'workerinput'):
        # Parameter reprs can contain data; identify only the test function.
        _write(directory / (config.workerinput['workerid'] + '-active.json'),
               {'test': item.nodeid.split('[')[0][:500], 'phase': phase,
                'phase_started': monotonic(), 'pid': os.getpid()})


def pytest_runtest_setup(item):
    _phase(item, 'setup')


def pytest_runtest_call(item):
    _phase(item, 'call')


def pytest_runtest_teardown(item):
    _phase(item, 'teardown')


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    try:
        return (yield)
    finally:
        config = item.config
        directory = getattr(config, '_worker_diagnostics_dir', None)
        if directory is not None and hasattr(config, 'workerinput'):
            (directory / (config.workerinput['workerid'] + '-active.json')).unlink(missing_ok=True)


class WorkerDiagnostics:
    def __init__(self, config):
        self.config = config
        self.directory = config._worker_diagnostics_dir
        self.before = runner_snapshot()
        self.workers = {}
        self.crashed = False
        self.lock = threading.Lock()
        self.stop = threading.Event()
        _write(self.directory / 'runner-start.json', self.before)
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node):
        # execnet exposes no public returncode API. Gracefully report unavailable
        # for remote gateways or if this private local-Popen seam changes.
        popen = getattr(getattr(node.gateway, '_io', None), 'popen', None)
        if not node.gateway.spec.popen:
            popen = None
        with self.lock:
            self.workers[node.gateway.id] = {'process': popen, 'samples': 0,
                                             'latest': None, 'unavailable_samples': 0,
                                             'partial_samples': dict.fromkeys(('rss_bytes', 'threads', 'fds', 'processes'), 0),
                                             'peak': dict.fromkeys(('rss_bytes', 'threads', 'fds', 'processes'))}

    def sample(self):
        while not self.stop.is_set():
            with self.lock:
                for row in self.workers.values():
                    process = row['process']
                    if process is None or 'outcome' in row:
                        continue
                    sample = process_tree(process.pid)
                    accumulate_sample(row, sample)
            self.stop.wait(1)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.crashed = self.crashed or error is not None
        with self.lock:
            row = self.workers[node.gateway.id]
            process = row['process']
        code = None
        if process is not None:
            try:
                # A normal worker's execnet process may live until group teardown.
                code = process.wait(timeout=1) if error is not None else process.poll()
            except (OSError, subprocess.TimeoutExpired):
                pass
        active_path = self.directory / (node.gateway.id + '-active.json')
        try:
            active = json.loads(active_path.read_text())
            active['phase_seconds'] = round(monotonic() - active.pop('phase_started'), 3)
        except (OSError, ValueError, KeyError):
            active = None
        after = runner_snapshot()
        with self.lock:
            row['outcome'] = {'worker': node.gateway.id, **exit_details(code),
                'pytest_exitstatus': getattr(node, 'workeroutput', {}).get('exitstatus'),
                'crashed': error is not None, 'active': active, 'samples': row['samples'],
                'latest': row['latest'], 'sampled_peak': row['peak'],
                'partial_samples': row['partial_samples'], 'unavailable_samples': row['unavailable_samples'],
                'cgroup_oom_delta': oom_delta(self.before['cgroup_memory'], after['cgroup_memory']),
                'runner_end': after}
            outcome = row['outcome']
        _write(self.directory / (node.gateway.id + '-exit.json'), outcome)
        if error is not None:
            reporter = self.config.pluginmanager.getplugin('terminalreporter')
            if reporter:
                reporter.write_line('WORKER EXIT DIAGNOSTICS ' + json.dumps(outcome, sort_keys=True))

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session):
        # xdist can otherwise report success when the last test passed and the
        # worker died during session shutdown, with no crash item left to fail.
        if self.crashed and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        self.stop.set()
        self.thread.join(2)
        _write(self.directory / 'runner-end.json', runner_snapshot())
