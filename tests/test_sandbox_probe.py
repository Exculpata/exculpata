"""Synthetic regressions for bounded TestClient environment checks."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading
from time import monotonic
from types import ModuleType, SimpleNamespace

import pytest

from tests import conftest as hooks
from tests import sandbox_probe as probe


@pytest.fixture
def working_primitives(monkeypatch):
    real_portal = probe._portal
    monkeypatch.setattr(probe, '_socketpair', lambda: None)
    monkeypatch.setattr(probe, '_socketpair_wakeup', lambda: None)
    monkeypatch.setattr(probe, '_portal', lambda: None)
    return real_portal


def test_socketpair_permission_message(monkeypatch):
    def denied():
        raise PermissionError(1, 'Operation not permitted')

    monkeypatch.setattr(probe.socket, 'socketpair', denied)
    assert probe.probe_testclient() == (
        'TestClient route tests cannot run in this sandbox: socket.socketpair(): '
        'PermissionError: [Errno 1] Operation not permitted. '
        'Run them in CI or an unrestricted shell.'
    )


def test_wakeup_send_permission_message(monkeypatch):
    class Socket:
        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            raise PermissionError(1, 'Operation not permitted')

    monkeypatch.setattr(probe.socket, 'socketpair', lambda: (Socket(), Socket()))
    assert probe.probe_testclient() == (
        'TestClient route tests cannot run in this sandbox: socketpair send/receive: '
        'PermissionError: [Errno 1] Operation not permitted. '
        'Run them in CI or an unrestricted shell.'
    )


@pytest.mark.parametrize('phase', ['start', 'call', 'stop'])
@pytest.mark.parametrize('failure', ['hang', 'raise'])
def test_portal_failure_is_bounded(monkeypatch, working_primitives, phase, failure):
    monkeypatch.setattr(probe, '_portal', working_primitives)
    release = threading.Event()

    def act(at):
        if at == phase:
            if failure == 'raise':
                raise RuntimeError('synthetic portal failure')
            release.wait(2)

    @contextmanager
    def portal():
        act('start')
        yield SimpleNamespace(call=lambda callback: act('call'))
        act('stop')

    monkeypatch.setattr(probe, 'start_blocking_portal', portal)
    started = monotonic()
    try:
        error = ('RuntimeError: synthetic portal failure' if failure == 'raise'
                 else 'timed out after 0.05 seconds')
        assert probe.probe_testclient(timeout=0.05) == (
            'TestClient route tests cannot run in this sandbox: '
            f'anyio portal start/call/stop: {error}. '
            'Run them in CI or an unrestricted shell.'
        )
        assert monotonic() - started < 1
    finally:
        release.set()


@pytest.mark.parametrize('primitive', ['_socketpair', '_socketpair_wakeup'])
def test_socket_checks_are_bounded(monkeypatch, working_primitives, primitive):
    release = threading.Event()
    monkeypatch.setattr(probe, primitive, lambda: release.wait(2))
    started = monotonic()
    try:
        message = probe.probe_testclient(timeout=0.05)
        assert 'timed out after 0.05 seconds' in message
        assert monotonic() - started < 1
    finally:
        release.set()


def test_real_probe_in_normal_environment():
    try:
        probe._socketpair_wakeup()
    except PermissionError:
        pytest.skip('Normal-environment probe requires socketpair send permission')
    started = monotonic()
    assert probe.probe_testclient() is None
    assert monotonic() - started < 2


def test_hook_skips_exact_message_and_caches(monkeypatch):
    message = probe.skip_message('socketpair send/receive', 'PermissionError: synthetic')
    calls = []
    monkeypatch.setattr(hooks, 'probe_testclient', lambda: calls.append(True) or message)
    module = SimpleNamespace(stash=pytest.Stash())
    module.stash[hooks._module_uses_testclient] = True
    config = SimpleNamespace(stash=pytest.Stash())
    item = SimpleNamespace(getparent=lambda cls: module, config=config)
    for _ in range(2):
        with pytest.raises(pytest.skip.Exception) as exc:
            hooks.pytest_runtest_setup(item)
        assert str(exc.value) == message
    assert calls == [True]


def test_hook_is_lazy_for_non_route_tests(monkeypatch):
    monkeypatch.setattr(hooks, 'probe_testclient', lambda: pytest.fail('probe was not lazy'))
    module = SimpleNamespace(stash=pytest.Stash())
    module.stash[hooks._module_uses_testclient] = False
    hooks.pytest_runtest_setup(SimpleNamespace(getparent=lambda cls: module))


@pytest.mark.parametrize('source', [
    'from fastapi.testclient import TestClient as Client',
    'def test_route():\n    from starlette.testclient import TestClient',
    'import fastapi.testclient as clients',
])
def test_source_detection(tmp_path, source):
    path = tmp_path / 'synthetic.py'
    path.write_text(source)
    assert hooks._uses_testclient(SimpleNamespace(__file__=str(path)))


def test_namespace_detection():
    client = type('TestClient', (), {'__module__': 'starlette.testclient'})
    assert hooks._uses_testclient(SimpleNamespace(Client=client))
    assert not hooks._uses_testclient(ModuleType('synthetic'))


@pytest.mark.parametrize('workers', [0, 2])
@pytest.mark.parametrize('blocked', [False, True])
def test_pytest_hook_integration(tmp_path, workers, blocked):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    (tmp_path / 'conftest.py').write_text(
        'from tests import conftest as hooks\n'
        'pytest_plugins = ["tests.conftest"]\n'
        'def pytest_configure(config):\n'
        f'    hooks.probe_testclient = lambda: {repr(probe.skip_message("synthetic primitive", "blocked")) if blocked else "None"}\n'
    )
    (tmp_path / 'test_route.py').write_text(
        'from fastapi.testclient import TestClient as Client\n'
        'import pytest\n'
        '@pytest.fixture(autouse=True)\n'
        'def fixture():\n'
        + ('    pytest.fail("fixture ran before skip")\n' if blocked else '    pass\n')
        + 'def test_one(): pass\ndef test_two(): pass\n'
    )
    (tmp_path / 'test_plain.py').write_text('def test_plain(): pass\n')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(root / 'src')]))
    command = ['timeout', '25', sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']
    if workers:
        command += ['-n', str(workers)]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    if blocked:
        assert '1 passed, 2 skipped' in result.stdout
        assert probe.skip_message('synthetic primitive', 'blocked') in result.stdout
    else:
        assert '3 passed' in result.stdout
        assert 'cannot run in this sandbox' not in result.stdout


def test_default_portal_timeout_message(monkeypatch, working_primitives):
    monkeypatch.setattr(probe, '_portal', working_primitives)
    release = threading.Event()

    @contextmanager
    def portal():
        release.wait(10)
        yield SimpleNamespace(call=lambda callback: None)

    monkeypatch.setattr(probe, 'start_blocking_portal', portal)
    started = monotonic()
    try:
        assert probe.probe_testclient() == (
            'TestClient route tests cannot run in this sandbox: '
            'anyio portal start/call/stop: timed out after 1.5 seconds. '
            'Run them in CI or an unrestricted shell.'
        )
        assert monotonic() - started < 3
    finally:
        release.set()


def test_successful_probe_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, 'probe_testclient', lambda: calls.append(True))
    module = SimpleNamespace(stash=pytest.Stash())
    module.stash[hooks._module_uses_testclient] = True
    item = SimpleNamespace(getparent=lambda cls: module,
                           config=SimpleNamespace(stash=pytest.Stash()))
    hooks.pytest_runtest_setup(item)
    hooks.pytest_runtest_setup(item)
    assert calls == [True]
