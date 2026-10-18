"""Synthetic transaction replacement and retained-cursor lifetime regressions."""
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from tests.fixture_write import fixture_write


@pytest.fixture
def store(tmp_path):
    raw = sqlite3.connect(tmp_path / 'synthetic-exit.sqlite')
    raw.execute('CREATE TABLE synthetic_fixture(value TEXT)')
    workspace = SimpleNamespace(connection=raw, _lock=RLock())
    try:
        yield workspace
    finally:
        raw.close()


@pytest.mark.parametrize('failure', [False, True])
def test_pep249_transition_cannot_leave_replacement_transaction_or_hide_next_fixture(store, tmp_path, failure):
    rejected = []
    try:
        with fixture_write(store) as connection:
            connection.execute("INSERT INTO synthetic_fixture VALUES ('first')")
            try:
                connection.autocommit = False
            except RuntimeError as error:
                rejected.append(str(error))
            if failure:
                raise ValueError('synthetic rollback')
    except ValueError:
        pass
    leaked_transaction = store.connection.in_transaction
    with fixture_write(store) as connection:
        connection.execute("INSERT INTO synthetic_fixture VALUES ('second')")
    with sqlite3.connect(tmp_path / 'synthetic-exit.sqlite') as independent:
        visible = independent.execute('SELECT value FROM synthetic_fixture ORDER BY value').fetchall()
    assert visible == ([('second',)] if failure else [('first',), ('second',)])
    assert not leaked_transaction
    assert rejected and 'autocommit=False' in rejected[0]
    assert store.connection.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL


@pytest.mark.parametrize('failure', [False, True])
def test_existing_pep249_mode_preserves_caller_transaction(store, tmp_path, failure):
    # fixture-write: Establish caller-owned PEP249 mode before testing savepoint ownership.
    store.connection.autocommit = False
    # fixture-write: Seed caller data that the fixture must neither commit nor discard.
    store.connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
    try:
        with fixture_write(store) as connection:
            connection.execute("INSERT INTO synthetic_fixture VALUES ('inner')")
            if failure:
                raise ValueError('synthetic rollback')
    except ValueError:
        pass
    assert store.connection.autocommit is False and store.connection.in_transaction
    assert store.connection.execute('SELECT value FROM synthetic_fixture ORDER BY value').fetchall() == (
        [('caller',)] if failure else [('caller',), ('inner',)]
    )
    with sqlite3.connect(tmp_path / 'synthetic-exit.sqlite') as independent:
        assert independent.execute('SELECT * FROM synthetic_fixture').fetchall() == []


def custom_factory(cursor, row):
    return {cursor.description[0][0]: row[0]}


@pytest.mark.parametrize('factory', [None, sqlite3.Row, custom_factory])
@pytest.mark.parametrize('operation', ['fetchone', 'fetchmany', 'fetchall', 'next'])
def test_retained_raw_nonempty_cursor_rejects_fetch_after_boundary(store, factory, operation):
    # fixture-write: Configure an isolated factory to exercise raw-cursor invalidation.
    store.connection.row_factory = factory
    with fixture_write(store):
        # fixture-write: Deliberately retain a raw cursor to verify the runtime factory scope guard.
        cursor = store.connection.execute("SELECT 'synthetic row' AS value")
    assert store.connection.row_factory is factory
    with pytest.raises(RuntimeError, match='owning boundary'):
        # fixture-write: Deliberately fetch from an expired raw cursor and require rejection.
        next(cursor) if operation == 'next' else getattr(cursor, operation)()


@pytest.mark.parametrize('factory', [None, sqlite3.Row, custom_factory])
@pytest.mark.parametrize('operation', ['fetchone', 'fetchmany', 'fetchall', 'next'])
def test_yielded_zero_row_cursor_rejects_fetch_without_needing_factory_callback(store, factory, operation):
    with fixture_write(store) as connection:
        connection.row_factory = factory
        cursor = connection.execute('SELECT 1 WHERE 0')
    with pytest.raises(RuntimeError, match='owning boundary'):
        # fixture-write: Deliberately fetch from an expired empty scoped cursor and require rejection.
        next(cursor) if operation == 'next' else getattr(cursor, operation)()


@pytest.mark.parametrize('factory', [None, sqlite3.Row, custom_factory])
def test_scoped_factory_preserves_row_types_and_restores_factory(store, factory):
    # fixture-write: Configure the isolated initial factory for compatibility verification.
    store.connection.row_factory = factory
    with fixture_write(store) as connection:
        assert connection.row_factory is factory
        row = connection.execute("SELECT 'synthetic row' AS value").fetchone()
        assert type(row) is (tuple if factory is None else sqlite3.Row if factory is sqlite3.Row else dict)
        assert row[0 if factory is None else 'value'] == 'synthetic row'
    assert store.connection.row_factory is factory


def test_custom_factory_still_receives_scoped_cursor_and_connection(store):
    captured = []
    def factory(cursor, row):
        captured.append((cursor, cursor.connection))
        return row
    with fixture_write(store) as connection:
        connection.row_factory = factory
        assert connection.execute('SELECT 7').fetchone() == (7,)
        cursor, callback_connection = captured[0]
        assert callback_connection is connection
    for handle in (cursor, callback_connection):
        with pytest.raises(RuntimeError, match='owning boundary'):
            handle.execute('SELECT 8')


@pytest.mark.parametrize('factory', [None, sqlite3.Row])
@pytest.mark.parametrize('active', [False, True])
def test_raw_nonempty_cursor_rejects_worker_thread_before_row_is_delivered(tmp_path, factory, active):
    from threading import Thread
    raw = sqlite3.connect(tmp_path / 'synthetic-thread.sqlite', check_same_thread=False)
    raw.row_factory = factory
    workspace = SimpleNamespace(connection=raw, _lock=RLock())
    delivered, errors = [], []
    def fetch():
        try:
            # fixture-write: Deliberately fetch this raw fixture cursor from a non-owning worker thread.
            delivered.append(cursor.fetchone())
        except RuntimeError as error:
            errors.append(str(error))
    try:
        with fixture_write(workspace):
            # fixture-write: Retain a raw nonempty cursor solely to prove the factory checks thread ownership.
            cursor = workspace.connection.execute("SELECT 'synthetic worker row'")
            if active:
                worker = Thread(target=fetch)
                worker.start()
                worker.join(5)
                assert not worker.is_alive()
        if not active:
            worker = Thread(target=fetch)
            worker.start()
            worker.join(5)
            assert not worker.is_alive()
        assert delivered == []
        assert len(errors) == 1 and 'owning boundary' in errors[0]
    finally:
        raw.close()


@pytest.mark.parametrize('factory', [None, sqlite3.Row, custom_factory])
@pytest.mark.parametrize('operation', ['fetchone', 'fetchmany', 'fetchall', 'next'])
def test_exhausted_yielded_cursor_still_rechecks_lifetime(store, factory, operation):
    with fixture_write(store) as connection:
        connection.row_factory = factory
        cursor = connection.execute('SELECT 1 AS value')
        assert len(cursor.fetchall()) == 1
        assert cursor.fetchall() == []
    with pytest.raises(RuntimeError, match='owning boundary'):
        # fixture-write: Deliberately consume an expired exhausted scoped cursor and require rejection.
        next(cursor) if operation == 'next' else getattr(cursor, operation)()
