"""Synthetic regressions for the four fixture-boundary review findings."""
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
                    getattr(nested, method)()
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
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.execute(\"CREATE TRIGGER synthetic AFTER INSERT ON synthetic_fixture BEGIN SELECT 1; END\")",
    "def inspect(connection):\n    return connection.execute('SELECT * FROM segment')",
    "bench.workspace.connection.rollback()  # fixture-write: Deliberately test an independent transaction rollback boundary.",
])
def test_guard_preserves_eager_and_guarded_operations(source):
    assert check_fixture_writes(source) == []
