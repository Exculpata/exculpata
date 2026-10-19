"""SQLite statement preparation can change state; shared callable handles reject."""
import sqlite3
from types import SimpleNamespace

import pytest

from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('operation', [
    'bench.workspace.connection(SQL)',
    'handle = bench.workspace.connection\nhandle(SQL)',
    'bench.workspace.connection.__call__(SQL)',
    "getattr(bench.workspace.connection, '__call__')(SQL)",
    'operation = bench.workspace.connection.__call__\noperation(SQL)',
    'sqlite3.Connection.__call__(bench.workspace.connection, SQL)',
    'operation = sqlite3.Connection.__call__\noperation(bench.workspace.connection, SQL)',
    'def invoke(handle):\n    handle(SQL)\ninvoke(bench.workspace.connection)',
    'def expose():\n    return bench.workspace.connection\nexpose()(SQL)',
])
@pytest.mark.parametrize('scope', ['outside', 'lock', 'fixture'])
def test_shared_callable_connection_is_explicitly_unsupported(operation, scope):
    operation = operation.replace('SQL', '"PRAGMA foreign_keys=OFF"')
    if scope != 'outside':
        context = 'bench.workspace._lock' if scope == 'lock' else 'fixture_write(bench.workspace)'
        operation = f'with {context}:\n' + '\n'.join('    ' + line for line in operation.splitlines())
    violations = check_fixture_writes('import sqlite3\n' + operation)
    assert any('unsupported' in v.rule for v in violations), violations


@pytest.mark.parametrize('source', [
    'independent("PRAGMA foreign_keys=OFF")',
    'independent.__call__("PRAGMA foreign_keys=OFF")',
    'sqlite3.Connection.__call__(independent, "PRAGMA foreign_keys=OFF")',
    'resource(synthetic)',
    'resource.__call__(synthetic)',
    'with fixture_write(bench.workspace) as handle:\n    handle.execute("INSERT INTO synthetic_fixture VALUES (1)")',
    'bench.workspace.connection.execute("SELECT 1")',
])
def test_callable_rule_preserves_independent_generic_and_ordinary_sql_controls(source):
    assert check_fixture_writes("import sqlite3\nindependent = sqlite3.connect(':memory:')\n" + source) == []


@pytest.mark.parametrize('operation', [
    'bench.workspace.connection("PRAGMA foreign_keys=OFF")',
    'sqlite3.Connection.__call__(bench.workspace.connection, "PRAGMA foreign_keys=OFF")',
])
def test_callable_connection_actually_changes_pragma_before_inventory_rejects(operation):
    raw = sqlite3.connect(':memory:')
    bench = SimpleNamespace(workspace=SimpleNamespace(connection=raw))
    try:
        raw.execute('PRAGMA foreign_keys=ON')
        assert raw.execute('PRAGMA foreign_keys').fetchone() == (1,)
        source = 'import sqlite3\n' + operation
        exec(source, {'bench': bench})
        assert raw.execute('PRAGMA foreign_keys').fetchone() == (0,)
        assert check_fixture_writes(source)
    finally:
        raw.close()
