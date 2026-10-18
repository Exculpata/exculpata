"""Regressions for deferred aliases and equivalent SQLite mutation forms."""
from textwrap import dedent

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize("source", [
    "def use():\n    with shared:\n        pass\nshared = bench.workspace.connection\nuse()",
    "def outer():\n    def use():\n        with shared:\n            pass\n    shared = bench.workspace.connection\n    use()",
    "import sqlite3\nshared = sqlite3.connect(':memory:')\ndef use():\n    with shared:\n        pass\nshared = bench.workspace.connection\nuse()\nshared = sqlite3.connect(':memory:')",
    "use = lambda: shared.close()\nshared = bench.workspace.connection\nuse()",
    "work = (shared.close() for _ in items)\nshared = bench.workspace.connection\nlist(work)",
    "def use():\n    setter(None)\nsetter = bench.workspace.connection.set_authorizer\nuse()",
    "class Synthetic:\n    def use(self):\n        with shared:\n            pass\nshared = bench.workspace.connection\nSynthetic().use()",
    "callbacks = ((lambda: shared.close()) for _ in items)\nshared = bench.workspace.connection\nnext(callbacks)()",
    "callbacks = ((shared.close() for inner in items) for outer in items)\nshared = bench.workspace.connection\nlist(next(callbacks))",
    "import sqlite3\nshared = sqlite3.connect(':memory:')\ncallbacks = [lambda: shared.close() for shared in [bench.workspace.connection]]\ncallbacks[0]()",
])
def test_late_global_and_closure_connection_aliases_are_checked(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize("source", [
    "import sqlite3\ndef use():\n    with shared:\n        pass\nshared = sqlite3.connect(':memory:')\nuse()",
    "import sqlite3\nshared = sqlite3.connect(':memory:')\ndef use(saved=shared):\n    with saved:\n        pass\nshared = bench.workspace.connection\nuse()",
    "import sqlite3\nshared = sqlite3.connect(':memory:')\nuse = lambda saved=shared: saved.close()\nshared = bench.workspace.connection\nuse()",
    "def use(store):\n    with fixture_write(store) as connection:\n        connection.execute('DELETE FROM workbench_matter')",
    "def use():\n    shared = sqlite3.connect(':memory:')\n    with shared:\n        pass\nshared = bench.workspace.connection\nuse()",
    "callbacks = ((lambda: shared.close()) for shared in non_sql_resources)\nshared = bench.workspace.connection",
    "import sqlite3\nshared = bench.workspace.connection\ncallbacks = [lambda: shared.close() for shared in [sqlite3.connect(':memory:')]]\ncallbacks[0]()",
])
def test_late_alias_check_preserves_defaults_local_shadowing_and_independent_controls(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("attribute", ["row_factory", "isolation_level", "autocommit"])
@pytest.mark.parametrize("setter", ["setattr", "assign"])
def test_constant_setattr_requires_same_boundary_as_assignment(attribute, setter):
    source = ("assign = setattr\n" if setter == "assign" else "")
    source += f"name = '{attribute}'\n{setter}(bench.workspace.connection, name, value)"
    assert check_fixture_writes(source)


@pytest.mark.parametrize("attribute", ["row_factory", "isolation_level", "autocommit"])
def test_setattr_accepts_yielded_handle_and_independent_database(attribute):
    assert check_fixture_writes(f"with fixture_write(bench.workspace) as connection:\n    setattr(connection, '{attribute}', value)") == []
    assert check_fixture_writes(f"import sqlite3\nconnection = sqlite3.connect(':memory:')\nsetattr(connection, '{attribute}', value)") == []


def test_setattr_row_factory_under_owner_lock_does_not_allow_raw_cursor_factory():
    assert check_fixture_writes("with bench.workspace._lock:\n    setattr(bench.workspace.connection, 'row_factory', callback)") == []
    assert check_fixture_writes("with bench.workspace._lock:\n    setattr(bench.workspace.connection.cursor(), 'row_factory', callback)")


@pytest.mark.parametrize("method, args", [
    ("set_trace_callback", "None"), ("set_authorizer", "None"),
    ("set_progress_handler", "None, 0"), ("create_function", "'synthetic', 0, None"),
    ("create_aggregate", "'synthetic', 0, None"), ("create_collation", "'synthetic', None"),
    ("create_window_function", "'synthetic', 0, None"),
])
def test_unannotated_callback_handle_requires_proven_ownership(method, args):
    source = f"def install(handle):\n    mutate = handle.{method}\n    mutate({args})\ninstall(bench.workspace.connection)"
    assert check_fixture_writes(source)


@pytest.mark.parametrize("source", [
    "def install(handle):\n    handle.set_label('synthetic')",
    "def install(handle: EventSource):\n    handle.set_trace_callback(None)",
    "import sqlite3\ndef install(handle=sqlite3.connect(':memory:')):\n    handle.set_authorizer(None)",
    "def install(handle):\n    handle = sqlite3.connect(':memory:')\n    handle.set_authorizer(None)",
    "def install(store):\n    with store._lock:\n        store.connection.set_authorizer(None)",
])
def test_callback_parameter_rule_preserves_independent_and_non_sql_helpers(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("method, args", [
    ("commit", ""), ("rollback", ""), ("close", ""),
    ("execute", ", 'UPDATE workbench_matter SET display_name=?', ('Synthetic',)"),
    ("executemany", ", 'DELETE FROM workbench_matter WHERE matter_id=?', []"),
    ("executescript", ", 'DELETE FROM workbench_matter;'"),
    ("set_trace_callback", ", None"), ("set_authorizer", ", None"),
    ("set_progress_handler", ", None, 0"), ("create_function", ", 'synthetic', 0, None"),
])
@pytest.mark.parametrize("aliased", [False, True])
def test_unbound_sqlite_descriptor_uses_first_argument_as_receiver(method, args, aliased):
    source = "from sqlite3 import Connection as Database\n"
    if aliased:
        source += f"mutate = Database.{method}\nmutate(bench.workspace.connection{args})"
    else:
        source += f"Database.{method}(bench.workspace.connection{args})"
    assert check_fixture_writes(source)


@pytest.mark.parametrize("source", [
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nsqlite3.Connection.close(connection)",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nsqlite3.Connection.commit(connection)",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nsqlite3.Connection.set_authorizer(connection, None)",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nsqlite3.Connection.execute(connection, 'DELETE FROM synthetic')",
    "import sqlite3\nsqlite3.Connection.execute(bench.workspace.connection, 'SELECT 1')",
    "import sqlite3\nwith bench.workspace._lock:\n    sqlite3.Connection.set_trace_callback(bench.workspace.connection, None)",
    "def use(resource):\n    resource.close()",
])
def test_descriptor_rule_preserves_reads_independent_control_and_product_close(source):
    assert check_fixture_writes(source) == []


def test_setattr_exact_exception_does_not_cover_a_neighbor():
    source = dedent("""\
        setattr(bench.workspace.connection, 'row_factory', callback)  # fixture-write: Deliberately install raw configuration in an isolated negative regression.
        setattr(bench.workspace.connection, 'row_factory', callback)
    """)
    assert [item.line for item in check_fixture_writes(source)] == [2]
