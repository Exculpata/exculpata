"""Synthetic transaction and lexical scope regressions for fixture boundaries."""
from textwrap import dedent

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('outer_setup', ['none', 'read', 'ddl'])
def test_logical_nested_fixture_preserves_outer_rollback_before_dml(tmp_path, outer_setup):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture (value TEXT)')
        with pytest.raises(ValueError, match='outer rollback'):
            with fixture_write(store) as connection:
                if outer_setup == 'read':
                    connection.execute('SELECT * FROM synthetic_fixture').fetchall()
                elif outer_setup == 'ddl':
                    connection.execute('CREATE TABLE synthetic_other (value TEXT)')
                assert not connection.in_transaction
                with fixture_write(store) as nested:
                    nested.execute("INSERT INTO synthetic_fixture VALUES ('inner')")
                raise ValueError('outer rollback')
        with fixture_write(store) as connection:
            assert connection.execute('SELECT * FROM synthetic_fixture').fetchall() == []
    finally:
        store.close()


@pytest.mark.parametrize('use', [
    "connection.execute('DELETE FROM segment')",
    "def mutate():\n    connection.execute('DELETE FROM segment')",
    "mutate = lambda: connection.execute('DELETE FROM segment')",
    "rows = [connection.execute('DELETE FROM segment') for _ in items]",
])
def test_class_assignments_do_not_replace_lexical_connection_alias(use):
    source = "import sqlite3\nconnection = bench.workspace.connection\nclass Synthetic:\n    connection = sqlite3.connect(':memory:')\n"
    if use.startswith('connection.'):
        source += use
    else:
        source += '\n'.join('    ' + line for line in use.splitlines())
    assert check_fixture_writes(source)


@pytest.mark.parametrize('path', ['bench.workspace.path', 'str(bench.workspace.path)', "'workbench.sqlite'", "f'{bench.workspace.path}'", 'pathlib.Path(bench.workspace.path)', 'os.fspath(bench.workspace.path)', 'bench.workspace.path.as_posix()'])
@pytest.mark.parametrize('operation', ["connection.execute('DELETE FROM segment')", 'connection.commit()', 'with connection:\n    pass'])
def test_exempt_connect_does_not_exempt_later_workspace_mutations(path, operation):
    source = f"import sqlite3\nimport pathlib\nimport os\nconnection = sqlite3.connect({path})  # fixture-write: Open an intentional independent actor against the synthetic workspace.\n{operation}"
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "import sqlite3\nclass Synthetic:\n    connection = sqlite3.connect(':memory:')\n    connection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = bench.workspace.connection\nclass Synthetic:\n    connection = sqlite3.connect(':memory:')\n    def mutate(connection=connection):\n        connection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nclass Synthetic:\n    connection = bench.workspace.connection\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = sqlite3.connect('synthetic.sqlite')\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = sqlite3.connect(bench.workspace.path)  # fixture-write: Open the independent actor for a deliberate concurrency test.\nconnection.execute('DELETE FROM segment')  # fixture-write: Deliberately mutate through the independent actor to exercise contention.",
])
def test_guard_preserves_class_local_defaults_and_independent_connections(source):
    assert check_fixture_writes(source) == []


def test_exempt_workspace_connection_context_does_not_exempt_its_body():
    source = dedent('''\
        import sqlite3
        with sqlite3.connect(database=bench.workspace.path) as connection:  # fixture-write: Open the independent actor for a deliberate concurrency test.
            connection.execute('DELETE FROM segment')
    ''')
    violations = check_fixture_writes(source)
    assert [(v.line, v.rule) for v in violations] == [(3, 'SQL fixture write outside fixture_write')]


def test_logical_nesting_is_per_connection_and_resets_after_rollback(tmp_path):
    first = WorkspaceStore(tmp_path / 'synthetic-first.sqlite')
    second = WorkspaceStore(tmp_path / 'synthetic-second.sqlite')
    try:
        for store in (first, second):
            with fixture_write(store) as connection:
                connection.execute('CREATE TABLE synthetic_fixture (value TEXT)')
        with pytest.raises(ValueError, match='outer rollback'):
            with fixture_write(first):
                with fixture_write(second) as connection:
                    connection.execute("INSERT INTO synthetic_fixture VALUES ('independent')")
                with fixture_write(first) as nested:
                    nested.execute("INSERT INTO synthetic_fixture VALUES ('rolled back')")
                raise ValueError('outer rollback')
        with fixture_write(second) as connection:
            assert connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'independent'
        with fixture_write(first) as connection:
            assert connection.execute('SELECT * FROM synthetic_fixture').fetchall() == []
            connection.executescript("INSERT INTO synthetic_fixture VALUES ('new outer');")
        with fixture_write(first) as connection:
            assert connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'new outer'
    finally:
        first.close()
        second.close()


def test_copied_context_does_not_retain_a_finished_fixture_owner(tmp_path):
    from contextvars import copy_context

    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            connection.execute('CREATE TABLE synthetic_fixture (value TEXT)')
            copied = copy_context()

        def later_fixture():
            with fixture_write(store) as connection:
                connection.execute("INSERT INTO synthetic_fixture VALUES ('later')")

        copied.run(later_fixture)
        with fixture_write(store) as connection:
            assert not connection.in_transaction
            assert connection.execute('SELECT value FROM synthetic_fixture').fetchone()[0] == 'later'
    finally:
        store.close()


@pytest.mark.parametrize('source', [
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nclass Synthetic:\n    global connection\n    connection = bench.workspace.connection\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\ndef seed():\n    connection = sqlite3.connect(':memory:')\n    class Synthetic:\n        nonlocal connection\n        connection = bench.workspace.connection\n    connection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = bench.workspace.connection\nclass Synthetic:\n    connection = sqlite3.connect(':memory:')\n    del connection\n    connection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nfor connection in connections:\n    connection.execute('DELETE FROM segment')",
    "import sqlite3\nif choose_live:\n    connection = bench.workspace.connection\nelse:\n    connection = sqlite3.connect(':memory:')\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = bench.workspace.connection\nfor item in items:\n    connection = sqlite3.connect(':memory:')\nconnection.execute('DELETE FROM segment')",
])
def test_guard_keeps_live_aliases_conservative_across_rebinding(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('setup, argument', [
    ("path = ''\npath += str(bench.workspace.path)\n", 'path'),
    ('', '(path := bench.workspace.path)'),
])
def test_guard_preserves_workspace_path_rebinding(setup, argument):
    source = f"import sqlite3\n{setup}connection = sqlite3.connect({argument})  # fixture-write: Open the independent actor for a deliberate concurrency test.\nconnection.execute('DELETE FROM segment')"
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "import sqlite3\nconnection = sqlite3.connect(bench.workspace.path if selected else ':memory:')  # fixture-write: Open the independent actor for a deliberate concurrency test.\nconnection.execute('DELETE FROM segment')",
    "with fixture_write(bench.workspace):\n    bench.workspace.connection.execute(dynamic_sql)",
    "import sqlite3\nconnection = bench.workspace.connection\nwhile items:\n    connection = sqlite3.connect(':memory:')\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\ntry:\n    connection = bench.workspace.connection\n    could_raise()\n    connection = sqlite3.connect(':memory:')\nexcept RuntimeError:\n    connection.execute('DELETE FROM segment')",
])
def test_guard_keeps_conditional_provenance_and_dynamic_sql_conservative(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "import sqlite3\nconnection = sqlite3.connect('workbench.sqlite' if selected else ':memory:')  # fixture-write: Open the independent actor for a deliberate concurrency test.\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = sqlite3.connect(bench.workspace.path or ':memory:')  # fixture-write: Open the independent actor for a deliberate concurrency test.\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = sqlite3.connect(':memory:')\nunused = (connection := bench.workspace.connection) if selected else (connection := sqlite3.connect(':memory:'))\nconnection.execute('DELETE FROM segment')",
    "import sqlite3\nconnection = bench.workspace.connection\nunused = selected and (connection := sqlite3.connect(':memory:'))\nconnection.execute('DELETE FROM segment')",
])
def test_guard_short_circuit_scope_keeps_workspace_provenance(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "value: bench.workspace.connection.execute('DELETE FROM segment')",
    "record.value: bench.workspace.connection.execute('DELETE FROM segment')",
    "class Synthetic:\n    value: bench.workspace.connection.execute('DELETE FROM segment')",
])
def test_guard_checks_eager_module_and_class_annotations(source):
    assert check_fixture_writes(source)


@pytest.mark.parametrize('source', [
    "def seed():\n    value: bench.workspace.connection.execute('DELETE FROM segment')",
    "from __future__ import annotations\nclass Synthetic:\n    value: bench.workspace.connection.execute('DELETE FROM segment')",
])
def test_guard_preserves_unevaluated_variable_annotations(source):
    assert check_fixture_writes(source) == []
