"""Synthetic regressions for fixture-boundary review findings."""
from textwrap import dedent
import sqlite3

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


def test_nested_script_cannot_commit_caller_writes(tmp_path):
    store = WorkspaceStore(tmp_path / "synthetic.sqlite")
    try:
        with fixture_write(store) as connection:
            connection.execute("CREATE TABLE synthetic_fixture (value TEXT)")
        with pytest.raises(ValueError, match="caller rollback"):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
                with fixture_write(store) as nested:
                    with pytest.raises(RuntimeError, match="fixture_write"):
                        nested.executescript("INSERT INTO synthetic_fixture VALUES ('script');")
                    assert connection.in_transaction
                    assert [r[0] for r in connection.execute("SELECT value FROM synthetic_fixture")] == ["caller"]
                raise ValueError("caller rollback")
        with fixture_write(store) as connection:
            assert connection.execute("SELECT * FROM synthetic_fixture").fetchall() == []
    finally:
        store.close()


def test_guard_rejects_generator_consumed_after_fixture_boundary():
    source = dedent("""\
        with fixture_write(bench.workspace) as connection:
            deferred = (connection.execute('DELETE FROM workbench_matter') for _ in items)
        list(deferred)
    """)
    violations = check_fixture_writes(source)
    assert [(v.line, v.rule) for v in violations] == [(2, "SQL fixture write outside fixture_write")]


@pytest.mark.parametrize("table", ["segment", "source_version"])
@pytest.mark.parametrize("receiver", ["connection", "database"])
def test_guard_rejects_unannotated_helper_literal_writes(table, receiver):
    source = f"def seed({receiver}):\n    {receiver}.execute('DELETE FROM {table}')\nseed(bench.workspace.connection)"
    violations = check_fixture_writes(source)
    assert [(v.line, v.rule) for v in violations] == [(2, "SQL fixture write outside fixture_write")]


@pytest.mark.parametrize("method", ["commit", "rollback"])
@pytest.mark.parametrize("use_alias", [False, True])
def test_guard_rejects_unlocked_transaction_termination(method, use_alias):
    source = (f"finish = bench.workspace.connection.{method}\nfinish()" if use_alias
              else f"bench.workspace.connection.{method}()")
    violations = check_fixture_writes(source)
    assert violations
    assert all("fixture_write" in str(v) for v in violations)


@pytest.mark.parametrize("target", ["nested", "outer", "cursor", "cursor_connection"])
@pytest.mark.parametrize("method, sql", [
    ("executescript", "SELECT 1;"),
    ("execute", "/* synthetic */ COMMIT"),
    ("execute", ";\ufeff END"),
    ("execute", "-- synthetic\nROLLBACK"),
    ("execute", "RELEASE fixture_write_nested"),
])
def test_nested_control_rejected_before_caller_transaction_changes(tmp_path, target, method, sql):
    store = WorkspaceStore(tmp_path / "synthetic.sqlite")
    try:
        with fixture_write(store) as connection:
            connection.execute("CREATE TABLE synthetic_fixture (value TEXT)")
        with pytest.raises(ValueError, match="caller rollback"):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
                cursor = connection.cursor()
                with fixture_write(store) as nested:
                    handle = {"nested": nested, "outer": connection, "cursor": cursor,
                              "cursor_connection": cursor.connection}[target]
                    with pytest.raises(RuntimeError, match="fixture_write"):
                        getattr(handle, method)(sql)
                    assert connection.in_transaction
                raise ValueError("caller rollback")
        with fixture_write(store) as connection:
            assert connection.execute("SELECT * FROM synthetic_fixture").fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize("method", ["commit", "rollback", "close"])
def test_nested_connection_methods_preserve_pending_work(tmp_path, method):
    store = WorkspaceStore(tmp_path / "synthetic.sqlite")
    try:
        with fixture_write(store) as connection:
            connection.execute("CREATE TABLE synthetic_fixture (value TEXT)")
            connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
            with fixture_write(store) as nested:
                with pytest.raises(RuntimeError, match="fixture_write"):
                    getattr(nested, method)()  # fixture-write: Deliberately attempt nested transaction termination or closure and require rejection.
                assert connection.in_transaction
                assert nested.execute("SELECT value FROM synthetic_fixture").fetchone()[0] == "caller"
    finally:
        store.close()


def test_nested_fixture_keeps_existing_sqlite_authorizer(tmp_path):
    store = WorkspaceStore(tmp_path / "synthetic.sqlite")
    try:
        with fixture_write(store) as connection:
            connection.execute("CREATE TABLE synthetic_fixture (value TEXT)")
            connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
            connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE else sqlite3.SQLITE_OK)
            with fixture_write(store) as nested:
                with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                    nested.execute("DROP TABLE synthetic_fixture")
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                connection.execute("DROP TABLE synthetic_fixture")
            connection.set_authorizer(None)
    finally:
        store.close()


@pytest.mark.parametrize("attribute, value", [("isolation_level", None), ("autocommit", True)])
def test_nested_transaction_mode_changes_are_rejected(tmp_path, attribute, value):
    store = WorkspaceStore(tmp_path / "synthetic.sqlite")
    try:
        with fixture_write(store) as connection:
            connection.execute("CREATE TABLE synthetic_fixture (value TEXT)")
            connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
            with fixture_write(store) as nested:
                with pytest.raises(RuntimeError, match="fixture_write"):
                    setattr(nested, attribute, value)
                assert connection.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize("source", [
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.executescript('SELECT 1')",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.commit()",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.rollback()",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.execute('COMMIT')",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.isolation_level = None",
    "bench.workspace.connection.autocommit = True",
    "with fixture_write(bench.workspace) as connection:\n    deferred = (item for item in items if connection.execute('DELETE FROM segment'))\nlist(deferred)",
    "with fixture_write(bench.workspace) as connection:\n    deferred = (item for items in groups for item in connection.execute('DELETE FROM segment'))\nlist(deferred)",
    "with fixture_write(bench.workspace) as connection:\n    deferred = (connection.execute('DELETE FROM segment') for _ in items)  # fixture-write: A surrounding assignment cannot authorize deferred writes.\nlist(deferred)",
    "def seed(database):\n    execute = database.execute\n    execute('UPDATE source_version SET value=?', values)",
    "def finish(database):\n    database.commit()",
    "with fixture_write(bench.workspace) as connection:\n    pass\nconnection.execute(sql='DELETE FROM segment')",
    "def seed(database):\n    database.execute(sql='UPDATE source_version SET value=?', parameters=values)",
    "connection = bench.workspace.connection\nfinish = connection.rollback\nfinish()",
])
def test_guard_closes_alias_and_deferred_scope_bypasses(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize("source", [
    "with fixture_write(bench.workspace) as connection:\n    connection.executescript('SELECT 1')",
    "with fixture_write(bench.workspace) as connection:\n    connection.commit()",
    "with fixture_write(bench.workspace) as connection:\n    connection.rollback()",
    "with fixture_write(bench.workspace) as connection:\n    connection.execute('COMMIT')",
    "with fixture_write(bench.workspace) as connection:\n    deferred = (row for row in connection.execute('DELETE FROM segment RETURNING rowid'))",
    "with fixture_write(bench.workspace) as connection:\n    rows = [connection.execute('DELETE FROM segment') for _ in items]",
    "with fixture_write(bench.workspace) as connection:\n    connection.execute(\"CREATE TRIGGER synthetic AFTER INSERT ON synthetic_fixture BEGIN SELECT 1; END\")",
    "def inspect(connection):\n    return connection.execute('SELECT * FROM segment')",
    "bench.workspace.connection.rollback()  # fixture-write: Deliberately test an independent transaction rollback boundary.",
])
def test_guard_preserves_eager_and_guarded_operations(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("alias", [False, True])
def test_guard_rejects_raw_connection_context_inside_fixture_boundary(alias):
    source = "connection = bench.workspace.connection\n" if alias else ""
    receiver = "connection" if alias else "bench.workspace.connection"
    source += f"with fixture_write(bench.workspace):\n    with {receiver}:\n        pass"
    violations = check_fixture_writes(source)
    assert len(violations) == 1
    assert violations[0].rule == "connection context requires the yielded fixture_write handle"


@pytest.mark.parametrize("context", [
    "connection:",
    "bench.workspace.connection:  # fixture-write: Deliberately exercise the caller-owned raw transaction boundary.",
])
def test_guard_allows_yielded_context_or_exact_statement_exception(context):
    source = f"with fixture_write(bench.workspace) as connection:\n    with {context}\n        pass"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("target", ["nested", "outer"])
def test_nested_connection_context_cannot_commit_caller_writes(tmp_path, target):
    store = WorkspaceStore(tmp_path / "synthetic.sqlite")
    try:
        with fixture_write(store) as connection:
            connection.execute("CREATE TABLE synthetic_fixture (value TEXT)")
        with pytest.raises(ValueError, match="caller rollback"):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
                with fixture_write(store) as nested:
                    handle = nested if target == "nested" else connection
                    with pytest.raises(RuntimeError, match="fixture_write"):
                        with handle:
                            pass
                    assert connection.in_transaction
                    assert connection.execute("SELECT value FROM synthetic_fixture").fetchone()[0] == "caller"
                raise ValueError("caller rollback")
        with fixture_write(store) as connection:
            assert connection.execute("SELECT * FROM synthetic_fixture").fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize('sql', [
    "INSERT OR ROLLBACK INTO synthetic_fixture VALUES ('caller')",
    "INSERT OR /* synthetic */ ROLLBACK INTO synthetic_fixture VALUES ('caller')",
    "CREATE TABLE synthetic_conflict (value TEXT UNIQUE ON CONFLICT ROLLBACK)",
])
def test_nested_rollback_conflicts_preserve_caller_work(tmp_path, sql):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture (value TEXT UNIQUE)')
        with pytest.raises(ValueError, match='caller rollback'):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
                with fixture_write(store) as nested:
                    with pytest.raises(RuntimeError, match='fixture_write'):
                        nested.execute(sql)
                    assert connection.in_transaction
                    assert [r[0] for r in connection.execute('SELECT value FROM synthetic_fixture')] == ['caller']
                raise ValueError('caller rollback')
        with fixture_write(store) as connection:
            assert connection.execute('SELECT * FROM synthetic_fixture').fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize('arguments', ['seeded=', '*, seeded='])
def test_guard_checks_eager_lambda_defaults(arguments):
    source = f"callback = lambda {arguments}bench.workspace.connection.execute('DELETE FROM segment'): None"
    assert check_fixture_writes(source)
    assert check_fixture_writes('with fixture_write(bench.workspace):\n    ' + source)
    assert check_fixture_writes('with fixture_write(bench.workspace) as connection:\n    '
                                + source.replace('bench.workspace.connection', 'connection')) == []


@pytest.mark.parametrize('sql', ['PRAGMA user_version(12)', 'PRAGMA main.application_id(12)',
                                  'PRAGMA journal_mode(WAL)'])
def test_guard_rejects_writable_parenthesized_pragmas(sql):
    source = f'bench.workspace.connection.execute({sql!r})'
    assert check_fixture_writes(source)
    assert check_fixture_writes('with fixture_write(bench.workspace):\n    ' + source)
    assert check_fixture_writes('with fixture_write(bench.workspace) as connection:\n    ' + source.replace('bench.workspace.connection', 'connection')) == []


@pytest.mark.parametrize('sql', ['PRAGMA table_info(segment)', 'PRAGMA main.table_xinfo(segment)',
                                  'PRAGMA index_info(synthetic_index)', 'PRAGMA foreign_key_list(segment)',
                                  'PRAGMA integrity_check(1)', 'PRAGMA user_version'])
def test_guard_preserves_read_only_pragmas(sql):
    assert check_fixture_writes(f'bench.workspace.connection.execute({sql!r})') == []


@pytest.mark.parametrize('sql', ['PRAGMA main . user_version(12)', 'PRAGMA "main"."user_version"(12)',
                                  "PRAGMA 'user_version'(12)", 'PRAGMA [user_version](12)'])
def test_guard_checks_quoted_and_qualified_writable_pragmas(sql):
    assert check_fixture_writes(f'bench.workspace.connection.execute({sql!r})')


def test_guard_requires_yielded_handle_for_rollback_conflict_sql():
    source = "with fixture_write(bench.workspace):\n    bench.workspace.connection.execute(\"INSERT OR ROLLBACK INTO segment VALUES ('synthetic')\")"
    assert check_fixture_writes(source)


@pytest.mark.parametrize('schema', ['main', 'temp', 'synthetic_attached'])
@pytest.mark.parametrize('rollback_schema', ['constraint', 'trigger'])
def test_nested_fixture_rejects_existing_transaction_rollback_schema(tmp_path, rollback_schema, schema):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    table = f'{schema}.synthetic_fixture'
    try:
        with fixture_write(store) as connection:
            if schema == 'synthetic_attached':
                store.connection.execute("ATTACH ':memory:' AS synthetic_attached")  # fixture-write: Deliberately prepare a raw attached schema to test nested rollback-policy rejection.
            conflict = ' ON CONFLICT ROLLBACK' if rollback_schema == 'constraint' else ''
            connection.execute(f'CREATE TABLE {table} (value TEXT UNIQUE{conflict})')
            if rollback_schema == 'trigger':
                connection.execute(f"CREATE TRIGGER {schema}.synthetic_rollback BEFORE INSERT ON synthetic_fixture "
                                   "WHEN NEW.value = 'conflict' BEGIN SELECT RAISE(ROLLBACK, 'synthetic conflict'); END")
        with pytest.raises(ValueError, match='caller rollback'):
            with fixture_write(store) as connection:
                connection.execute(f"INSERT INTO {table} VALUES ('caller')")
                with pytest.raises(RuntimeError, match='fixture_write'):
                    with fixture_write(store) as nested:
                        value = 'caller' if rollback_schema == 'constraint' else 'conflict'
                        nested.execute(f'INSERT INTO {table} VALUES (?)', (value,))
                assert connection.in_transaction
                assert [r[0] for r in connection.execute(f'SELECT value FROM {table}')] == ['caller']
                raise ValueError('caller rollback')
        with fixture_write(store) as connection:
            assert connection.execute(f'SELECT * FROM {table}').fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize('sql', ['PRAGMA optimize', 'PRAGMA main.wal_checkpoint', 'PRAGMA incremental_vacuum'])
def test_guard_rejects_action_pragmas_without_arguments(sql):
    assert check_fixture_writes(f'bench.workspace.connection.execute({sql!r})')


def test_nested_fixture_checks_schema_attached_after_entry(tmp_path):
    external = tmp_path / 'synthetic-attached.sqlite'
    with sqlite3.connect(external) as independent:
        independent.execute('CREATE TABLE synthetic_conflict (value TEXT UNIQUE ON CONFLICT ROLLBACK)')
        independent.execute("INSERT INTO synthetic_conflict VALUES ('duplicate')")
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture (value TEXT)')
        with pytest.raises(ValueError, match='caller rollback'):
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
                with fixture_write(store) as nested:
                    store.connection.execute('ATTACH ? AS synthetic_external', (str(external),))  # fixture-write: Deliberately inject an external attachment after nested entry to exercise schema rechecking.
                    with pytest.raises(RuntimeError, match='fixture_write'):
                        nested.execute("INSERT INTO synthetic_external.synthetic_conflict VALUES ('duplicate')")
                    assert connection.in_transaction
                    # The fixture lock still protects this direct state read.
                    assert store.connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'caller'
                raise ValueError('caller rollback')
        with fixture_write(store) as connection:
            assert connection.execute('SELECT * FROM synthetic_fixture').fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize('source', [
    "@decorate(bench.workspace.connection.execute('DELETE FROM segment'))\ndef seed():\n    pass",
    "@decorate(bench.workspace.connection.execute('DELETE FROM segment'))\nasync def seed():\n    pass",
    "def seed(value: bench.workspace.connection.execute('DELETE FROM segment')):\n    pass",
    "def seed() -> bench.workspace.connection.execute('DELETE FROM segment'):\n    pass",
])
def test_guard_checks_eager_function_expressions(source):
    assert check_fixture_writes(source)
    assert check_fixture_writes('with fixture_write(bench.workspace):\n' + '\n'.join('    ' + line for line in source.splitlines()))
    scoped = source.replace('bench.workspace.connection', 'connection')
    assert check_fixture_writes('with fixture_write(bench.workspace) as connection:\n'
                                + '\n'.join('    ' + line for line in scoped.splitlines())) == []


def test_guard_preserves_postponed_function_annotations():
    source = "from __future__ import annotations\ndef seed(value: bench.workspace.connection.execute('DELETE FROM segment')):\n    pass"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('expression', [
    "[connection.execute('DELETE FROM segment') for connection in connections]",
    "{connection.execute('DELETE FROM segment') for connection in connections}",
    "{index: connection.execute('DELETE FROM segment') for index, connection in connections}",
])
def test_guard_does_not_inherit_independent_alias_for_comprehension_target(expression):
    source = "import sqlite3\nconnection = sqlite3.connect(':memory:')\n" + expression
    assert check_fixture_writes(source)
