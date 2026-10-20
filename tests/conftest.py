"""Pytest process defaults for the application suite."""

from __future__ import annotations

import ast
from functools import cache
import inspect
import os
from pathlib import Path
import sysconfig
import textwrap

import pytest

from tests.sandbox_probe import probe_testclient

# Reserve 0 is the suite's disk escape. A real node must set this switch itself.
os.environ.setdefault("CASE_INTELLIGENCE_TEST_MODE", "1")

pytest_plugins = ['tests.worker_diagnostics']


_probe_result = pytest.StashKey[str | None]()
_worker_failures = pytest.StashKey[set[str]]()
_repository_root = Path(__file__).resolve().parents[1]
_stdlib_roots = tuple(
    Path(sysconfig.get_path(name)).resolve() for name in ('stdlib', 'platstdlib')
)
_helper_depth_limit = 6


def _namespace_uses_testclient(namespace):
    for value in namespace.values():
        if isinstance(value, type) and any(
            cls.__name__ == 'TestClient' and cls.__module__ == 'starlette.testclient'
            for cls in value.__mro__
        ):
            return True
    return False


def _uses_testclient(module):
    if _namespace_uses_testclient(vars(module)):
        return True
    path = getattr(module, '__file__', None)
    if path is None:
        return False
    try:
        with open(path, encoding='utf-8') as source:
            tree = ast.parse(source.read())
    except (OSError, SyntaxError, UnicodeError):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            'fastapi.testclient', 'starlette.testclient'
        } and any(alias.name in {'TestClient', '*'} for alias in node.names):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name in {'fastapi.testclient', 'starlette.testclient'}
            for alias in node.names
        ):
            return True
    return False


@cache
def _callable_testclient_references(function):
    try:
        function = inspect.unwrap(function)
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, ValueError, SyntaxError, UnicodeError):
        return None
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        if isinstance(node, ast.Attribute) and node.attr == 'TestClient':
            return True, ()
    if inspect.isclass(function):
        module = inspect.getmodule(function)
        namespace = dict(vars(module)) if module is not None else {}
        namespace.update(vars(function))
    else:
        namespace = dict(getattr(function, '__globals__', {}))
        if inspect.isfunction(function) or inspect.ismethod(function):
            namespace.update(inspect.getclosurevars(function).nonlocals)
    references = {name: namespace[name] for name in names if name in namespace}
    direct = 'TestClient' in names or _namespace_uses_testclient(references)
    callables = [value for value in references.values()
                 if inspect.isfunction(value) or inspect.isclass(value)]
    if inspect.isclass(function):
        for _, method in inspect.getmembers_static(function):
            if isinstance(method, (classmethod, staticmethod)):
                method = method.__func__
            if inspect.isfunction(method):
                callables.append(method)
    return direct, tuple(callables)


@cache
def _local_callable(function, rootdir):
    try:
        path = inspect.getsourcefile(inspect.unwrap(function))
    except (TypeError, ValueError):
        return False
    if path is None:
        return False
    path = Path(path).resolve()
    if {'site-packages', 'dist-packages'}.intersection(path.parts):
        return False
    if any(path.is_relative_to(root) for root in _stdlib_roots):
        return False
    return path.is_relative_to(_repository_root) or path.is_relative_to(rootdir)


@cache
def _function_uses_testclient(function, rootdir=_repository_root):
    def visit(current, depth, ancestors):
        if current in ancestors:
            return False
        references = _callable_testclient_references(current)
        if references is None:
            return None
        direct, callables = references
        if direct:
            return True
        if depth == 0:
            return False
        ancestors = ancestors | {current}
        return any(
            visit(target, depth - 1, ancestors) is True
            for target in callables if _local_callable(target, rootdir)
        )

    return visit(function, _helper_depth_limit, set())


def _item_uses_testclient(item):
    rootdir = item.config.rootpath
    if _function_uses_testclient(item.obj, rootdir) is not False:
        return True
    if item.cls is not None and _function_uses_testclient(item.cls, rootdir) is not False:
        return True
    for name in item.fixturenames:
        for fixture in item._fixtureinfo.name2fixturedefs.get(name, ()):
            uses_testclient = _function_uses_testclient(fixture.func, rootdir)
            if uses_testclient is None:
                module = inspect.getmodule(inspect.unwrap(fixture.func))
                uses_testclient = module is None or _uses_testclient(module)
            if uses_testclient:
                return True
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if item.getparent(pytest.Module) is None or not _item_uses_testclient(item):
        return
    if _probe_result not in item.config.stash:
        item.config.stash[_probe_result] = probe_testclient()
    failure = item.config.stash[_probe_result]
    if failure is not None:
        if os.environ.get('CI', '').lower() not in {'', '0', 'false'}:
            pytest.fail(failure, pytrace=False)
        pytest.skip(failure)


def pytest_sessionfinish(session):
    failure = session.config.stash.get(_probe_result, None)
    if failure is not None and hasattr(session.config, 'workeroutput'):
        session.config.workeroutput['testclient_sandbox_failure'] = failure


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    failure = getattr(node, 'workeroutput', {}).get('testclient_sandbox_failure')
    if failure is not None:
        if _worker_failures not in node.config.stash:
            node.config.stash[_worker_failures] = set()
        node.config.stash[_worker_failures].add(failure)


def pytest_terminal_summary(terminalreporter):
    failures = set(terminalreporter.config.stash.get(_worker_failures, set()))
    failure = terminalreporter.config.stash.get(_probe_result, None)
    if failure is not None:
        failures.add(failure)
    for message in sorted(failures):
        terminalreporter.write_line(message)
