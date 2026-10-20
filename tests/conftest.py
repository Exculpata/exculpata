"""Pytest process defaults for the application suite."""

from __future__ import annotations

import ast
import os

import pytest

from tests.sandbox_probe import probe_testclient

# Reserve 0 is the suite's disk escape. A real node must set this switch itself.
os.environ.setdefault("CASE_INTELLIGENCE_TEST_MODE", "1")

pytest_plugins = ['tests.worker_diagnostics']


_probe_result = pytest.StashKey[str | None]()
_module_uses_testclient = pytest.StashKey[bool]()
_worker_failures = pytest.StashKey[set[str]]()


def _uses_testclient(module):
    for value in vars(module).values():
        if isinstance(value, type) and any(
            cls.__name__ == 'TestClient' and cls.__module__ == 'starlette.testclient'
            for cls in value.__mro__
        ):
            return True
    path = getattr(module, '__file__', None)
    if path is None:
        return False
    with open(path, encoding='utf-8') as source:
        tree = ast.parse(source.read())
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


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    module = item.getparent(pytest.Module)
    if module is None:
        return
    if _module_uses_testclient not in module.stash:
        module.stash[_module_uses_testclient] = _uses_testclient(item.module)
    if not module.stash[_module_uses_testclient]:
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
