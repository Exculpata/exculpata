"""Pytest hooks for replaying service-independent setup without a database."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


SENTINEL = 'ci-parity-replay-stub-dsn'
STUBS = frozenset({'postgres_base_dsn', 'postgres_test_dsn'})
REPORT_ENV = 'CI_PARITY_REPLAY_REPORT'
STATE = pytest.StashKey[dict]()


def pytest_configure(config):
    config.stash[STATE] = {
        'counts': dict(stubbed_setup_ok=0, body_skipped=0, ran=0, passed=0,
                       skipped=0, unreplayable=0, failed=0),
        'events': [],
    }


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname in STUBS:
        fixturedef.cached_result = (SENTINEL, fixturedef.cache_key(request), None)
        return SENTINEL


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    if STUBS.intersection(item.fixturenames):
        item.stash[STATE] = True
        pytest.skip('service-dependent body not replayed')


def database_exception(error):
    """Only database exception provenance or the fixed sentinel grants a gap."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        module = type(current).__module__.split('.')[0]
        missing = isinstance(current, ModuleNotFoundError) and (current.name or '').split('.')[0] == 'psycopg'
        if module == 'psycopg' or missing or SENTINEL in str(current):
            return current
        pending.extend(cause for cause in (current.__cause__, current.__context__) if cause is not None)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return None


def record_failure(config, nodeid, phase, error):
    database = database_exception(error)
    kind = 'unreplayable' if database is not None else 'failed'
    selected = database if database is not None else error
    state = config.stash[STATE]
    state['counts'][kind] += 1
    state['events'].append(dict(kind=kind, test=nodeid, phase=phase,
                                reason=type(selected).__name__,
                                message=(str(selected).splitlines() or [''])[0][:120]))
    return kind


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    state = item.config.stash[STATE]
    counts = state['counts']
    body_skipped = report.when == 'call' and item.stash.get(STATE, False)
    if report.when == 'setup' and report.passed and STUBS.intersection(item.fixturenames):
        counts['stubbed_setup_ok'] += 1
    if body_skipped:
        counts['body_skipped'] += 1
        state['events'].append(dict(kind='body_skipped', test=item.nodeid, phase='call'))
    elif report.when == 'call':
        counts['ran'] += 1
        if report.passed:
            counts['passed'] += 1
    if report.skipped and not body_skipped:
        counts['skipped'] += 1
    if report.failed:
        record_failure(item.config, item.nodeid, report.when, call.excinfo.value)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    report = yield
    if report.failed:
        call = getattr(report, 'call', None)
        error = call.excinfo.value if call and call.excinfo else RuntimeError(str(report.longrepr))
        kind = record_failure(collector.config, collector.nodeid, 'collect', error)
        if kind == 'unreplayable':
            # Collection gaps must not abort independent modules' replay.
            report.outcome = 'skipped'
            report.longrepr = (str(collector.path), 0, 'database import unavailable during replay')
    return report


def pytest_sessionfinish(session):
    path = os.environ.get(REPORT_ENV)
    if path:
        Path(path).write_text(json.dumps(session.config.stash[STATE]))
