"""Synthetic PRAGMA configuration and transactional metadata boundaries."""
from contextlib import nullcontext
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from tests.fixture_write import fixture_write


@pytest.fixture
def workspace():
    raw = sqlite3.connect(':memory:')
    raw.execute('PRAGMA foreign_keys=ON')
    store = SimpleNamespace(connection=raw, _lock=RLock())
    with fixture_write(store) as connection:
        connection.execute('CREATE TABLE synthetic_fixture(value TEXT)')
    try:
        yield store
    finally:
        raw.close()


@pytest.mark.parametrize('sql', [
    'PRAGMA foreign_keys=OFF',
    'PRAGMA main.foreign_keys(OFF)',
    '\ufeff; /* synthetic */ PRAGMA "main" . [foreign_keys] /* setting */ = OFF',
    "PRAGMA 'foreign_keys'(OFF)",
    'PRAGMA `foreign_keys`=OFF',
    'EXPLAIN PRAGMA foreign_keys=OFF',
    'EXPLAIN QUERY PLAN PRAGMA foreign_keys=OFF',
    'PRAGMA journal_mode=WAL',
    'PRAGMA writable_schema=ON',
    'PRAGMA busy_timeout=0',
    'PRAGMA defer_foreign_keys=ON',
    'PRAGMA synchronous=OFF',
    'PRAGMA legacy_alter_table=ON',
    'PRAGMA optimize',
    'PRAGMA main.wal_checkpoint',
    'PRAGMA incremental_vacuum(1)',
    'PRAGMA shrink_memory',
])
@pytest.mark.parametrize('cursor', [False, True])
@pytest.mark.parametrize('nested', [False, True])
def test_unsafe_pragmas_fail_before_changing_transaction_or_settings(workspace, sql, cursor, nested):
    with fixture_write(workspace) as outer:
        if nested:
            outer.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
        with fixture_write(workspace) if nested else nullcontext(outer) as connection:
            active = connection.in_transaction
            handle = connection.cursor() if cursor else connection
            with pytest.raises(RuntimeError, match='fixture_write.*PRAGMA'):
                # fixture-write: Deliberately exercise rejected PRAGMAs through the selected live fixture handle.
                handle.execute(sql)
            assert connection.in_transaction is active
            assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            assert connection.execute('PRAGMA writable_schema').fetchone()[0] == 0
            assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        assert [row[0] for row in outer.execute('SELECT * FROM synthetic_fixture')] == (['caller'] if nested else [])


@pytest.mark.parametrize('cursor', [False, True])
def test_script_rejects_unsafe_pragma_before_executing_any_statement(workspace, cursor):
    with fixture_write(workspace) as connection:
        handle = connection.cursor() if cursor else connection
        with pytest.raises(RuntimeError, match='fixture_write.*PRAGMA'):
            handle.executescript("INSERT INTO synthetic_fixture VALUES ('unwanted'); PRAGMA foreign_keys=OFF;")
        assert not connection.in_transaction
        assert connection.execute('SELECT * FROM synthetic_fixture').fetchall() == []


@pytest.mark.parametrize('sql', [
    'PRAGMA foreign_keys', 'PRAGMA main.journal_mode', 'PRAGMA writable_schema',
    'PRAGMA user_version', 'PRAGMA database_list', 'PRAGMA table_info(synthetic_fixture)',
    'PRAGMA main.table_xinfo(synthetic_fixture)', 'PRAGMA foreign_key_check(synthetic_fixture)',
    'PRAGMA integrity_check(1)', 'PRAGMA index_list(synthetic_fixture)',
    "PRAGMA table_info('synthetic_fixture/*literal*/')",
    'EXPLAIN PRAGMA foreign_keys',
])
def test_pragma_queries_remain_available_without_opening_transaction(workspace, sql):
    with fixture_write(workspace) as connection:
        connection.execute(sql).fetchall()
        assert not connection.in_transaction


@pytest.mark.parametrize('name', ['user_version', 'application_id'])
@pytest.mark.parametrize('syntax', ['PRAGMA main.{name}=42', 'PRAGMA "main"."{name}"(42)'])
def test_supported_metadata_pragma_commits_and_rolls_back(workspace, name, syntax):
    with fixture_write(workspace) as connection:
        connection.execute(syntax.format(name=name))
        assert connection.execute(f'PRAGMA {name}').fetchone()[0] == 42
    with pytest.raises(ValueError, match='synthetic rollback'):
        with fixture_write(workspace) as connection:
            connection.execute(f'PRAGMA {name}=84')
            raise ValueError('synthetic rollback')
    with fixture_write(workspace) as connection:
        assert connection.execute(f'PRAGMA {name}').fetchone()[0] == 42


def test_nested_metadata_pragma_rollback_preserves_caller(workspace):
    with fixture_write(workspace) as outer:
        outer.execute('PRAGMA user_version=42')
        with pytest.raises(ValueError, match='synthetic rollback'):
            with fixture_write(workspace) as inner:
                inner.execute('PRAGMA user_version=84')
                raise ValueError('synthetic rollback')
        assert outer.in_transaction
        assert outer.execute('PRAGMA user_version').fetchone()[0] == 42
