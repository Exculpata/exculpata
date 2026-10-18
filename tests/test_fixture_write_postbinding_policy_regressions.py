"""SQLite prepare-time mutations and explicit argument-expansion regressions."""
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('sql', [
    'EXPLAIN PRAGMA foreign_keys=OFF',
    'EXPLAIN QUERY PLAN PRAGMA foreign_keys=OFF',
    'EXPLAIN /* synthetic */ PRAGMA main.foreign_keys(OFF)',
    'EXPLAIN PRAGMA "main" . [foreign_keys] = OFF',
    'EXPLAIN PRAGMA optimize',
])
def test_explain_pragma_mutations_require_scoped_boundary(sql):
    assert check_fixture_writes(f'bench.workspace.connection.execute({sql!r})')
    assert check_fixture_writes(f'with fixture_write(bench.workspace):\n    bench.workspace.connection.execute({sql!r})')


@pytest.mark.parametrize('prefix', ['EXPLAIN', 'EXPLAIN QUERY PLAN'])
def test_explain_pragma_really_changes_sqlite_configuration_at_prepare_time(prefix):
    raw = sqlite3.connect(':memory:')
    try:
        raw.execute('PRAGMA foreign_keys=ON')
        assert raw.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        statement = f'{prefix} PRAGMA foreign_keys=OFF'
        # Preparing the EXPLAIN changes this connection, without stepping rows.
        # fixture-write: Independent in-memory connection demonstrates SQLite prepare-time configuration mutation.
        prepared = raw.execute(statement)
        assert raw.execute('PRAGMA foreign_keys').fetchone()[0] == 0
        prepared.close()
        assert check_fixture_writes(f'bench.workspace.connection.execute({statement!r})')
    finally:
        raw.close()


@pytest.mark.parametrize('sql', [
    'EXPLAIN SELECT 1', 'EXPLAIN QUERY PLAN SELECT 1',
    'EXPLAIN PRAGMA foreign_keys', 'EXPLAIN QUERY PLAN PRAGMA foreign_keys',
    'EXPLAIN PRAGMA table_info(synthetic)',
    'EXPLAIN DELETE FROM workbench_synthetic',
    "SELECT 'EXPLAIN PRAGMA foreign_keys=OFF'",
])
def test_explain_and_pragma_read_controls(sql):
    assert check_fixture_writes(f'bench.workspace.connection.execute({sql!r})') == []


@pytest.mark.parametrize('arguments', [
    '*(bench.workspace.connection,)',
    '*[bench.workspace.connection]',
    "**{'handle': bench.workspace.connection}",
    "*(), **{'handle': bench.workspace.connection}",
])
@pytest.mark.parametrize('helper', [
    'def dispose(handle):\n    handle.close()',
    'dispose = lambda handle: handle.close()',
    'def dispose(handle):\n    finish = handle.close\n    finish()',
])
def test_known_argument_expansions_retain_shared_close_provenance(helper, arguments):
    assert check_fixture_writes(f'{helper}\ndispose({arguments})')


@pytest.mark.parametrize('assignment, call', [
    ('args = (bench.workspace.connection,)', 'dispose(*args)'),
    ('args = [bench.workspace.connection]', 'dispose(*args)'),
    ("kwargs = {'handle': bench.workspace.connection}", 'dispose(**kwargs)'),
    ("kwargs = {'handle': bench.workspace.connection}\naliased = kwargs", 'dispose(**aliased)'),
])
def test_known_expansion_aliases_retain_shared_close_provenance(assignment, call):
    assert check_fixture_writes(f'def dispose(handle):\n    handle.close()\n{assignment}\n{call}')


@pytest.mark.parametrize('receiver', ['resource', 'bench.workspace', 'bench.workspace.connection.cursor()', "sqlite3.connect(':memory:')"])
@pytest.mark.parametrize('arguments', ['*({receiver},)', "**{{'handle': {receiver}}}"])
def test_expansion_controls_preserve_nonshared_close(receiver, arguments):
    source = f'import sqlite3\ndef dispose(handle):\n    handle.close()\ndispose({arguments.format(receiver=receiver)})'
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('mutation', [
    'workspace.connection.text_factory = bytes',
    "setattr(workspace.connection, 'text_factory', bytes)",
    'sqlite3.Connection.text_factory.__set__(workspace.connection, bytes)',
    'assign = sqlite3.Connection.text_factory.__set__\nassign(workspace.connection, bytes)',
])
def test_text_factory_changes_require_owning_boundary_and_change_query_behavior(mutation):
    raw = sqlite3.connect(':memory:')
    workspace = SimpleNamespace(connection=raw, _lock=RLock())
    try:
        assert raw.execute("SELECT 'synthetic'").fetchone()[0] == 'synthetic'
        source = 'import sqlite3\n' + mutation
        exec(source, {'workspace': workspace})
        assert raw.execute("SELECT 'synthetic'").fetchone()[0] == b'synthetic'
        assert check_fixture_writes(source)
    finally:
        raw.close()


@pytest.mark.parametrize('mutation', [
    'workspace.connection.text_factory = bytes',
    "setattr(workspace.connection, 'text_factory', bytes)",
    'sqlite3.Connection.text_factory.__set__(workspace.connection, bytes)',
])
def test_text_factory_owner_lock_is_sufficient_for_serialized_configuration(mutation):
    assert check_fixture_writes('import sqlite3\nwith workspace._lock:\n    ' + mutation) == []
    assert check_fixture_writes('import sqlite3\nwith other._lock:\n    ' + mutation)


def test_text_factory_scoped_and_independent_controls():
    assert check_fixture_writes('with fixture_write(workspace) as connection:\n    connection.text_factory = bytes') == []
    assert check_fixture_writes("import sqlite3\nconnection = sqlite3.connect(':memory:')\nconnection.text_factory = bytes") == []
    assert check_fixture_writes('resource.text_factory = bytes') == []


def test_text_factory_scoped_assignment_and_explicit_restoration_change_actual_query_behavior():
    raw = sqlite3.connect(':memory:')
    workspace = SimpleNamespace(connection=raw, _lock=RLock())
    try:
        with fixture_write(workspace) as connection:
            connection.text_factory = bytes
            assert connection.execute("SELECT 'synthetic'").fetchone()[0] == b'synthetic'
            connection.text_factory = str
        assert raw.execute("SELECT 'synthetic'").fetchone()[0] == 'synthetic'
    finally:
        raw.close()


def test_unknown_explain_target_cannot_claim_a_read_only_pragma():
    assert check_fixture_writes('bench.workspace.connection.execute(f"EXPLAIN {statement}")')


def test_literal_expansion_composition_and_branch_aliases_preserve_provenance():
    assert check_fixture_writes("def dispose(handle):\n    handle.close()\nargs = (bench.workspace.connection,)\ndispose(*[*args])")
    assert check_fixture_writes("def dispose(handle):\n    handle.close()\nkwargs = {'handle': bench.workspace.connection}\ndispose(**{**kwargs})")
    assert check_fixture_writes("def dispose(handle):\n    handle.close()\nif choice:\n    kwargs = {'handle': bench.workspace.connection}\nelse:\n    kwargs = {'handle': resource}\ndispose(**kwargs)")
