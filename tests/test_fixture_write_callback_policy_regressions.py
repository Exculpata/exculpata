"""Synthetic policy regressions for connection configuration and alias scopes."""

from textwrap import dedent

import pytest

from tests.fixture_write_policy import check_fixture_writes


CALLBACKS = [
    ("set_trace_callback", "None"),
    ("set_authorizer", "None"),
    ("set_progress_handler", "None, 0"),
    ("create_function", "'synthetic', 0, None"),
    ("create_aggregate", "'synthetic', 0, None"),
    ("create_collation", "'synthetic', None"),
    ("create_window_function", "'synthetic', 0, None"),
]


@pytest.mark.parametrize("source", [
    "bench.workspace.connection.close()",
    "connection = bench.workspace.connection\nclose = connection.close\nclose()",
    "getattr(bench.workspace.connection, 'close')()",
    "with fixture_write(bench.workspace) as connection:\n    connection.close()",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.close()",
    "bench.workspace.connection.cursor().connection.close()",
])
def test_guard_rejects_closing_shared_connection(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize("source", [
    "bench.workspace.close()",
    "store = WorkspaceStore(path)\nclose = store.close\nclose()",
    "cursor = bench.workspace.connection.cursor()\ncursor.close()",
    "bench.workspace.connection.execute('SELECT 1').close()",
    "with fixture_write(bench.workspace) as connection:\n    cursor = connection.cursor()\n    close = cursor.close\n    close()",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nconnection.close()",
])
def test_guard_preserves_product_cursor_and_independent_close(source):
    assert check_fixture_writes(source) == []


def test_cursor_marker_preserves_owner_when_merging_scoped_fixture_handles():
    source = "with fixture_write(store) as connection:\n    handle = connection.cursor() if cursor_mode else connection\n    handle.execute(statement)"
    assert check_fixture_writes(source) == []


def test_possible_connection_close_is_not_hidden_by_cursor_alternative():
    source = "with fixture_write(store) as connection:\n    handle = connection.cursor() if cursor_mode else connection\n    handle.close()"
    assert check_fixture_writes(source)


@pytest.mark.parametrize("method, arguments", CALLBACKS)
@pytest.mark.parametrize("alias", [False, True])
def test_guard_rejects_unlocked_connection_callback_changes(method, arguments, alias):
    source = f"connection = bench.workspace.connection\n"
    source += (f"configure = connection.{method}\nconfigure({arguments})" if alias
               else f"connection.{method}({arguments})")
    assert check_fixture_writes(source)


@pytest.mark.parametrize("method, arguments", CALLBACKS)
@pytest.mark.parametrize("boundary", ["with workspace._lock:", "with fixture_write(workspace):"])
def test_callback_changes_accept_exact_owning_lock_or_fixture(method, arguments, boundary):
    source = f"workspace = bench.workspace\nconfigure = workspace.connection.{method}\n{boundary}\n    configure({arguments})\nclient.get('/synthetic')"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("method, arguments", CALLBACKS)
def test_callback_changes_accept_independent_sqlite_control(method, arguments):
    source = f"import sqlite3\nconnection = sqlite3.connect(':memory:')\nconnection.{method}({arguments})"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("source", [
    "with other.workspace._lock:\n    bench.workspace.connection.set_authorizer(None)",
    "with bench.workspace._lock:\n    pass\nbench.workspace.connection.set_trace_callback(None)",
    "with bench.workspace._lock:\n    def later():\n        bench.workspace.connection.set_trace_callback(None)",
    "with bench.workspace._lock:\n    later = lambda: bench.workspace.connection.set_trace_callback(None)",
    "with bench.workspace._lock:\n    later = (bench.workspace.connection.set_trace_callback(None) for _ in items)",
])
def test_callback_lock_does_not_cover_other_connections_or_deferred_scopes(source):
    assert check_fixture_writes(source)


def test_callback_lock_does_not_grant_sql_transaction_permission():
    assert check_fixture_writes("with bench.workspace._lock:\n    bench.workspace.connection.execute('DELETE FROM workbench_matter')")


@pytest.mark.parametrize("source", [
    "bench.workspace.connection.row_factory = callback",
    "connection = bench.workspace.connection\nconnection.row_factory = callback",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.row_factory = callback",
    "with bench.workspace._lock:\n    with fixture_write(bench.workspace):\n        bench.workspace.connection.row_factory = callback",
    "with other._lock:\n    bench.workspace.connection.row_factory = callback",
])
def test_row_factory_changes_require_scoped_handle_or_exact_lock(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize("source", [
    "with bench.workspace._lock:\n    bench.workspace.connection.row_factory = callback",
    "with fixture_write(bench.workspace) as connection:\n    connection.row_factory = callback",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nconnection.row_factory = callback",
])
def test_row_factory_changes_accept_locked_or_independent_setup(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("expression", [
    "[(connection := bench.workspace.connection) for item in items]",
    "{(connection := bench.workspace.connection) for item in items}",
    "{item: (connection := bench.workspace.connection) for item in items}",
    "{(connection := bench.workspace.connection): item for item in items}",
    "[item for item in items if (connection := bench.workspace.connection)]",
    "[[(connection := bench.workspace.connection) for inner in items] for item in items]",
    "[(lambda saved=(connection := bench.workspace.connection): saved) for item in items]",
    "[item for item in items if (connection := bench.workspace.connection) if (connection := sqlite3.connect(':memory:'))]",
])
def test_eager_comprehension_walrus_retains_containing_connection_provenance(expression):
    source = f"import sqlite3\nconnection = sqlite3.connect(':memory:')\n{expression}\nwith connection:\n    pass"
    assert check_fixture_writes(source)


def test_zero_iterations_cannot_erase_existing_connection_provenance():
    source = "import sqlite3\nconnection = bench.workspace.connection\n[(connection := sqlite3.connect(':memory:')) for item in items]\nwith connection:\n    pass"
    assert check_fixture_writes(source)


@pytest.mark.parametrize("expression", [
    "[connection for connection in [bench.workspace.connection]]",
    "{connection for connection in [bench.workspace.connection]}",
    "{connection: None for connection in [bench.workspace.connection]}",
    "[(lambda: (connection := bench.workspace.connection)) for item in items]",
    "((connection := bench.workspace.connection) for item in items)",
])
def test_comprehension_iteration_names_and_deferred_walrus_do_not_leak(expression):
    source = f"import sqlite3\nconnection = sqlite3.connect(':memory:')\n{expression}\nwith connection:\n    pass"
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize("name", ["connection", "conn", "db", "database"])
def test_connection_like_parameter_cannot_open_naked_transaction(name):
    source = f"def use({name}):\n    with {name}:\n        pass\nuse(bench.workspace.connection)"
    assert check_fixture_writes(source)


@pytest.mark.parametrize("source", [
    "import sqlite3\ndef use(connection=sqlite3.connect(':memory:')):\n    with connection:\n        pass",
    "def use(connection):\n    connection = sqlite3.connect(':memory:')\n    with connection:\n        pass",
    "def use(store):\n    with fixture_write(store) as connection:\n        with connection:\n            pass",
    "import socket\ndef use(connection: socket.socket):\n    with connection:\n        pass",
    "def use(resource):\n    with resource:\n        pass",
])
def test_parameter_rule_preserves_proven_independent_and_non_sql_contexts(source):
    assert check_fixture_writes(source) == []


def test_exact_exception_allows_deliberate_close_negative_without_covering_next_call():
    source = dedent("""\
        connection = bench.workspace.connection
        connection.close()  # fixture-write: Deliberately exercise the shared connection close rejection.
        connection.close()
    """)
    assert [violation.line for violation in check_fixture_writes(source)] == [3]
