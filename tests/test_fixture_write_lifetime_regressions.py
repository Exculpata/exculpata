"""Synthetic fixture lifetime, script and provenance regressions."""
import sqlite3
import threading
from textwrap import dedent

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('cursor_script', [False, True])
def test_outer_script_cannot_escape_exception_rollback(tmp_path, cursor_script):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        with pytest.raises(ValueError, match='synthetic rollback'):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('first')")
                handle = connection.cursor() if cursor_script else connection
                handle.executescript("INSERT INTO synthetic_fixture VALUES ('script');")
                raise ValueError('synthetic rollback')
        with fixture_write(store) as connection:
            assert connection.execute('SELECT * FROM synthetic_fixture').fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize('attribute,value', [('row_factory', sqlite3.Row), ('text_factory', bytes)])
def test_writable_connection_attributes_change_query_results(tmp_path, attribute, value):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        store.connection.row_factory = None
        with fixture_write(store) as connection:
            connection.row_factory = None
            setattr(connection, attribute, value)
            assert getattr(connection, attribute) is value
            row = connection.execute("SELECT 'synthetic' AS value").fetchone()
            if attribute == 'row_factory':
                assert isinstance(row, sqlite3.Row)
                assert row['value'] == 'synthetic'
            else:
                assert row[0] == b'synthetic'
    finally:
        store.close()


@pytest.mark.parametrize('operation', ['context', 'commit', 'cursor', 'saved_method'])
def test_expired_handle_cannot_touch_a_worker_transaction(tmp_path, operation):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    began, release = threading.Event(), threading.Event()
    errors = []
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
            cursor = connection.cursor()
            saved_method = connection.set_authorizer

        def worker_transaction():
            try:
                with fixture_write(store) as worker:
                    worker.execute("INSERT INTO synthetic_fixture VALUES ('worker')")
                    began.set()
                    assert release.wait(5)
                    assert worker.in_transaction, 'expired handle ended worker transaction'
                    raise ValueError('worker rollback')
            except ValueError:
                pass
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=worker_transaction, daemon=True)
        thread.start()
        try:
            assert began.wait(5)
            with pytest.raises(RuntimeError, match='fixture_write'):
                if operation == 'context':
                    with connection:  # fixture-write: Deliberately attempt expired context entry against the worker transaction.
                        pass
                elif operation == 'commit':
                    connection.commit()  # fixture-write: Deliberately attempt an expired commit against the worker transaction.
                elif operation == 'cursor':
                    cursor.execute("INSERT INTO synthetic_fixture VALUES ('expired')")  # fixture-write: Deliberately attempt an expired cursor write against the worker transaction.
                else:
                    saved_method(None)
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert not errors, errors
        with fixture_write(store) as current:
            assert current.execute('SELECT * FROM synthetic_fixture').fetchall() == []
    finally:
        store.close()


def test_guard_merges_match_case_provenance():
    source = dedent('''\
        import sqlite3
        match choice:
            case 'live':
                connection = bench.workspace.connection
            case _:
                connection = sqlite3.connect(':memory:')
        connection.execute('DELETE FROM segment')
    ''')
    assert check_fixture_writes(source)


@pytest.mark.parametrize('declaration', ['global', 'nonlocal'])
def test_guard_tracks_explicit_class_outer_connection_assignments(declaration):
    body = dedent(f'''\
        connection = sqlite3.connect(':memory:')
        class Synthetic:
            {declaration} connection
            connection = bench.workspace.connection
        with connection:
            pass
    ''')
    source = 'import sqlite3\n'
    source += ('def seed():\n' + '\n'.join('    ' + line for line in body.splitlines())
               if declaration == 'nonlocal' else body)
    assert any(v.rule == 'naked connection context' for v in check_fixture_writes(source))


@pytest.mark.parametrize('expression', ["f'PRAGMA {value}'", "'PRAGMA ' + value", "'PRAGMA {}'.format(value)", "f'PRAGMA user_version{value}'"])
def test_guard_treats_dynamic_pragma_as_unknown_write(expression):
    source = f'def seed(value):\n    bench.workspace.connection.execute({expression})'
    assert check_fixture_writes(source)


@pytest.mark.parametrize('yielded', [False, True])
def test_guard_accepts_keyword_fixture_workspace(yielded):
    binding = ' as connection' if yielded else ''
    receiver = 'connection' if yielded else 'bench.workspace.connection'
    source = f"with fixture_write(workspace=bench.workspace){binding}:\n    {receiver}.execute('DELETE FROM segment')"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('failure', ['outer', 'script'])
def test_script_ddl_and_trigger_statements_roll_back_together(tmp_path, failure):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    script = """
        CREATE TABLE synthetic_fixture(value TEXT);
        CREATE TABLE synthetic_audit(value TEXT);
        CREATE TRIGGER synthetic_log AFTER INSERT ON synthetic_fixture BEGIN
            INSERT INTO synthetic_audit VALUES ('trigger;value');
            INSERT INTO synthetic_audit VALUES (NEW.value);
        END;
        INSERT INTO synthetic_fixture VALUES ('quoted;value'); -- comment ;
    """
    try:
        with pytest.raises(ValueError if failure == 'outer' else sqlite3.OperationalError):
            with fixture_write(store) as connection:
                connection.executescript(script + ('SELECT * FROM missing_synthetic_table;' if failure == 'script' else ''))
                assert [r[0] for r in connection.execute('SELECT value FROM synthetic_audit')] == ['trigger;value', 'quoted;value']
                raise ValueError('outer rollback')
        with fixture_write(store) as connection:
            assert connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'synthetic_%'").fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize('statement', ['COMMIT;', 'END;', 'ROLLBACK;', 'BEGIN;', 'SAVEPOINT escape;', 'RELEASE escape;', "INSERT OR ROLLBACK INTO synthetic_fixture VALUES ('x');"])
def test_outer_script_rejects_transaction_escape_before_execution(tmp_path, statement):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
            connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
            with pytest.raises(RuntimeError, match='fixture_write'):
                connection.executescript("INSERT INTO synthetic_fixture VALUES ('script');" + statement)
            assert connection.in_transaction
            assert [r[0] for r in connection.execute('SELECT * FROM synthetic_fixture')] == ['caller']
    finally:
        store.close()


@pytest.mark.parametrize('operation', ['blobopen', 'deserialize', 'backup', 'iterdump'])
def test_unsupported_connection_operations_fail_closed(tmp_path, operation):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            with pytest.raises(RuntimeError, match='fixture_write does not support'):
                getattr(connection, operation)
    finally:
        store.close()


def test_cursor_factories_remain_real_attributes_and_expire(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            cursor = connection.cursor()
            cursor.row_factory = sqlite3.Row
            assert cursor.row_factory is sqlite3.Row
            cursor.arraysize = 2
            cursor.execute("SELECT 'one' AS value UNION ALL SELECT 'two'")
            assert [row['value'] for row in cursor.fetchmany()] == ['one', 'two']
            saved_fetch = cursor.fetchone
            saved_factory = connection.cursor
            with pytest.raises(AttributeError, match='read-only'):
                cursor.connection = connection
        for operation in [lambda: cursor.fetchone(), saved_fetch, saved_factory, lambda: next(cursor),
                          lambda: setattr(cursor, 'arraysize', 3), lambda: setattr(connection, 'row_factory', None)]:
            with pytest.raises(RuntimeError, match='fixture_write'):
                operation()
    finally:
        store.close()


def test_active_handle_is_not_transferable_to_another_thread(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    errors = []
    try:
        with fixture_write(store) as connection:
            def use_from_another_thread():
                try:
                    connection.execute('SELECT 1')
                except RuntimeError as error:
                    errors.append(str(error))
            thread = threading.Thread(target=use_from_another_thread, daemon=True)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            assert len(errors) == 1 and 'fixture_write' in errors[0]
    finally:
        store.close()


@pytest.mark.parametrize('factory', [lambda cursor, row: dict(zip([c[0] for c in cursor.description], row)), None])
def test_nested_schema_checks_ignore_custom_row_and_text_factories(tmp_path, factory):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as outer:
            outer.row_factory = factory
            outer.text_factory = bytes
            with fixture_write(store) as nested:
                nested.execute('CREATE TABLE synthetic_fixture(value TEXT)')
                nested.execute("INSERT INTO synthetic_fixture VALUES ('nested')")
            outer.row_factory = None
            assert outer.execute('SELECT * FROM synthetic_fixture').fetchone() == (b'nested',)
    finally:
        store.close()


@pytest.mark.parametrize('source,violation', [
    ("import sqlite3\nmatch choice:\n    case 'first':\n        connection = sqlite3.connect(':memory:')\n    case _:\n        connection = sqlite3.connect(':memory:')\nconnection.execute('DELETE FROM segment')", False),
    ("import sqlite3\nconnection = sqlite3.connect(':memory:')\nmatch choice:\n    case 'live' if (connection := bench.workspace.connection) and False:\n        pass\n    case _:\n        with connection:\n            pass", True),
    ("connection = bench.workspace.connection\nmatch choice:\n    case connection:\n        pass\nwith connection:\n    pass", True),
])
def test_match_guards_capture_and_exhaustive_independent_cases(source, violation):
    assert bool(check_fixture_writes(source)) is violation
