"""Synthetic regressions for bounded TestClient environment checks."""

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
from textwrap import dedent
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
    monkeypatch.delenv('CI', raising=False)
    message = probe.skip_message('socketpair send/receive', 'PermissionError: synthetic')
    calls = []
    monkeypatch.setattr(hooks, 'probe_testclient', lambda: calls.append(True) or message)
    module = SimpleNamespace()
    monkeypatch.setattr(hooks, '_item_uses_testclient', lambda item: True)
    config = SimpleNamespace(stash=pytest.Stash())
    item = SimpleNamespace(getparent=lambda cls: module, config=config)
    for _ in range(2):
        with pytest.raises(pytest.skip.Exception) as exc:
            hooks.pytest_runtest_setup(item)
        assert str(exc.value) == message
    assert calls == [True]


def test_probe_failure_fails_instead_of_skipping_under_ci(monkeypatch):
    monkeypatch.setenv('CI', 'true')
    message = probe.skip_message('socketpair send/receive', 'PermissionError: synthetic')
    monkeypatch.setattr(hooks, 'probe_testclient', lambda: message)
    module = SimpleNamespace()
    monkeypatch.setattr(hooks, '_item_uses_testclient', lambda item: True)
    item = SimpleNamespace(getparent=lambda cls: module,
                           config=SimpleNamespace(stash=pytest.Stash()))
    try:
        with pytest.raises(pytest.fail.Exception) as exc:
            hooks.pytest_runtest_setup(item)
    except pytest.skip.Exception as exc:
        pytest.fail(f'Probe failure skipped instead of failing: {exc}')
    assert str(exc.value) == message


def test_hook_is_lazy_for_non_route_tests(monkeypatch):
    monkeypatch.setattr(hooks, 'probe_testclient', lambda: pytest.fail('probe was not lazy'))
    module = SimpleNamespace()
    monkeypatch.setattr(hooks, '_item_uses_testclient', lambda item: False)
    hooks.pytest_runtest_setup(SimpleNamespace(getparent=lambda cls: module))


@pytest.mark.parametrize('source', [
    'from fastapi.testclient import TestClient as Client',
    'def test_route():\n    from starlette.testclient import TestClient',
    'import fastapi.testclient as clients',
])
def test_source_detection(tmp_path, source):
    path = tmp_path / 'synthetic.py'
    # fixture-write: Create synthetic Python source for detection.
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
    # fixture-write: Isolate the synthetic pytest configuration.
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    # fixture-write: Install repository hooks with a synthetic probe result.
    (tmp_path / 'conftest.py').write_text(
        'from tests import conftest as hooks\n'
        'pytest_plugins = ["tests.conftest"]\n'
        'def pytest_configure(config):\n'
        f'    hooks.probe_testclient = lambda: {repr(probe.skip_message("synthetic primitive", "blocked")) if blocked else "None"}\n'
    )
    # fixture-write: Create synthetic route tests for the subprocess.
    (tmp_path / 'test_route.py').write_text(
        'from fastapi.testclient import TestClient as Client\n'
        'import pytest\n'
        '@pytest.fixture(autouse=True)\n'
        'def fixture():\n'
        '    assert Client is not None\n'
        + ('    pytest.fail("fixture ran before skip")\n' if blocked else '    pass\n')
        + 'def test_one(): pass\ndef test_two(): pass\n'
    )
    # fixture-write: Create a synthetic plain test for the subprocess.
    (tmp_path / 'test_plain.py').write_text('def test_plain(): pass\n')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(root / 'src')]))
    env.pop('CI', None)
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


def _run_external_fixture_tests(tmp_path, ci, module_import):
    root = Path(__file__).resolve().parents[1]
    message = probe.skip_message('synthetic primitive', 'blocked')
    # fixture-write: Isolate the synthetic pytest configuration.
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    # fixture-write: Define an external fixture and force a synthetic probe failure.
    (tmp_path / 'conftest.py').write_text(
        'import pytest\n'
        'from fastapi import FastAPI\n'
        'from fastapi.testclient import TestClient as Client\n'
        'from tests import conftest as hooks\n'
        'pytest_plugins = ["tests.conftest"]\n'
        'def pytest_configure(config):\n'
        f'    hooks.probe_testclient = lambda: {message!r}\n'
        '@pytest.fixture\n'
        'def client():\n'
        '    pytest.fail("external fixture ran before probe failure")\n'
        '    return Client(FastAPI())\n'
        '@pytest.fixture\n'
        'def route_client(client):\n'
        '    return client\n'
    )
    # fixture-write: Create route and plain tests sharing a synthetic module.
    (tmp_path / 'test_mixed.py').write_text(
        ('from fastapi.testclient import TestClient\n' if module_import else '')
        + 'def test_route(route_client):\n'
        '    assert route_client is not None\n'
        'def test_plain():\n'
        '    assert 1 + 1 == 2\n'
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(root / 'src')]))
    env.pop('CI', None)
    if ci:
        env['CI'] = 'true'
    result = subprocess.run(
        ['timeout', '25', sys.executable, '-m', 'pytest', '-v', '-rs', '-p', 'no:cacheprovider'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == (1 if ci else 0), result.stdout + result.stderr
    assert message in result.stdout
    assert 'test_mixed.py::test_route ' + ('ERROR' if ci else 'SKIPPED') in result.stdout
    assert 'test_mixed.py::test_plain PASSED' in result.stdout
    assert '1 passed' in result.stdout
    assert ('1 error' if ci else '1 skipped') in result.stdout
    assert 'external fixture ran before probe failure' not in result.stdout


@pytest.mark.parametrize('ci', [False, True])
def test_external_fixture_hook_integration(tmp_path, ci):
    _run_external_fixture_tests(tmp_path, ci, module_import=False)


@pytest.mark.parametrize('ci', [False, True])
def test_plain_test_in_testclient_module_runs(tmp_path, ci):
    _run_external_fixture_tests(tmp_path, ci, module_import=True)


@pytest.mark.parametrize('ci', [False, True])
@pytest.mark.parametrize('client_name', ['TestClient', 'Client'])
def test_helper_hook_integration(tmp_path, ci, client_name):
    root = Path(__file__).resolve().parents[1]
    message = probe.skip_message('synthetic primitive', 'blocked')
    # fixture-write: Isolate the synthetic pytest configuration.
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    # fixture-write: Install repository hooks with a synthetic probe failure.
    (tmp_path / 'conftest.py').write_text(
        'from tests import conftest as hooks\n'
        'pytest_plugins = ["tests.conftest"]\n'
        'def pytest_configure(config):\n'
        f'    hooks.probe_testclient = lambda: {message!r}\n'
    )
    # fixture-write: Create direct and transitive helper routes alongside a plain test.
    (tmp_path / 'test_helpers.py').write_text(
        'from fastapi import FastAPI\n'
        f'from fastapi.testclient import TestClient as {client_name}\n'
        'def make_client():\n'
        f'    return {client_name}(FastAPI())\n'
        'def second_helper():\n'
        '    return make_client()\n'
        'def test_helper_route():\n'
        '    client = make_client()\n'
        '    assert client is not None\n'
        'def test_transitive_route():\n'
        '    client = second_helper()\n'
        '    assert client is not None\n'
        'def test_plain():\n'
        '    assert "TestClient".startswith("Test")\n'
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(root / 'src')]))
    env.pop('CI', None)
    if ci:
        env['CI'] = 'true'
    result = subprocess.run(
        ['timeout', '25', sys.executable, '-m', 'pytest', '-v', '-rs', '-p', 'no:cacheprovider'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == (1 if ci else 0), result.stdout + result.stderr
    assert message in result.stdout
    for name in ('test_helper_route', 'test_transitive_route'):
        assert f'test_helpers.py::{name} ' + ('ERROR' if ci else 'SKIPPED') in result.stdout
    assert 'test_helpers.py::test_plain PASSED' in result.stdout
    assert '1 passed' in result.stdout
    assert ('2 errors' if ci else '2 skipped') in result.stdout


@pytest.fixture
def synthetic_module(tmp_path, monkeypatch):
    def load(source):
        path = tmp_path / 'synthetic_helpers.py'
        # fixture-write: Provide inspectable synthetic helper functions and classes.
        path.write_text(dedent(source))
        spec = importlib.util.spec_from_file_location('synthetic_helpers', path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return module

    return load


@pytest.mark.parametrize('source, expected', [
    ('''
        from fastapi.testclient import TestClient as Client
        def factory():
            alias = Client
            def helper():
                return alias
            def entry():
                return helper()
            return entry
        entry = factory()
    ''', True),
    ('''
        from fastapi.testclient import TestClient as Client
        def factory():
            alias = Client
            class Helper:
                def make(self):
                    return alias
            def entry():
                return Helper()
            return entry
        entry = factory()
    ''', True),
    ('''
        from fastapi.testclient import TestClient as Client
        class Helper:
            @staticmethod
            def make():
                return Client
        def entry():
            return Helper()
    ''', True),
    ('''
        from fastapi.testclient import TestClient as Client
        class Derived(Client):
            pass
        def entry():
            return Derived
    ''', True),
    ('''
        def helper():
            return entry()
        def entry():
            return helper()
    ''', False),
    ('''
        from fastapi.testclient import TestClient as Client
        def helper():
            return entry(), Client
        def entry():
            return helper()
    ''', True),
    ('''
        from math import sqrt
        def helper():
            return len("TestClient"), sqrt(4)
        def entry():
            return helper()
    ''', False),
])
def test_helper_source_detection(synthetic_module, tmp_path, source, expected):
    module = synthetic_module(source)
    assert hooks._function_uses_testclient(module.entry, tmp_path) is expected


@pytest.mark.parametrize('method', [
    'def setup_method(self):',
    '@classmethod\n    def setup_class(cls):',
    '@staticmethod\n    def helper():',
])
def test_item_class_methods_use_helpers(synthetic_module, tmp_path, method):
    module = synthetic_module(
        'from fastapi.testclient import TestClient as Client\n'
        'def make_client():\n'
        '    return Client\n'
        'class Base:\n'
        f'    {method}\n'
        '        return make_client()\n'
        'class TestRoutes(Base):\n'
        '    def test_route(self):\n'
        '        assert True\n'
    )
    item = SimpleNamespace(
        obj=module.TestRoutes.test_route, cls=module.TestRoutes,
        config=SimpleNamespace(rootpath=tmp_path), fixturenames=[],
    )
    assert hooks._item_uses_testclient(item)


def test_fixture_helper_detection(synthetic_module, tmp_path):
    module = synthetic_module('''
        from fastapi.testclient import TestClient as Client
        def make_client():
            return Client
        def client_fixture():
            return make_client()
        def entry(client_fixture):
            assert client_fixture is not None
    ''')
    item = SimpleNamespace(
        obj=module.entry, cls=None, config=SimpleNamespace(rootpath=tmp_path),
        fixturenames=['client_fixture'],
        _fixtureinfo=SimpleNamespace(name2fixturedefs={
            'client_fixture': [SimpleNamespace(func=module.client_fixture)],
        }),
    )
    assert hooks._item_uses_testclient(item)


def test_helper_detection_is_cached_and_depth_bounded(synthetic_module, tmp_path, monkeypatch):
    module = synthetic_module(
        'from fastapi.testclient import TestClient as Client\n'
        + ''.join(f'def helper_{i}(): return helper_{i + 1}()\n' for i in range(8))
        + 'def helper_8(): return Client\n'
    )
    inspected = []
    getsource = hooks.inspect.getsource

    def record_source(function):
        inspected.append(function.__name__)
        return getsource(function)

    monkeypatch.setattr(hooks.inspect, 'getsource', record_source)
    for _ in range(2):
        assert hooks._function_uses_testclient(module.helper_0, tmp_path) is False
    assert inspected == [f'helper_{i}' for i in range(7)]
    assert hooks._function_uses_testclient(module.helper_2, tmp_path) is True


@pytest.mark.parametrize('location', ['outside', 'site-packages', 'stdlib'])
def test_helper_detection_excludes_external_source(synthetic_module, tmp_path, monkeypatch, location):
    module = synthetic_module('''
        from fastapi.testclient import TestClient as Client
        def helper():
            return Client
        def entry():
            return helper()
    ''')
    paths = {
        'outside': tmp_path.parent / 'external_helpers.py',
        'site-packages': tmp_path / 'site-packages' / 'external_helpers.py',
        'stdlib': hooks._stdlib_roots[0] / 'external_helpers.py',
    }
    getsourcefile = hooks.inspect.getsourcefile

    def sourcefile(function):
        return str(paths[location]) if function is module.helper else getsourcefile(function)

    monkeypatch.setattr(hooks.inspect, 'getsourcefile', sourcefile)
    assert hooks._function_uses_testclient(module.entry, tmp_path) is False


@pytest.mark.parametrize('invalid', [False, True])
def test_unavailable_source_keeps_fail_closed_detection(synthetic_module, tmp_path, monkeypatch, invalid):
    module = synthetic_module('''
        def helper():
            return True
        def entry():
            return helper()
    ''')
    getsource = hooks.inspect.getsource

    def source(function):
        if function is module.helper:
            if invalid:
                return 'def invalid('
            raise OSError('synthetic unavailable source')
        return getsource(function)

    monkeypatch.setattr(hooks.inspect, 'getsource', source)
    assert hooks._function_uses_testclient(module.helper, tmp_path) is None
    assert hooks._function_uses_testclient(module.entry, tmp_path) is False
    item = SimpleNamespace(obj=module.helper, config=SimpleNamespace(rootpath=tmp_path))
    assert hooks._item_uses_testclient(item)


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
    module = SimpleNamespace()
    monkeypatch.setattr(hooks, '_item_uses_testclient', lambda item: True)
    item = SimpleNamespace(getparent=lambda cls: module,
                           config=SimpleNamespace(stash=pytest.Stash()))
    hooks.pytest_runtest_setup(item)
    hooks.pytest_runtest_setup(item)
    assert calls == [True]
