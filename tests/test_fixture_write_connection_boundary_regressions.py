"""Synthetic connection lifetime/configuration regression coverage."""
import sqlite3

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write


@pytest.mark.parametrize('saved', [False, True])
def test_outer_fixture_cannot_close_shared_connection(tmp_path, saved):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    try:
        with fixture_write(store) as connection:
            close = connection.close
            with pytest.raises(RuntimeError, match='fixture_write'):
                close() if saved else connection.close()  # fixture-write: Deliberately attempt shared-handle closure and require rejection before SQLite is touched.
            assert connection.execute('SELECT 1').fetchone()[0] == 1
        assert store.connection.execute('SELECT 1').fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('replacement', [None, lambda cursor, row: cursor.connection])
def test_raw_row_factory_replacement_is_restored_before_unlock(tmp_path, failure, replacement):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    original = store.connection.row_factory
    try:
        try:
            with fixture_write(store):
                store.connection.row_factory = replacement  # fixture-write: Deliberately replace raw configuration in an isolated negative restoration regression.
                if failure:
                    raise ValueError('synthetic rollback')
        except ValueError:
            assert failure
        assert store.connection.row_factory is original
        row = store.connection.execute('SELECT 1 AS value').fetchone()
        assert isinstance(row, sqlite3.Row) and row['value'] == 1
    finally:
        store.close()


def test_nested_row_factory_change_restores_outer_callback(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    callback = lambda cursor, row: row
    try:
        with fixture_write(store) as outer:
            outer.row_factory = callback
            with fixture_write(store) as nested:
                nested.row_factory = None
                assert nested.execute('SELECT 1').fetchone() == (1,)
            assert outer.row_factory is callback
            assert outer.execute('SELECT 1').fetchone() == (1,)
        assert store.connection.row_factory is sqlite3.Row
    finally:
        store.close()
