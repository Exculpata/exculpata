"""Actual isolated SQLite mutations behind the non-SQL ownership inventory."""
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


@pytest.fixture
def sqlite_pair():
    target = sqlite3.connect(':memory:')
    source = sqlite3.connect(':memory:')
    for raw, value in ((target, 'original'), (source, 'replacement')):
        initial = SimpleNamespace(connection=raw, _lock=threading.RLock())
        with fixture_write(initial) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
            connection.execute('INSERT INTO synthetic_fixture VALUES (?)', (value,))
            connection.execute('CREATE TABLE synthetic_blob(value BLOB)')
            connection.execute('INSERT INTO synthetic_blob VALUES (?)', (b'old!',))
    target.row_factory = sqlite3.Row
    workspace = SimpleNamespace(connection=target, _lock=threading.RLock())
    try:
        yield SimpleNamespace(workspace=workspace), source
    finally:
        target.close()
        source.close()


@pytest.mark.parametrize('attribute', ['row_factory', 'text_factory'])
@pytest.mark.parametrize('descriptor', [False, True])
def test_factory_deletion_changes_raw_state_and_requires_inventory(sqlite_pair, attribute, descriptor):
    bench, _ = sqlite_pair
    operation = (f'sqlite3.Connection.{attribute}.__delete__(bench.workspace.connection)' if descriptor
                 else f'del bench.workspace.connection.{attribute}')
    original = getattr(bench.workspace.connection, attribute)
    try:
        exec(operation, {'bench': bench, 'sqlite3': sqlite3})
        assert getattr(bench.workspace.connection, attribute) is None
        # Do not run SQLite queries with a deleted text factory: some builds crash.
    finally:
        # fixture-write: Restore the exact factory immediately after isolated deletion evidence, before any query.
        setattr(bench.workspace.connection, attribute, original)
    assert bench.workspace.connection.execute("SELECT 'synthetic restored'").fetchone()[0] == 'synthetic restored'
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('operation', [
    'bench.workspace.connection.deserialize(payload)',
    'sqlite3.Connection.deserialize(bench.workspace.connection, payload)',
])
def test_deserialize_replaces_actual_shared_database_without_sql(sqlite_pair, operation):
    bench, source = sqlite_pair
    payload = source.serialize()
    assert bench.workspace.connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'original'
    exec(operation, {'bench': bench, 'sqlite3': sqlite3, 'payload': payload})
    assert bench.workspace.connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'replacement'
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('opening', [
    "bench.workspace.connection.blobopen('synthetic_blob', 'value', 1)",
    "sqlite3.Connection.blobopen(bench.workspace.connection, 'synthetic_blob', 'value', 1)",
])
def test_writable_blob_changes_actual_shared_bytes_without_sql(sqlite_pair, opening):
    bench, _ = sqlite_pair
    operation = f"with {opening} as blob:\n    blob.write(b'new!')"
    exec(operation, {'bench': bench, 'sqlite3': sqlite3})
    assert bench.workspace.connection.execute('SELECT value FROM synthetic_blob').fetchone()[0] == b'new!'
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('operation', [
    'source.backup(bench.workspace.connection)',
    'sqlite3.Connection.backup(source, bench.workspace.connection)',
])
def test_backup_overwrites_actual_shared_destination_without_sql(sqlite_pair, operation):
    bench, source = sqlite_pair
    exec(operation, {'bench': bench, 'source': source, 'sqlite3': sqlite3})
    assert bench.workspace.connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'replacement'
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('operation', [
    'bench.workspace.connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)',
    'sqlite3.Connection.setlimit(bench.workspace.connection, sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096)',
])
def test_setlimit_changes_actual_connection_configuration_and_requires_ownership(sqlite_pair, operation):
    bench, _ = sqlite_pair
    old = bench.workspace.connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH)
    assert old > 4096
    exec(operation, {'bench': bench, 'sqlite3': sqlite3})
    assert bench.workspace.connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH) == 4096
    assert check_fixture_writes('import sqlite3\n' + operation)


@pytest.mark.parametrize('target,attribute', [
    ('connection', 'row_factory'), ('connection', 'text_factory'), ('cursor', 'row_factory'),
])
@pytest.mark.parametrize('descriptor', [False, True])
def test_yielded_proxy_rejects_factory_deletion_without_touching_raw_state(sqlite_pair, target, attribute, descriptor):
    bench, _ = sqlite_pair
    with fixture_write(bench.workspace) as connection:
        handle = connection if target == 'connection' else connection.cursor()
        raw = bench.workspace.connection if target == 'connection' else handle._cursor
        original = getattr(raw, attribute)
        descriptor_type = 'Connection' if target == 'connection' else 'Cursor'
        operation = (f'sqlite3.{descriptor_type}.{attribute}.__delete__(handle)' if descriptor
                     else f'del handle.{attribute}')
        with pytest.raises(TypeError if descriptor else RuntimeError):
            exec(operation, {'handle': handle, 'sqlite3': sqlite3})
        assert getattr(raw, attribute) is original
        assert connection.execute("SELECT 'synthetic untouched'").fetchone()[0] == 'synthetic untouched'


@pytest.mark.parametrize('target,attribute', [
    ('connection', 'row_factory'), ('connection', 'text_factory'), ('cursor', 'row_factory'),
])
def test_saved_and_expired_deletion_recheck_boundary_without_mutating_factories(sqlite_pair, target, attribute):
    bench, _ = sqlite_pair
    with fixture_write(bench.workspace) as connection:
        handle = connection if target == 'connection' else connection.cursor()
        raw = bench.workspace.connection if target == 'connection' else handle._cursor
        original = getattr(raw, attribute)
        saved = handle.__delattr__
        with pytest.raises(RuntimeError, match='does not support attribute deletion'):
            saved(attribute)
        assert getattr(raw, attribute) is original
    for remove in (lambda: saved(attribute), lambda: delattr(handle, attribute)):
        with pytest.raises(RuntimeError, match='owning boundary'):
            remove()
        assert getattr(raw, attribute) is original


@pytest.mark.parametrize('operation', ['deserialize', 'blobopen', 'backup'])
def test_yielded_proxy_rejects_unsupported_mutation_before_raw_state_changes(sqlite_pair, operation):
    bench, source = sqlite_pair
    with fixture_write(bench.workspace) as connection:
        with pytest.raises(RuntimeError, match='fixture_write does not support'):
            if operation == 'deserialize':
                # fixture-write: Deliberately attempt unsupported deserialization through the proxy and require rejection.
                connection.deserialize(source.serialize())
            elif operation == 'blobopen':
                # fixture-write: Deliberately attempt unsupported blob access through the proxy and require rejection.
                connection.blobopen('synthetic_blob', 'value', 1)
            else:
                # fixture-write: Deliberately attempt unsupported backup through the proxy and require rejection.
                connection.backup(source)
        assert connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'original'
        assert connection.execute('SELECT value FROM synthetic_blob').fetchone()[0] == b'old!'
    assert source.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'replacement'


def test_backup_cannot_accept_scoped_proxy_as_shared_destination(sqlite_pair):
    bench, source = sqlite_pair
    with fixture_write(bench.workspace) as connection:
        with pytest.raises(TypeError):
            # fixture-write: Deliberately pass a scoped destination to SQLite and verify rejection before mutation.
            source.backup(connection)
        assert connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'original'


def test_scoped_setlimit_rechecks_thread_and_lifetime_before_mutating(sqlite_pair):
    bench, _ = sqlite_pair
    errors = []
    with fixture_write(bench.workspace) as connection:
        original = connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH)
        assert connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4096) == original
        assert connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH) == 4096
        saved = connection.setlimit

        def another_thread():
            try:
                # fixture-write: Deliberately invoke the saved setter from another thread and require ownership rejection.
                saved(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192)
            except RuntimeError as error:
                errors.append(str(error))

        thread = threading.Thread(target=another_thread)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1 and 'owning boundary' in errors[0]
        assert connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH) == 4096
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, original)
    with pytest.raises(RuntimeError, match='owning boundary'):
        # fixture-write: Deliberately invoke the expired saved setter and require rejection before changing limits.
        saved(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192)
    assert bench.workspace.connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH) == original
