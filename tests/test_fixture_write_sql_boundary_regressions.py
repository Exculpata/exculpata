from textwrap import dedent
from tests.fixture_probe import fixture_probe
"""Synthetic SQL state and alias boundary regressions."""
import sqlite3

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('method,args', [('execute', "'DELETE FROM segment'"), ('executescript', "'DELETE FROM segment;'"), ('commit', ''), ('rollback', '')])
@pytest.mark.parametrize('saved', [False, True])
def test_guard_tracks_getattr_sql_and_transaction_methods(method, args, saved):
    method_source = f"getattr(bench.workspace.connection, {method!r})"
    source = f'operation = {method_source}\noperation({args})' if saved else f'{method_source}({args})'
    assert check_fixture_writes(source)


@pytest.mark.parametrize('assignment', [
    'connection, = (bench.workspace.connection,)',
    '[connection] = [bench.workspace.connection]',
    "pair = (bench.workspace.connection, 'synthetic')\nconnection, label = pair",
    "((connection,), label) = ((bench.workspace.connection,), 'synthetic')",
    "*connections, label = (bench.workspace.connection, 'synthetic')\nconnection, = connections",
])
def test_guard_preserves_unpacked_connection_contexts(assignment):
    assert any(v.rule == 'naked connection context' for v in check_fixture_writes(assignment+'\nwith connection:\n    pass'))


@pytest.mark.parametrize('statement', ['CREATE TABLE synthetic_new(value TEXT)', 'DROP TABLE synthetic_fixture', 'ALTER TABLE synthetic_fixture ADD COLUMN extra TEXT'])
@pytest.mark.parametrize('cursor', [False, True])
def test_ordinary_ddl_rolls_back_without_prior_dml(tmp_path, statement, cursor):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        before = [tuple(r) for r in store.connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
        with pytest.raises(ValueError, match='synthetic rollback'):
            with fixture_write(store) as connection:
                handle = connection.cursor() if cursor else connection
                handle.execute(statement)
                raise ValueError('synthetic rollback')
        assert [tuple(r) for r in store.connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name")] == before
    finally:
        store.close()


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('action', ['ATTACH', 'DETACH'])
def test_failed_fixture_preserves_attachment_set(tmp_path, nested, action):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        if action == 'DETACH':
            store.connection.execute("ATTACH ':memory:' AS synthetic_external")  # fixture-write: Prepare an isolated raw attachment solely to test rejected detachment.
        before = [tuple(row) for row in store.connection.execute('PRAGMA database_list')]
        try:
            with fixture_write(store) as outer:
                sql = "ATTACH ':memory:' AS synthetic_external" if action == 'ATTACH' else 'DETACH synthetic_external'
                if nested:
                    with fixture_write(store) as connection:
                        connection.execute(sql)
                else:
                    outer.execute(sql)
                raise ValueError('synthetic rollback')
        except (RuntimeError, ValueError):
            pass
        assert [tuple(row) for row in store.connection.execute('PRAGMA database_list')] == before
    finally:
        store.close()


@pytest.mark.parametrize('level', ['connection', 'cursor'])
@pytest.mark.parametrize('returned', ['connection', 'cursor'])
def test_row_factory_cannot_leak_unscoped_sqlite_objects(tmp_path, level, returned):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            factory = lambda cursor, row: cursor.connection if returned == 'connection' else cursor
            if level == 'connection':
                connection.row_factory = factory
                cursor = connection.cursor()
            else:
                cursor = connection.cursor()
                cursor.row_factory = factory
            leaked = cursor.execute('SELECT 1').fetchone()
        with pytest.raises(RuntimeError, match='fixture_write'):
            leaked.execute('SELECT 1')
    finally:
        store.close()


@pytest.mark.parametrize('keyword', [False, True])
def test_custom_cursor_factory_cannot_receive_raw_connection(tmp_path, keyword):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    seen = []
    def factory(raw_connection):
        seen.append(raw_connection)
        return raw_connection.cursor()
    try:
        with fixture_write(store) as connection:
            with pytest.raises(RuntimeError, match='fixture_write'):
                connection.cursor(factory=factory) if keyword else connection.cursor(factory)
            assert seen == []
    finally:
        store.close()


@pytest.mark.parametrize('sql', [
    "WITH items(value) AS (VALUES ('synthetic')) INSERT INTO synthetic_fixture SELECT value FROM items",
    "WITH items(value) AS (VALUES ('original')) DELETE FROM synthetic_fixture WHERE value IN (SELECT value FROM items)",
    "; INSERT INTO synthetic_fixture VALUES ('synthetic')",
    "\ufeffINSERT INTO synthetic_fixture VALUES ('synthetic')",
    'ANALYZE synthetic_fixture',
])
def test_nonimplicit_sql_writes_roll_back(tmp_path, sql):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
            connection.execute("INSERT INTO synthetic_fixture VALUES ('original')")
        before_schema = [tuple(row) for row in store.connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
        with pytest.raises(ValueError, match='synthetic rollback'):
            with fixture_write(store) as connection:
                connection.execute(sql)
                raise ValueError('synthetic rollback')
        assert [tuple(row) for row in store.connection.execute('SELECT * FROM synthetic_fixture')] == [('original',)]
        assert [tuple(row) for row in store.connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name')] == before_schema
    finally:
        store.close()


def test_copied_context_cannot_commit_an_active_nested_fixture(tmp_path):
    from contextvars import copy_context
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        with pytest.raises(ValueError, match='synthetic rollback'):
            with fixture_write(store) as outer:
                outer.execute("INSERT INTO synthetic_fixture VALUES ('outer')")
                saved = copy_context()
                with fixture_write(store) as nested:
                    nested.execute("INSERT INTO synthetic_fixture VALUES ('inner')")
                    with pytest.raises(RuntimeError, match='fixture_write'):
                        saved.run(outer.commit)
                raise ValueError('synthetic rollback')
        assert list(store.connection.execute('SELECT * FROM synthetic_fixture')) == []
    finally:
        store.close()


def test_empty_context_cannot_hide_read_first_outer_fixture(tmp_path):
    from contextvars import Context
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        def insert():
            with fixture_write(store) as nested:
                nested.execute("INSERT INTO synthetic_fixture VALUES ('inner')")
        with pytest.raises(ValueError, match='synthetic rollback'):
            with fixture_write(store) as outer:
                outer.execute('SELECT 1')
                Context().run(insert)
                raise ValueError('synthetic rollback')
        assert list(store.connection.execute('SELECT * FROM synthetic_fixture')) == []
    finally:
        store.close()


def test_public_workspace_query_uses_scoped_row_factory(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    source = dedent("""\
        import pytest
        import sqlite3
        from tests.fixture_write import fixture_write
        store = workspace
        captured = []
        def factory(cursor, row):
            captured.append(cursor.connection)
            return dict(zip([column[0] for column in cursor.description], row))
        try:
            principal = store.upsert_principal('local', 'synthetic', 'Synthetic reviewer', 'synthetic')
            original = store.connection.row_factory
            with fixture_write(store) as connection:
                connection.row_factory = factory
                assert connection.row_factory is factory
                # fixture-write: Probe a public query on an isolated synthetic store to verify scoped callback handles.
                assert store.principal_preference(principal.principal_id).theme == 'dark'
            assert captured
            with pytest.raises(RuntimeError, match='fixture_write'):
                captured[0].execute('SELECT 1')
            assert store.connection.row_factory is original
            assert store.principal_preference(principal.principal_id).theme == 'dark'
        finally:
            store.close()
    """)
    assert check_fixture_writes(source)
    error, _ = fixture_probe(source, workspace=store)
    if error is not None:
        raise error


@pytest.mark.parametrize('configured_before', [False, True])
def test_autocommit_true_is_rejected_before_fixture_mutation(tmp_path, configured_before):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        if configured_before:
            store.connection.autocommit = True  # fixture-write: Deliberately configure an unsupported isolated connection to verify fail-closed entry.
        with pytest.raises(RuntimeError, match='fixture_write'):
            with fixture_write(store) as connection:
                if not configured_before:
                    connection.autocommit = True
                connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
        assert store.connection.execute("SELECT name FROM sqlite_master WHERE name='synthetic_fixture'").fetchone() is None
        assert not store.connection.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize('sql', ["WITH value(n) AS (VALUES(1)) INSERT INTO segment SELECT n FROM value", '; INSERT INTO segment VALUES(1)', '\ufeffINSERT INTO segment VALUES(1)', 'ANALYZE segment'])
def test_guard_requires_handle_for_nonimplicit_sql(sql):
    raw = f'with fixture_write(bench.workspace):\n    bench.workspace.connection.execute({sql!r})'
    scoped = f'with fixture_write(bench.workspace) as connection:\n    connection.execute({sql!r})'
    assert check_fixture_writes(raw)
    assert not check_fixture_writes(scoped)


def test_nested_custom_factories_restore_outer_and_keep_caller_transaction(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    source = dedent("""\
        import pytest
        import sqlite3
        from tests.fixture_write import fixture_write
        store = workspace
        captured = []
        def factory(cursor, row):
            captured.append(cursor.connection)
            return row
        try:
            with fixture_write(store) as outer:
                outer.execute('CREATE TABLE synthetic_fixture(value TEXT)')
                outer.row_factory = factory
                with fixture_write(store) as nested:
                    assert nested.row_factory is factory
                    nested.execute("INSERT INTO synthetic_fixture VALUES ('inner')")
                    nested.execute('SELECT 1').fetchone()
                    with pytest.raises(RuntimeError, match='fixture_write'):
                        captured[-1].commit()  # fixture-write: Negative regression deliberately invokes the callback-captured handle and requires rejection.
                assert outer.row_factory is factory
                outer.execute('SELECT 1').fetchone()
                assert captured[-1] is outer
                assert outer.in_transaction
            assert store.connection.row_factory is sqlite3.Row
        finally:
            store.close()
    """)
    assert check_fixture_writes(source)
    error, _ = fixture_probe(source, workspace=store)
    if error is not None:
        raise error


@pytest.mark.parametrize('assignment', [
    'if choice:\n    pair = (bench.workspace.connection,)\nelse:\n    pair = (other.workspace.connection,)\nconnection, = pair',
    'pair = (bench.workspace.connection,) if choice else (other.workspace.connection,)\nconnection, = pair',
])
def test_guard_keeps_unpacked_connection_provenance_across_branches(assignment):
    assert check_fixture_writes(assignment + '\nwith connection:\n    pass')


@pytest.mark.parametrize('source', [
    'if flag:\n    write = left.workspace.connection.execute\nelse:\n    write = right.workspace.connection.execute\nwrite("DELETE FROM workbench_matter")',
    'import sqlite3\nif flag:\n    workspace = left.workspace\nelse:\n    workspace = right.workspace\nsqlite3.connect(workspace.path)\nworkspace.queue_media_job("synthetic")',
    'if flag:\n    method = "execute"\nelse:\n    method = "executemany"\ngetattr(bench.workspace.connection, method)("DELETE FROM workbench_matter", [])',
    'getattr(bench.workspace, "queue_media_job")("synthetic")',
])
def test_guard_retains_operation_and_workspace_aliases(source):
    assert check_fixture_writes(source)
