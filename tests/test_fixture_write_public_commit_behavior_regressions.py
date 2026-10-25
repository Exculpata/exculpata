"""Actual public-API and explicit-context transaction termination evidence."""
from tests.fixture_probe import fixture_probe
import sqlite3
from threading import Event, RLock, Thread
from types import SimpleNamespace

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


def test_workspace_audit_method_commits_fixture_before_exception_and_requires_rejection(tmp_path):
    path = tmp_path / 'synthetic-public-commit.sqlite'
    store = WorkspaceStore(path)
    bench = SimpleNamespace(workspace=store)
    source = '''with fixture_write(bench.workspace) as connection:
    connection.execute("INSERT INTO synthetic_fixture VALUES ('escaped fixture')")
    bench.workspace.append_audit_event(actor_principal_id=None, session_id=None,
        matter_id=None, request_id='synthetic-request', action='synthetic.fixture', outcome='success')
    raise ValueError('synthetic body failure')
'''
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        with pytest.raises(ValueError, match='synthetic body failure'):
            error, _ = fixture_probe(source, bench=bench, fixture_write=fixture_write)
            if error is not None:
                raise error
        # The real public API commits both its audit and the prior fixture SQL.
        # Keep this behavior evidence explicit: the inventory must reject source.
        with sqlite3.connect(path) as independent:
            assert independent.execute('SELECT value FROM synthetic_fixture').fetchall() == [('escaped fixture',)]
            assert independent.execute("SELECT COUNT(*) FROM workbench_audit_event WHERE request_id='synthetic-request'").fetchone()[0] == 1
        assert check_fixture_writes(source)
    finally:
        store.close()


@pytest.mark.parametrize('style', ['direct', 'saved', 'unbound'])
@pytest.mark.parametrize('rollback', [False, True])
def test_explicit_connection_exit_ends_actual_worker_transaction_without_lock(tmp_path, style, rollback):
    path = tmp_path / 'synthetic-worker-exit.sqlite'
    raw = sqlite3.connect(path, check_same_thread=False)
    raw.execute('CREATE TABLE synthetic_fixture(value TEXT)')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw, _lock=RLock()))
    ready, resume = Event(), Event()
    observations, errors = [], []
    def worker():
        try:
            with bench.workspace._lock:
                # fixture-write: Deliberately create worker-owned pending data for the raw termination negative regression.
                raw.execute("INSERT INTO synthetic_fixture VALUES ('worker pending')")
                ready.set()
                assert resume.wait(5)
                observations.append(raw.in_transaction)
        except BaseException as error:
            errors.append(error)
            ready.set()
    worker_thread = Thread(target=worker)
    worker_thread.start()
    args = 'ValueError, ValueError("synthetic failure"), None' if rollback else 'None, None, None'
    source = {
        'direct': f'bench.workspace.connection.__exit__({args})',
        'saved': f'finish = bench.workspace.connection.__exit__\nfinish({args})',
        'unbound': f'import sqlite3\nsqlite3.Connection.__exit__(bench.workspace.connection, {args})',
    }[style]
    try:
        assert ready.wait(5)
        assert not errors
        error, _ = fixture_probe(source, bench=bench)
        if error is not None:
            raise error
        with sqlite3.connect(path) as independent:
            rows = independent.execute('SELECT value FROM synthetic_fixture').fetchall()
        assert rows == ([] if rollback else [('worker pending',)])
        resume.set()
        worker_thread.join(5)
        assert not worker_thread.is_alive()
        assert not errors and observations == [False]
        assert check_fixture_writes(source)
    finally:
        resume.set()
        worker_thread.join(5)
        raw.close()
