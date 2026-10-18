"""Raw cursor factories must not leak the workspace connection past its lock."""
import sqlite3

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write
from tests.fixture_write_policy import check_fixture_writes


@pytest.mark.parametrize('cursor_expression', [
    'workspace.connection.cursor()',
    "workspace.connection.execute('SELECT 1')",
])
@pytest.mark.parametrize('boundary', [
    'with fixture_write(workspace):',
    'with workspace._lock:',
])
def test_raw_cursor_factory_escape_is_inventoried(tmp_path, cursor_expression, boundary):
    source = (
        f'{boundary}\n'
        f'    cursor = {cursor_expression}\n'
        '    cursor.row_factory = lambda current, row: current.connection\n'
        "    leaked = cursor.execute('SELECT 1').fetchone()\n"
    )
    workspace = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        namespace = {'workspace': workspace, 'fixture_write': fixture_write}
        exec(source, namespace)
        # This raw SQLite path really escapes the boundary; the inventory must
        # reject its source rather than depend on the eventual consumer's name.
        assert namespace['leaked'] is workspace.connection
        assert [v.line for v in check_fixture_writes(source)] == (
            [2, 3] if boundary == 'with fixture_write(workspace):' else [3]
        )
    finally:
        workspace.close()


@pytest.mark.parametrize('assignment', [
    'cursor.row_factory = callback',
    "setattr(cursor, 'row_factory', callback)",
])
def test_shared_cursor_factory_requires_yielded_cursor(assignment):
    assert check_fixture_writes(
        'cursor = bench.workspace.connection.cursor()\n' + assignment
    )
    assert check_fixture_writes(
        'with fixture_write(bench.workspace):\n'
        '    cursor = bench.workspace.connection.cursor()\n'
        f'    {assignment}'
    )


@pytest.mark.parametrize('source', [
    'with fixture_write(workspace) as connection:\n'
    '    cursor = connection.cursor()\n'
    '    cursor.row_factory = callback',
    'import sqlite3\n'
    "connection = sqlite3.connect(':memory:')\n"
    'cursor = connection.cursor()\n'
    'cursor.row_factory = callback',
])
def test_yielded_and_independent_cursor_factories_remain_supported(source):
    assert check_fixture_writes(source) == []


@pytest.mark.parametrize('nested', [False, True])
def test_yielded_cursor_factory_connection_expires_with_owning_boundary(tmp_path, nested):
    workspace = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(workspace) as outer:
            if nested:
                with fixture_write(workspace) as inner:
                    cursor = inner.cursor()
                    cursor.row_factory = lambda current, row: current.connection
                    leaked = cursor.execute('SELECT 1').fetchone()
                    assert leaked is inner
            else:
                cursor = outer.cursor()
                cursor.row_factory = lambda current, row: current.connection
                leaked = cursor.execute('SELECT 1').fetchone()
                assert leaked is outer
            if nested:
                with pytest.raises(RuntimeError, match='owning boundary'):
                    leaked.commit()  # fixture-write: Exercise an expired callback handle in an isolated lifetime regression.
                assert outer.execute('SELECT 1').fetchone()[0] == 1
        with pytest.raises(RuntimeError, match='owning boundary'):
            leaked.commit()  # fixture-write: Exercise an expired callback handle in an isolated lifetime regression.
        assert workspace.connection.row_factory is sqlite3.Row
        assert workspace.connection.execute('SELECT 1').fetchone()[0] == 1
    finally:
        workspace.close()
