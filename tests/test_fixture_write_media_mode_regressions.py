"""Synthetic media fixture entry preserves independent caller transactions."""
import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.fixture_write import fixture_write


@pytest.fixture
def media_workspace(tmp_path):
    store = WorkspaceStore(tmp_path / 'synthetic.sqlite')
    actor = store.upsert_principal('test', 'synthetic-owner', 'Synthetic Reviewer', 'synthetic').principal_id
    matter = store.create_matter('Synthetic media fixture', '', actor)
    with fixture_write(store) as connection:
        connection.execute('CREATE TABLE synthetic_caller(value TEXT)')
    try:
        yield store, matter, actor
    finally:
        store.close()


def seed_media(store, matter, actor, entered):
    # fixture-write: Deliberately invoke this reusable media boundary from caller transactions and require rejection before its body.
    with fixture_write(store, media=True):
        entered.append(True)
        # fixture-write: This synthetic seed body must remain unentered when the negative caller-transaction cases reject media entry.
        job = store.queue_media_job(
            matter.matter_id, 'a' * 32, 'b' * 32, actor,
            source_sha256='c' * 64, byte_size=1, media_type='audio/wav', duration_ms=1,
        )
        # fixture-write: Preserve the complete synthetic seed for outer-mode controls; nested negative cases must never reach this claim.
        claimed = store.claim_media_job('synthetic-fixture-worker')
        assert claimed.media_job_id == job.media_job_id
        # fixture-write: Complete only the valid outer-mode control; the negative caller-transaction cases must reject before this finish.
        return store.finish_media_job(job.media_job_id, degraded=False, message='Synthetic ready')


def test_reusable_outer_media_fixture_retains_public_api_commits(media_workspace):
    store, matter, actor = media_workspace
    entered = []
    result = seed_media(store, matter, actor, entered)
    assert entered == [True]
    assert result.state == 'succeeded'
    assert not store.connection.in_transaction
    assert store.media_job(matter.matter_id, 'a' * 32, 'b' * 32).state == 'succeeded'


@pytest.mark.parametrize('setup', ['read-first', 'write-first', 'nested'])
def test_indirect_media_fixture_rejects_active_same_workspace_before_body(media_workspace, setup):
    store, matter, actor = media_workspace
    entered = []
    with pytest.raises(ValueError, match='caller rollback'):
        with fixture_write(store) as outer:
            outer.execute('SELECT * FROM synthetic_caller').fetchall()
            if setup == 'write-first':
                outer.execute("INSERT INTO synthetic_caller VALUES ('caller')")
            if setup == 'nested':
                with fixture_write(store) as inner:
                    inner.execute("INSERT INTO synthetic_caller VALUES ('inner')")
                    with pytest.raises(RuntimeError, match='media=True.*caller transaction'):
                        seed_media(store, matter, actor, entered)
                    assert inner.in_transaction
                    assert [row[0] for row in inner.execute('SELECT * FROM synthetic_caller')] == ['inner']
            else:
                active = outer.in_transaction
                with pytest.raises(RuntimeError, match='media=True.*caller transaction'):
                    seed_media(store, matter, actor, entered)
                assert outer.in_transaction is active
                assert [row[0] for row in outer.execute('SELECT * FROM synthetic_caller')] == (
                    ['caller'] if setup == 'write-first' else []
                )
            assert entered == []
            # fixture-write: Inspect isolated media state inside the caller transaction to prove rejected seed entry did not create a job.
            assert store.media_job(matter.matter_id, 'a' * 32, 'b' * 32) is None
            raise ValueError('caller rollback')
    with fixture_write(store) as connection:
        assert connection.execute('SELECT * FROM synthetic_caller').fetchall() == []


def test_media_fixture_rejects_existing_raw_caller_transaction(media_workspace):
    store, matter, actor = media_workspace
    entered = []
    with store._lock:
        # fixture-write: Establish a raw caller-owned transaction to verify media entry cannot commit it.
        store.connection.execute('BEGIN IMMEDIATE')
        try:
            # fixture-write: Seed pending caller work before the deliberately rejected media fixture.
            store.connection.execute("INSERT INTO synthetic_caller VALUES ('caller')")
            with pytest.raises(RuntimeError, match='media=True.*caller transaction'):
                seed_media(store, matter, actor, entered)
            assert entered == []
            assert store.connection.in_transaction
            assert [row[0] for row in store.connection.execute('SELECT * FROM synthetic_caller')] == ['caller']
        finally:
            # fixture-write: Roll back the independently owned test transaction after the entry assertion.
            store.connection.rollback()
    assert store.media_job(matter.matter_id, 'a' * 32, 'b' * 32) is None


def test_media_fixture_on_distinct_workspace_keeps_independent_commit(media_workspace, tmp_path):
    media_store, matter, actor = media_workspace
    caller = WorkspaceStore(tmp_path / 'synthetic-caller.sqlite')
    entered = []
    try:
        with pytest.raises(ValueError, match='caller rollback'):
            with fixture_write(caller) as connection:
                connection.execute('CREATE TABLE synthetic_caller(value TEXT)')
                connection.execute("INSERT INTO synthetic_caller VALUES ('caller')")
                assert seed_media(media_store, matter, actor, entered).state == 'succeeded'
                assert connection.in_transaction
                raise ValueError('caller rollback')
        assert entered == [True]
        assert media_store.media_job(matter.matter_id, 'a' * 32, 'b' * 32).state == 'succeeded'
        assert not media_store.connection.in_transaction
        assert not caller.connection.execute("SELECT 1 FROM sqlite_schema WHERE name='synthetic_caller'").fetchone()
    finally:
        caller.close()


@pytest.mark.parametrize('mode', [None, 0, 1, 'true', [], object()])
def test_media_mode_requires_an_actual_bool_before_entering(media_workspace, mode):
    store, _, _ = media_workspace
    factory = store.connection.row_factory
    with pytest.raises(TypeError, match='media must be a bool'):
        with fixture_write(store, media=mode):
            pytest.fail('invalid media mode entered the fixture')
    assert not store.connection.in_transaction
    assert store.connection.row_factory is factory


def test_media_mode_is_keyword_only(media_workspace):
    store, _, _ = media_workspace
    with pytest.raises(TypeError):
        with fixture_write(store, True):
            pytest.fail('positional media mode entered the fixture')


def test_explicit_false_retains_nested_sql_savepoint_behavior(media_workspace):
    store, _, _ = media_workspace
    with fixture_write(store, media=False) as outer:
        outer.execute("INSERT INTO synthetic_caller VALUES ('caller')")
        with pytest.raises(ValueError, match='inner rollback'):
            with fixture_write(store, media=False) as inner:
                inner.execute("INSERT INTO synthetic_caller VALUES ('inner')")
                raise ValueError('inner rollback')
        assert outer.in_transaction
        assert [row[0] for row in outer.execute('SELECT * FROM synthetic_caller')] == ['caller']
