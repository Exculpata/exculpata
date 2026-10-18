"""Synthetic C1 storage, authorization, atomicity, fidelity and purge contracts."""

from tests.fixture_write import fixture_write
import json
import sqlite3
from pathlib import Path

import pytest

from tests.case_lifecycle_helpers import claim_synthetic_exit

from case_intelligence.matter_purpose import MatterPurposeConflict
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

ACTOR = 'synthetic-purpose-reviewer'
OBJECTIVE = '  Understand the café timeline <synthetic> & retain spaces.  '
BACKGROUND = 'Synthetic background\r\nSecond line\nCafé — e\u0301\t終'


def seed(store):
    store.upsert_principal('test', ACTOR, 'Synthetic reviewer', ACTOR, preferred_principal_id=ACTOR)
    return store.create_matter('Synthetic matter orientation', 'Unchanged descriptor', ACTOR)


def update(store, matter, revision=0, **kwargs):
    return store.update_matter_purpose(matter.matter_id, ACTOR, expected_revision=revision,
        objective=kwargs.pop('objective', OBJECTIVE), background=kwargs.pop('background', BACKGROUND),
        request_id='synthetic-purpose-request', **kwargs)


def snapshot(store, matter_id):
    """Canonical UTF-8 receipts compare all stored field and audit bytes."""
    record = dict(store.connection.execute('SELECT * FROM workbench_matter_purpose WHERE matter_id=?', (matter_id,)).fetchone())
    events = [dict(row) for row in store.connection.execute(
        "SELECT * FROM workbench_audit_event WHERE matter_id=? AND action='matter.purpose_update' ORDER BY occurred_at,event_id",
        (matter_id,))]
    return json.dumps(dict(record=record, audit=events), ensure_ascii=False, sort_keys=True).encode('utf-8')


@pytest.fixture
def case(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite')
    matter = seed(store)
    yield store, matter
    store.close()


def test_set_edit_clear_reopen_and_content_free_audit(case):
    store, matter = case
    assert store.matter_purpose(matter.matter_id, ACTOR)['revision'] == 0
    assert not store.connection.execute('SELECT * FROM workbench_matter_purpose').fetchall()
    saved = update(store, matter)
    assert saved['objective'] == OBJECTIVE and saved['background'] == BACKGROUND
    assert update(store, matter, 1) == saved  # No-op preserves revision and timestamp.
    other = WorkspaceStore(Path(store.connection.execute('PRAGMA database_list').fetchone()[2]))
    try:
        assert other.matter_purpose(matter.matter_id, ACTOR) == saved
        changed = update(other, matter, 1, objective='Edited synthetic objective')
        cleared = update(other, matter, 2, objective='', background='')
        assert changed['revision'] == 2 and cleared['revision'] == 3
        assert cleared['objective'] == cleared['background'] == ''
    finally:
        other.close()
    events = [e for e in store.audit_events(matter.matter_id) if e.action == 'matter.purpose_update']
    assert len(events) == 3
    assert all(e.actor_principal_id == ACTOR and e.occurred_at and e.details == {} for e in events)
    assert events[0].occurred_at == saved['updated_at']
    assert OBJECTIVE not in repr(events) and BACKGROUND not in repr(events)
    assert store.get_matter(matter.slug, ACTOR).descriptor == 'Unchanged descriptor'


@pytest.mark.parametrize('field,value', [
    ('objective', 'x' * 501), ('objective', '界' * 342),
    ('background', 'x' * 10001), ('background', '界' * 8001),
    ('objective', '\x00'), ('background', '\ud800'), ('objective', None),
], ids=['objective-characters', 'objective-bytes', 'background-characters', 'background-bytes', 'control', 'surrogate', 'null'])
def test_explicit_character_byte_plain_text_limits_are_atomic(case, field, value):
    store, matter = case
    with pytest.raises(WorkspaceProblem):
        update(store, matter, **{field: value})
    assert store.matter_purpose(matter.matter_id, ACTOR)['revision'] == 0
    assert not store.audit_events(matter.matter_id)


def test_boundaries_separate_connections_stale_revision_and_rollback(case):
    store, matter = case
    update(store, matter, objective='x' * 500, background='界' * 8000)
    second = WorkspaceStore(Path(store.connection.execute('PRAGMA database_list').fetchone()[2]))
    try:
        stale = second.matter_purpose(matter.matter_id, ACTOR)
        update(store, matter, 1)
        before = snapshot(store, matter.matter_id)
        with pytest.raises(MatterPurposeConflict):
            update(second, matter, stale['revision'], objective='Stale')
        calls = []
        def revoke_late():
            calls.append(1)
            if len(calls) == 2:
                raise KeyError(ACTOR)
        with pytest.raises(KeyError):
            update(store, matter, 2, objective='Late denial', check_authority=revoke_late)
        assert snapshot(store, matter.matter_id) == before
    finally:
        second.close()


def test_cross_matter_members_revoked_users_and_lifecycle(case):
    store, matter = case
    other = store.create_matter('Other synthetic matter', '', ACTOR)
    update(store, matter)
    assert store.matter_purpose(other.matter_id, ACTOR)['objective'] == ''
    reviewer = 'synthetic-purpose-second'
    store.upsert_principal('test', reviewer, 'Second synthetic reviewer', reviewer, preferred_principal_id=reviewer)
    for operation in (lambda: store.matter_purpose(matter.matter_id, reviewer),
                      lambda: store.update_matter_purpose(matter.matter_id, reviewer, expected_revision=1,
                          objective='Denied', background='', request_id='synthetic-denied')):
        with pytest.raises(KeyError):
            operation()
    store.add_member(matter.matter_id, reviewer, ACTOR)
    result = store.update_matter_purpose(matter.matter_id, reviewer, expected_revision=1,
        objective='Team edit', background=BACKGROUND, request_id='synthetic-team-edit')
    assert result['updated_by'] == reviewer
    store.revoke_member(matter.matter_id, reviewer, ACTOR)
    with pytest.raises(KeyError):
        store.matter_purpose(matter.matter_id, reviewer)
    _, lifecycle = claim_synthetic_exit(store, matter, ACTOR)
    for admin in (False, True):
        with pytest.raises(KeyError):
            store.matter_purpose(matter.matter_id, ACTOR, administrator_override=admin)
    with pytest.raises(KeyError):
        update(store, matter, 2)
    store.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert store.connection.execute('SELECT display_name FROM workbench_matter WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 'Deleted matter'
    assert not store.connection.execute('SELECT * FROM workbench_matter_purpose').fetchall()
    assert len([e for e in store.audit_events(matter.matter_id) if e.action == 'matter.purpose_update']) == 2
    assert store.matter_purpose(other.matter_id, ACTOR)['revision'] == 0


def test_backup_clean_instance_and_migration_mirror(case, tmp_path):
    store, matter = case
    root = Path(__file__).resolve().parents[1]
    migration = '0038_matter_purpose.sql'
    assert (root / 'migrations/sqlite' / migration).read_bytes() == (root / 'src/case_intelligence/migrations/sqlite' / migration).read_bytes()
    update(store, matter)
    expected = snapshot(store, matter.matter_id)
    restored_path = tmp_path / 'clean' / 'workspace.sqlite'
    restored_path.parent.mkdir()
    with sqlite3.connect(restored_path) as target:
        with store._lock:
            store.connection.backup(target)
    for _ in range(2):
        restored = WorkspaceStore(restored_path)
        assert snapshot(restored, matter.matter_id) == expected
        assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
        restored.close()


def test_exact_byte_boundaries_and_disabled_principal(case):
    store, matter = case
    saved = update(store, matter, objective='界' * 341 + 'x', background='x' * 10000)
    assert len(saved['objective'].encode()) == 1024 and len(saved['background']) == 10000
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (ACTOR,))
    for operation in (lambda: store.matter_purpose(matter.matter_id, ACTOR), lambda: update(store, matter, 1)):
        with pytest.raises(KeyError):
            operation()


@pytest.mark.parametrize('state', ['purging', 'purge_failed', 'deleted'])
def test_nonactive_matter_denies_read_write_even_with_administrator_read_override(case, state):
    store, matter = case
    update(store, matter)
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_matter_lifecycle SET state=? WHERE matter_id=?', (state, matter.matter_id))
    for admin in (True, False):
        with pytest.raises(KeyError):
            store.matter_purpose(matter.matter_id, ACTOR, administrator_override=admin)
    with pytest.raises(KeyError):
        update(store, matter, 1)
