"""Synthetic legacy-autocommit rejection before fixture DML can persist."""
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from tests.fixture_write import fixture_write


@pytest.fixture
def database(tmp_path):
    raw = sqlite3.connect(tmp_path / 'synthetic.sqlite', isolation_level='IMMEDIATE')
    raw.execute('CREATE TABLE synthetic_fixture(value TEXT)')
    store = SimpleNamespace(connection=raw, _lock=RLock())
    try:
        yield store
    finally:
        raw.close()


@pytest.mark.parametrize('media', [False, True])
def test_legacy_autocommit_failure_never_persists_raw_fixture_dml(database, tmp_path, media):
    # fixture-write: Configure legacy autocommit solely to reproduce the rejected fixture mode.
    database.connection.isolation_level = None
    entered = []
    failure = None
    try:
        with fixture_write(database, media=media):
            entered.append(True)
            # fixture-write: Deliberately reproduce raw DML that historically survived this failed fixture.
            database.connection.execute("INSERT INTO synthetic_fixture VALUES ('synthetic escaped row')")
            raise ValueError('synthetic fixture failure')
    except (RuntimeError, ValueError) as error:
        failure = error
    with sqlite3.connect(tmp_path / 'synthetic.sqlite') as independent:
        assert independent.execute('SELECT * FROM synthetic_fixture').fetchall() == []
    assert entered == []
    assert isinstance(failure, RuntimeError) and 'legacy autocommit' in str(failure)


@pytest.mark.parametrize('pending', [False, True])
def test_rejection_preserves_existing_connection_state_and_caller_work(database, pending):
    # fixture-write: Configure the unsupported mode before testing that fixture entry is side-effect free.
    database.connection.isolation_level = None
    if pending:
        # fixture-write: Establish an explicit caller transaction in legacy autocommit for entry rejection.
        database.connection.execute('BEGIN IMMEDIATE')
        # fixture-write: Keep caller-owned pending data that rejected fixture entry must preserve.
        database.connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
    factory = database.connection.row_factory
    with pytest.raises(RuntimeError, match='fixture_write.*legacy autocommit'):
        with fixture_write(database):
            pytest.fail('unsupported fixture mode entered its body')
    assert database.connection.in_transaction is pending
    assert database.connection.isolation_level is None
    assert database.connection.row_factory is factory
    assert database.connection.execute('SELECT * FROM synthetic_fixture').fetchall() == ([('caller',)] if pending else [])


def test_yielded_handle_rejects_switch_to_legacy_autocommit_before_committing(database, tmp_path):
    with pytest.raises(ValueError, match='synthetic rollback'):
        with fixture_write(database) as connection:
            connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
            with pytest.raises(RuntimeError, match='fixture_write.*legacy autocommit'):
                connection.isolation_level = None
            assert connection.in_transaction
            assert connection.isolation_level == 'IMMEDIATE'
            raise ValueError('synthetic rollback')
    with sqlite3.connect(tmp_path / 'synthetic.sqlite') as independent:
        assert independent.execute('SELECT * FROM synthetic_fixture').fetchall() == []


def test_autocommit_false_with_none_isolation_preserves_pep249_caller_transaction(database):
    # fixture-write: Configure supported PEP249 transaction control on this isolated synthetic connection.
    database.connection.autocommit = False
    # fixture-write: isolation_level is ignored in PEP249 mode and must remain supported here.
    database.connection.isolation_level = None
    # fixture-write: Seed pending PEP249 caller data before exercising fixture savepoint rollback.
    database.connection.execute("INSERT INTO synthetic_fixture VALUES ('caller')")
    with pytest.raises(ValueError, match='inner rollback'):
        with fixture_write(database) as connection:
            connection.execute("INSERT INTO synthetic_fixture VALUES ('inner')")
            raise ValueError('inner rollback')
    assert database.connection.autocommit is False
    assert database.connection.in_transaction
    assert database.connection.execute('SELECT * FROM synthetic_fixture').fetchall() == [('caller',)]


def test_switch_from_pep249_to_legacy_none_is_rejected_before_mode_change(database):
    with fixture_write(database) as connection:
        connection.autocommit = False
        connection.isolation_level = None
        with pytest.raises(RuntimeError, match='fixture_write.*legacy autocommit'):
            connection.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
        assert connection.autocommit is False
        assert connection.in_transaction


@pytest.mark.parametrize('isolation_level', ['', 'DEFERRED', 'IMMEDIATE', 'EXCLUSIVE'])
def test_supported_legacy_transaction_modes_retain_exception_rollback(database, isolation_level):
    # fixture-write: Configure a supported synthetic transaction mode before testing rollback behavior.
    database.connection.isolation_level = isolation_level
    with pytest.raises(ValueError, match='synthetic rollback'):
        with fixture_write(database) as connection:
            connection.execute("INSERT INTO synthetic_fixture VALUES ('discard')")
            raise ValueError('synthetic rollback')
    assert not database.connection.in_transaction
    assert database.connection.execute('SELECT * FROM synthetic_fixture').fetchall() == []
