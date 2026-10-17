"""Fixture mutations must never join or commit a concurrent worker transaction."""
import ast
import inspect
import sqlite3
from types import SimpleNamespace
import threading

import pytest

from case_intelligence.report_compilation_jobs import ReportCompilationJobs
from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests import test_matter_media_workflow as media


def test_fixture_write_commits_rolls_back_and_releases(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture (value TEXT)')
            connection.execute("INSERT INTO synthetic_fixture VALUES ('committed')")
        with pytest.raises(ValueError, match='synthetic failure'):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('rolled back')")
                raise ValueError('synthetic failure')
        with fixture_write(store) as connection:
            assert [row[0] for row in connection.execute('SELECT value FROM synthetic_fixture')] == ['committed']
            assert not connection.in_transaction
        acquired = threading.Event()
        def another_writer():
            with fixture_write(store):
                acquired.set()
        worker = threading.Thread(target=another_writer, daemon=True)
        worker.start()
        worker.join(5)
        assert acquired.is_set() and not worker.is_alive()
    finally:
        store.close()


def test_live_media_fixture_does_not_commit_concurrent_report_claim(tmp_path, monkeypatch):
    """Run the exact media fixture write with an overlapping real queue claim.

    Extract only the write block so media inspection/HTTP prerequisites do not
    hide the shared-connection regression. The block is taken from the actual
    workflow test; reverting its helper call also reverts this exercised code.
    """
    source = inspect.getsource(media.test_interrupted_overview_at_automatic_limit_requires_manual_retry)
    tree = ast.parse(source)
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.With)
                 and any(isinstance(item, ast.Constant) and isinstance(item.value, str)
                         and item.value.startswith('UPDATE workbench_media_summary')
                         for statement in node.body if isinstance(statement, ast.Expr)
                         for item in ast.walk(statement)))
    code = compile(ast.Module(body=[block], type_ignores=[]), '<live-media-fixture>', 'exec')
    workspace = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    lock = workspace._lock
    connection = workspace.connection
    jobs = ReportCompilationJobs(workspace)
    recover = jobs._recover
    main_thread = threading.current_thread()
    release = threading.Event()
    began = threading.Event()
    errors = []

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is main_thread:
                release.set()
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    class ObservedConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def __enter__(self):
            connection.__enter__()
            return self

        def __exit__(self, *args):
            try:
                return connection.__exit__(*args)
            finally:
                if threading.current_thread() is main_thread:
                    release.set()

    def paused_recover(database, now):
        assert database.in_transaction
        began.set()
        assert release.wait(5), 'fixture never reached workspace boundary'
        assert database.in_transaction, 'fixture committed the concurrent report claim'
        return recover(database, now)

    def claim():
        try:
            assert jobs.claim('synthetic-fixture-overlap') is None
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(workspace, '_lock', ObservedLock())
    monkeypatch.setattr(workspace, 'connection', ObservedConnection())
    monkeypatch.setattr(jobs, '_recover', paused_recover)
    worker = threading.Thread(target=claim, daemon=True)
    try:
        worker.start()
        assert began.wait(5), 'real report claim did not begin'
        exec(code, {**vars(media), 'bench': SimpleNamespace(workspace=workspace),
                    'failed': SimpleNamespace(transcript_id='synthetic-no-transcript')})
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive(), 'report claim did not finish'
        workspace.close()
    assert not errors, errors


def test_nested_fixture_write_preserves_caller_transaction(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture (value TEXT)')
        with pytest.raises(ValueError, match='outer rollback'):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('outer')")
                with fixture_write(store) as nested:
                    nested.execute("INSERT INTO synthetic_fixture VALUES ('inner')")
                assert connection.in_transaction
                with pytest.raises(ValueError, match='inner rollback'):
                    with fixture_write(store) as nested:
                        nested.execute("INSERT INTO synthetic_fixture VALUES ('discard')")
                        raise ValueError('inner rollback')
                assert [row[0] for row in connection.execute('SELECT value FROM synthetic_fixture')] == ['outer', 'inner']
                raise ValueError('outer rollback')
        with fixture_write(store) as connection:
            assert connection.execute('SELECT value FROM synthetic_fixture').fetchall() == []
    finally:
        store.close()


def test_nested_fixture_preserves_sqlite_constraint_error_and_caller_work(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture (value TEXT UNIQUE)')
        with fixture_write(store) as connection:
            connection.execute("INSERT INTO synthetic_fixture VALUES ('duplicate')")
            with pytest.raises(sqlite3.IntegrityError, match='UNIQUE constraint'):
                with fixture_write(store) as nested:
                    nested.execute("INSERT INTO synthetic_fixture VALUES ('duplicate')")
            assert connection.in_transaction
            assert [row[0] for row in connection.execute('SELECT value FROM synthetic_fixture')] == ['duplicate']
    finally:
        store.close()
