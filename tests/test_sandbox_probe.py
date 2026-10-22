"""Synthetic regressions for bounded TestClient environment checks."""

from contextlib import contextmanager
import ast
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
        module = ModuleType('synthetic_helpers')
        module.__file__ = str(path)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(path.read_text(), str(path), 'exec'), vars(module))
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
        assert hooks._function_uses_testclient(module.helper_0, tmp_path) is True
    assert inspected == [f'helper_{i}' for i in range(8)]
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
    assert hooks._scan_testclient(module.helper, tmp_path, fixture=True) is False
    module.Client = type('TestClient', (), {'__module__': 'starlette.testclient'})
    assert hooks._scan_testclient(module.helper, tmp_path, fixture=True) is True


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


def _run_detection_tests(tmp_path, ci, fixtures, tests, blocked, passed):
    root = Path(__file__).resolve().parents[1]
    message = probe.skip_message('synthetic primitive', 'blocked')
    # fixture-write: Isolate the synthetic pytest configuration.
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    # fixture-write: Install synthetic fixtures and a deterministic probe failure.
    (tmp_path / 'conftest.py').write_text(
        'import pytest\n'
        'from tests import conftest as hooks\n'
        'pytest_plugins = ["tests.conftest"]\n'
        'def pytest_configure(config):\n'
        f'    hooks.probe_testclient = lambda: {message!r}\n'
        + dedent(fixtures)
    )
    # fixture-write: Supply synthetic route and plain cases for hook classification.
    (tmp_path / 'test_detection.py').write_text(dedent(tests))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root), str(root / 'src')]))
    env.pop('CI', None)
    if ci:
        env['CI'] = 'true'
    result = subprocess.run(
        ['timeout', '25', sys.executable, '-m', 'pytest', '-v', '-rs', '-p', 'no:cacheprovider'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (1 if ci else 0), output
    assert message in output, output
    for name in blocked:
        assert f'test_detection.py::{name} ' + ('ERROR' if ci else 'SKIPPED') in output, output
    for name in passed:
        assert f'test_detection.py::{name} PASSED' in output, output
    assert f'{len(passed)} passed' in output, output
    assert f'{len(blocked)} ' + ('errors' if ci else 'skipped') in output, output


@pytest.mark.parametrize('ci', [False, True])
def test_getfixturevalue_hook_integration(tmp_path, ci):
    _run_detection_tests(tmp_path, ci, '''
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        @pytest.fixture
        def client_fixture():
            with TestClient(FastAPI()) as client:
                yield client
        @pytest.fixture
        def plain_fixture():
            return 42
        @pytest.fixture
        def indirect_fixture(request):
            return request.getfixturevalue('client_fixture')
        @pytest.fixture
        def transitive_fixture(indirect_fixture):
            return indirect_fixture
    ''', '''
        import pytest
        def test_dynamic(request):
            assert request.getfixturevalue('client_fixture') is not None
        @pytest.mark.parametrize('mode', ('route', 'plain'))
        def test_selected(request, mode):
            assert request.getfixturevalue('client_fixture' if mode == 'route' else 'plain_fixture') is not None
        def helper(request):
            return request.getfixturevalue('transitive_fixture')
        def test_helper(request):
            assert helper(request) is not None
        def test_plain():
            assert 'TestClient'.startswith('Test')
    ''', ['test_dynamic', 'test_selected[route]', 'test_helper'],
        ['test_selected[plain]', 'test_plain'])


@pytest.mark.parametrize('ci', [False, True])
def test_module_object_hook_integration(tmp_path, ci):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    # fixture-write: Provide a synthetic dynamically loaded TestClient helper.
    (scripts / 'helper_script.py').write_text(
        'from fastapi import FastAPI\n'
        'from fastapi.testclient import TestClient\n'
        'def make(): return TestClient(FastAPI())\n'
        'def noop(): return 42\n'
    )
    _run_detection_tests(tmp_path, ci, '''
        import importlib.util
        from pathlib import Path
        ROOT = Path(__file__).parent
        def load_script(name, filename):
            spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        @pytest.fixture
        def script_fixture():
            return load_script('fixture_helper', 'helper_script.py')
    ''', '''
        import importlib.util
        from pathlib import Path
        import pytest
        spec = importlib.util.spec_from_file_location('module_helper', Path(__file__).parent / 'scripts' / 'helper_script.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        def test_module():
            assert mod.make() is not None
        def test_fixture(script_fixture):
            assert script_fixture.make() is not None
        def test_plain_fixture(script_fixture):
            assert script_fixture.noop() == 42
        def test_inspect_only():
            import inspect
            assert 'TestClient' in inspect.getsource(mod.make)
        @pytest.mark.parametrize('mode', ('route', 'plain'))
        def test_selected_module(mode):
            if mode == 'route':
                assert mod.make() is not None
            else:
                assert mod.noop() == 42
        def test_plain():
            assert 'TestClient'.startswith('Test')
    ''', ['test_module', 'test_fixture', 'test_selected_module[route]'],
        ['test_plain_fixture', 'test_inspect_only', 'test_selected_module[plain]', 'test_plain'])


@pytest.mark.parametrize('expression, params, expected', [
    ("'plain_fixture'", {}, 'plain_fixture'),
    ("'client_fixture' if mode == 'route' else 'plain_fixture'", {'mode': 'plain'}, 'plain_fixture'),
    ("'client_fixture' if mode != 'plain' and not disabled else 'plain_fixture'",
     {'mode': 'route', 'disabled': False}, 'client_fixture'),
    ("'client_fixture' if 0 < count <= +2 or ~flag == -1 else 'plain_fixture'",
     {'count': 1, 'flag': 1}, 'client_fixture'),
    ("name", {'name': 'plain_fixture'}, 'plain_fixture'),
    ("unknown", {}, None),
    ("str(mode)", {'mode': 'plain_fixture'}, None),
    ("mode.name", {'mode': SimpleNamespace(name='plain_fixture')}, None),
    ("'plain_' + 'fixture'", {}, None),
    ("42", {}, None),
])
def test_fixture_name_expression(expression, params, expected):
    tree = ast.parse(expression, mode='eval')
    assert hooks._fixture_name_expression(tree.body, params) == expected


@pytest.mark.parametrize('expression', ["'missing_fixture'", 'unknown', 'str(42)'])
def test_unresolved_fixture_lookup_fails_closed(synthetic_module, tmp_path, expression):
    module = synthetic_module(f'''
        def entry(request):
            return request.getfixturevalue({expression})
    ''')
    resolved = []

    def getfixturedefs(name, item):
        resolved.append(name)
        return None

    item = SimpleNamespace(
        obj=module.entry, cls=None, config=SimpleNamespace(rootpath=tmp_path),
        fixturenames=[], session=SimpleNamespace(
            _fixturemanager=SimpleNamespace(getfixturedefs=getfixturedefs)),
    )
    assert hooks._item_uses_testclient(item)
    assert resolved == (['missing_fixture'] if expression == "'missing_fixture'" else [])


@pytest.mark.parametrize('loader', [
    "importlib.util.spec_from_file_location('synthetic', 'missing.py')",
    "importlib.import_module('missing')",
    "runpy.run_path('missing.py')",
    "runpy.run_module('missing')",
])
def test_unresolved_dynamic_module_fails_closed(synthetic_module, tmp_path, loader):
    module = synthetic_module(f'''
        import importlib.util
        import runpy
        def loader_only():
            return {loader}
        def entry():
            loaded = {loader}
            return loaded.missing()
    ''')
    assert hooks._function_uses_testclient(module.loader_only, tmp_path) is False
    assert hooks._function_uses_testclient(module.entry, tmp_path) is True


def test_dynamic_plain_module_stays_plain(synthetic_module, tmp_path):
    # fixture-write: Supply a plain module to distinguish loading from TestClient use.
    (tmp_path / 'plain_helper.py').write_text('def noop(): return "TestClient"\n')
    module = synthetic_module('''
        import importlib.util
        def load_script(filename):
            return importlib.util.spec_from_file_location('synthetic', filename)
        def entry():
            return load_script('plain_helper.py')
        class Record:
            def label(self):
                return 'plain'
        def build_record():
            load_script('plain_helper.py')
            return Record()
        def call_record():
            record = build_record()
            return record.label()
    ''')
    assert hooks._function_uses_testclient(module.entry, tmp_path) is False
    assert hooks._function_uses_testclient(module.call_record, tmp_path) is False


def test_dynamic_fixture_cycles_are_bounded(synthetic_module, tmp_path):
    module = synthetic_module('''
        def first(request):
            return request.getfixturevalue('second')
        def second(first):
            return first
        def entry(request):
            return request.getfixturevalue('first')
    ''')
    definitions = {
        'first': [SimpleNamespace(func=module.first, argnames=('request',))],
        'second': [SimpleNamespace(func=module.second, argnames=('first',))],
    }
    item = SimpleNamespace(
        obj=module.entry, cls=None, config=SimpleNamespace(rootpath=tmp_path),
        fixturenames=[], session=SimpleNamespace(
            _fixturemanager=SimpleNamespace(getfixturedefs=lambda name, item: definitions.get(name))),
    )
    assert hooks._item_uses_testclient(item) is False


@pytest.mark.parametrize('extra', [0, 1])
def test_plain_helpers_at_depth_boundary(synthetic_module, tmp_path, extra):
    depth = hooks._helper_depth_limit + extra
    module = synthetic_module(
        'from fastapi.testclient import TestClient as Client\n'
        + ''.join(f'def helper_{i}(): return helper_{i + 1}()\n' for i in range(depth))
        + f'def helper_{depth}(): return True\n'
    )
    assert hooks._function_uses_testclient(module.helper_0, tmp_path) is bool(extra)


@pytest.mark.parametrize('client_at_end', [False, True])
def test_import_closure_controls_deep_helper_leaves(
    synthetic_module, tmp_path, monkeypatch, client_at_end,
):
    depth = hooks._helper_depth_limit + 3
    for index in reversed(range(depth)):
        name = f'synthetic_chain_{index}'
        if index < depth - 1:
            source = (f'from synthetic_chain_{index + 1} import helper as following\n'
                      'def helper(): return following()\n')
        elif client_at_end:
            source = ('from fastapi.testclient import TestClient as Client\n'
                      'def helper(): return Client\n')
        else:
            source = 'def helper(): return "testclient"\n'
        path = tmp_path / f'{name}.py'
        # fixture-write: Build a synthetic local import chain beyond the depth limit.
        path.write_text(source)
        module = ModuleType(name)
        module.__file__ = str(path)
        monkeypatch.setitem(sys.modules, name, module)
        exec(compile(source, str(path), 'exec'), vars(module))
    entry = synthetic_module('''
        from synthetic_chain_0 import helper
        def entry():
            return helper()
    ''')
    assert hooks._function_uses_testclient(entry.entry, tmp_path) is client_at_end
    info = hooks._module_imports.cache_info()
    assert hooks._module_is_testclient_free(tmp_path / 'synthetic_chain_0.py', tmp_path) is not client_at_end
    assert hooks._module_imports.cache_info().misses == info.misses


def test_dynamic_module_filename_in_followed_helper(synthetic_module, tmp_path):
    # fixture-write: Supply a plain script whose filename comes from a helper.
    (tmp_path / 'plain_helper.py').write_text('def noop(): return "TestClient"\n')
    module = synthetic_module('''
        import importlib.util
        def filename():
            return 'plain_helper.py'
        def entry():
            return importlib.util.spec_from_file_location('synthetic', filename())
    ''')
    assert hooks._function_uses_testclient(module.entry, tmp_path) is False


@pytest.mark.parametrize('ending, expected', [
    ('from . import first\ndef helper(): return "TestClient"\n', True),
    ('from . import first\nfrom fastapi.testclient import TestClient as Client\n', False),
    ('from . import missing\n', False),
    ('from .missing import helper\n', False),
    ('from importlib import import_module as load\ndef helper(name): return load(name)\n', False),
    ('def helper(source): return exec(source)\n', False),
    ('def helper(request): return request.getfixturevalue("client")\n', False),
])
def test_module_import_proof_handles_cycles_and_unknowns(tmp_path, ending, expected):
    package = tmp_path / 'synthetic_package'
    package.mkdir()
    for name, source in {
        '__init__': '',
        'first': 'from .second import helper\n',
        'second': ending,
    }.items():
        # fixture-write: Model safe and unsafe cyclic local import closures.
        (package / f'{name}.py').write_text(source)
    assert hooks._module_is_testclient_free(package / 'first.py', tmp_path) is expected


@pytest.mark.parametrize('initializer, expected', [
    ('LABEL = "testclient"\n', True),
    ('from fastapi.testclient import TestClient as LABEL\n', False),
])
def test_relative_package_attributes_remain_in_import_closure(tmp_path, initializer, expected):
    package = tmp_path / 'synthetic_package'
    package.mkdir()
    # fixture-write: Distinguish an ordinary package constant from a TestClient alias.
    (package / '__init__.py').write_text(initializer)
    # fixture-write: Exercise a relative import of an attribute rather than a module.
    (package / 'helper.py').write_text('from . import LABEL\ndef helper(): return LABEL\n')
    assert hooks._module_is_testclient_free(package / 'helper.py', tmp_path) is expected


@pytest.mark.parametrize('expression', [
    "ROOT / 'scripts' / ('plain_' + 'helper.py')",
    "Path(__file__).resolve().parents[0] / 'scripts' / 'plain_helper.py'",
    "Path(__file__).parent / f'scripts/{filename}'",
])
def test_dynamic_paths_follow_specific_reexport(synthetic_module, tmp_path, expression):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    # fixture-write: A reexport must resolve without importing or executing its module.
    (scripts / 'plain_helper.py').write_text(
        'from synthetic_navigation import noop\n'
        'raise AssertionError("detector executed a module")\n'
    )
    # fixture-write: Unrelated TestClient use must not flag the selected plain helper.
    (scripts / 'synthetic_navigation.py').write_text(
        'from fastapi.testclient import TestClient as Client\n'
        'def noop(): return 42\n'
        'def route(): return Client\n'
    )
    module = synthetic_module(f'''
        import importlib.util
        from pathlib import Path
        ROOT = Path(__file__).parent
        def entry():
            filename = 'plain_helper.py'
            spec = importlib.util.spec_from_file_location('synthetic', {expression})
            loaded = importlib.util.module_from_spec(spec)
            return loaded.noop()
    ''')
    assert hooks._function_uses_testclient(module.entry, tmp_path) is False


def test_dynamic_loader_ignores_unrelated_keyword_unpacking(synthetic_module, tmp_path):
    module = synthetic_module('''
        from fastapi.testclient import TestClient as Client
        def build(**kwargs):
            return object()
        def entry(options):
            record = build(**options)
            return record.label(), Client
        def plain(options):
            record = build(**options)
            return record.label()
    ''')
    assert hooks._function_uses_testclient(module.plain, tmp_path) is False
    assert hooks._function_uses_testclient(module.entry, tmp_path) is True


def test_dynamic_paths_preserve_parameter_types(synthetic_module, tmp_path):
    for filename, source in {
        'True.py': 'def main(): return 42\n',
        '1.py': 'from fastapi.testclient import TestClient\ndef main(): return TestClient\n',
    }.items():
        # fixture-write: Equal parameter values with distinct types select different modules.
        (tmp_path / filename).write_text(source)
    module = synthetic_module('''
        import importlib.util
        from pathlib import Path
        ROOT = Path(__file__).parent
        def entry(mode):
            spec = importlib.util.spec_from_file_location('synthetic', ROOT / f'{mode}.py')
            loaded = importlib.util.module_from_spec(spec)
            return loaded.main()
    ''')
    for mode, expected in ((True, False), (1, True)):
        item = SimpleNamespace(
            obj=module.entry, cls=None, config=SimpleNamespace(rootpath=tmp_path),
            fixturenames=[], callspec=SimpleNamespace(params={'mode': mode}),
        )
        assert hooks._item_uses_testclient(item) is expected


def test_shared_helpers_use_shortest_path_before_depth_limit(synthetic_module, tmp_path):
    module = synthetic_module(
        ''.join(f'def helper_{i}(): return helper_{i + 1}()\n' for i in range(8))
        + 'def helper_8(): return True\n'
        + 'def entry(): return helper_0(), helper_4()\n'
    )
    assert hooks._function_uses_testclient(module.entry, tmp_path) is False


def test_parameter_source_cache_preserves_value_types(synthetic_module, tmp_path):
    module = synthetic_module('''
        from fastapi.testclient import TestClient as Client
        def entry(mode):
            if mode is True:
                return Client
            return 'plain'
    ''')
    for mode, expected in ((True, True), (1, False)):
        item = SimpleNamespace(
            obj=module.entry, cls=None, config=SimpleNamespace(rootpath=tmp_path),
            fixturenames=[], callspec=SimpleNamespace(params={'mode': mode}),
        )
        assert hooks._item_uses_testclient(item) is expected


def test_annotated_class_ignores_synthetic_helpers(synthetic_module, tmp_path):
    module = synthetic_module('''
        class Record:
            label: str
        def entry():
            return Record()
    ''')
    assert hooks._function_uses_testclient(module.entry, tmp_path) is False
    annotate = getattr(module.Record, '__annotate__', None)
    if annotate is not None:
        assert hooks._local_callable(annotate, tmp_path) is False


def test_parameter_variants_share_source_analysis(synthetic_module, tmp_path, monkeypatch):
    module = synthetic_module('''
        from fastapi.testclient import TestClient as Client
        def route():
            return Client
        def entry(mode):
            if mode == 'route':
                return route()
            return 'plain'
    ''')
    inspected = []
    getsource = hooks.inspect.getsource

    def record_source(function):
        inspected.append(function)
        return getsource(function)

    monkeypatch.setattr(hooks.inspect, 'getsource', record_source)
    before = hooks._selected_references.cache_info().misses
    for mode in ('route', 'plain', 'another', 'route'):
        item = SimpleNamespace(
            obj=module.entry, cls=None, config=SimpleNamespace(rootpath=tmp_path),
            fixturenames=[], callspec=SimpleNamespace(params={'mode': mode}),
        )
        assert hooks._item_uses_testclient(item) is (mode == 'route')
    assert inspected == [module.entry, module.route]
    assert hooks._selected_references.cache_info().misses - before == 3
