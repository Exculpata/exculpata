"""Synthetic portability and recovery controls for index checkpoint scheduling."""
from contextlib import contextmanager
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from case_intelligence.modules import ModuleDatabase
from case_intelligence.modules.discovery_review.stored_index.models import Budget
from case_intelligence.modules.discovery_review.stored_index.repository import Repository
from tests.fixture_write import fixture_write


CAPACITY = 2 * 1024 * 1024
CHECKPOINT_BYTES = 8 * 1024 * 1024


@pytest.fixture
def standalone_repository(tmp_path):
    connection = sqlite3.connect(tmp_path / 'synthetic-index.sqlite')
    repository = Repository(ModuleDatabase(connection, RLock()))
    try:
        yield repository
    finally:
        connection.close()


def test_checkpoint_policy_restores_after_begin_failure(standalone_repository, tmp_path):
    repository = standalone_repository
    assert repository.rows('PRAGMA journal_mode=WAL') == (('wal',),)
    repository.rows('PRAGMA busy_timeout=0')
    prior = repository.rows('PRAGMA wal_autocheckpoint')[0][0]
    blocker = sqlite3.connect(tmp_path / 'synthetic-index.sqlite')
    workspace = SimpleNamespace(connection=blocker, _lock=RLock())
    budget = Budget(256, 224)
    reached = []
    try:
        with fixture_write(workspace) as connection:
            connection.execute('BEGIN IMMEDIATE')
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                with repository.transaction(budget, CAPACITY, CHECKPOINT_BYTES):
                    reached.append(True)
        assert not reached
        assert repository.rows('PRAGMA wal_autocheckpoint')[0][0] == prior
        assert repository.rows('PRAGMA synchronous') == ((2,),)
        assert budget.control == budget.touches == 5 and budget.work == 0
    finally:
        blocker.close()


def test_checkpoint_policy_leaves_non_wal_database_unchanged(standalone_repository):
    repository = standalone_repository
    assert repository.rows('PRAGMA journal_mode') == (('delete',),)
    prior = repository.rows('PRAGMA wal_autocheckpoint')
    budget = Budget(256, 224)
    with repository.transaction(budget, CAPACITY, CHECKPOINT_BYTES) as tx:
        assert tx.read('SELECT 1') == ((1,),)
    assert repository.rows('PRAGMA journal_mode') == (('delete',),)
    assert repository.rows('PRAGMA wal_autocheckpoint') == prior
    assert budget.control == budget.work == 1 and budget.touches == 2


@pytest.mark.parametrize('interrupt', [False, True])
def test_checkpoint_policy_bypasses_postgres_connection(interrupt):
    transitions = []

    @contextmanager
    def postgres_transaction():
        transitions.append('begin')
        try:
            yield
        except RuntimeError:
            transitions.append('rollback')
            raise
        else:
            transitions.append('commit')

    def reject_sql(*args):
        raise AssertionError('checkpoint policy must not issue SQLite SQL to PostgreSQL')

    database = ModuleDatabase(SimpleNamespace(transaction=postgres_transaction, execute=reject_sql), RLock())
    repository = Repository(database)
    budget = Budget(256, 224)
    try:
        with repository.transaction(budget, CAPACITY, CHECKPOINT_BYTES) as tx:
            assert tx.active and tx.repository is repository
            if interrupt:
                raise RuntimeError('synthetic interrupted transaction')
    except RuntimeError as error:
        assert interrupt and str(error) == 'synthetic interrupted transaction'
    assert not tx.active
    assert transitions == ['begin', 'rollback' if interrupt else 'commit']
    assert budget.control == budget.work == budget.touches == 0


@pytest.mark.parametrize('page_size', [1024, 8192])
def test_checkpoint_policy_uses_actual_page_size(standalone_repository, page_size):
    repository = standalone_repository
    repository.rows(f'PRAGMA page_size={page_size}')
    assert repository.rows('PRAGMA journal_mode=WAL') == (('wal',),)
    assert repository.rows('PRAGMA page_size') == ((page_size,),)
    prior = repository.rows('PRAGMA wal_autocheckpoint')[0][0]
    budget = Budget(256, 224)
    with repository.transaction(budget, CAPACITY, CHECKPOINT_BYTES):
        assert repository.checkpoint_metadata(budget, 'PRAGMA wal_autocheckpoint') == (
            (CHECKPOINT_BYTES // (page_size + 24),),
        )
    assert repository.rows('PRAGMA wal_autocheckpoint')[0][0] == prior
    assert budget.control == budget.touches == 6 and budget.work == 0


def test_checkpointed_transactions_reopen_with_only_committed_bytes(tmp_path):
    database_path = tmp_path / 'synthetic-reopen.sqlite'
    original = sqlite3.connect(database_path)
    lock = RLock()
    workspace = SimpleNamespace(connection=original, _lock=lock)
    repository = Repository(ModuleDatabase(original, lock))
    payload = b'\x00synthetic-index-value\xff'
    try:
        assert repository.rows('PRAGMA journal_mode=WAL') == (('wal',),)
        with fixture_write(workspace) as connection:
            connection.execute('CREATE TABLE synthetic_index (row_id INTEGER PRIMARY KEY, payload BLOB)')
        with repository.transaction(Budget(256, 224), CAPACITY, CHECKPOINT_BYTES) as tx:
            tx.write('INSERT INTO synthetic_index VALUES (?,?)', (1, payload))
        with pytest.raises(RuntimeError, match='synthetic interrupted transaction'):
            with repository.transaction(Budget(256, 224), CAPACITY, CHECKPOINT_BYTES) as tx:
                tx.write('INSERT INTO synthetic_index VALUES (?,?)', (2, b'uncommitted'))
                raise RuntimeError('synthetic interrupted transaction')
        assert repository.rows('PRAGMA synchronous') == ((2,),)
    finally:
        original.close()
    reopened = sqlite3.connect(database_path)
    try:
        restored = Repository(ModuleDatabase(reopened, RLock()))
        assert restored.rows('SELECT row_id,payload FROM synthetic_index ORDER BY row_id') == ((1, payload),)
        assert restored.rows('PRAGMA integrity_check') == (('ok',),)
    finally:
        reopened.close()
