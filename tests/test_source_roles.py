"""Synthetic C2 designation, identity, authorization and recovery contracts."""
import json
import sqlite3
from pathlib import Path

import pytest

from tests.case_lifecycle_helpers import claim_synthetic_exit

from case_intelligence.source_roles import SourceRoleConflict, SourceRoleService, add_source_role_export
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

ACTOR = 'synthetic-role-reviewer'
DOCUMENT = 'a' * 32
TOKEN = 'b' * 32
VERSION = 'c' * 32


def seed(store):
    store.upsert_principal('test', ACTOR, 'Synthetic reviewer', ACTOR, preferred_principal_id=ACTOR)
    matter = store.create_matter('Synthetic charging paper roles', 'Unchanged synthetic descriptor', ACTOR)
    collection = store.create_source_collection(matter.matter_id, 'Synthetic intake', 'upload', ACTOR)
    store.register_source_organizations(matter.matter_id, collection.collection_id, [(DOCUMENT, 'synthetic-charge.txt')], ACTOR)
    store.upsert_source_catalog(matter.matter_id, [dict(document_id=DOCUMENT, version_id=VERSION, action_token=TOKEN,
        display_name='synthetic-charge.txt', relative_path='synthetic-charge.txt', media_type='text/plain',
        kind='TXT', tone='ready', origin='upload', source_state='ready', state_label='Ready', count_label='1 section')])
    return matter


def update(store, matter, revision=0, role='background', **kwargs):
    return SourceRoleService(store).update(matter.matter_id, ACTOR, TOKEN,
        expected_revision=revision, source_role=role, request_id='synthetic-role-change', **kwargs)


def snapshot(store, matter_id):
    rows = [dict(row) for row in store.connection.execute(
        'SELECT document_id,source_role,role_revision,role_updated_by,role_updated_at FROM workbench_source_organization WHERE matter_id=? ORDER BY document_id', (matter_id,))]
    events = [dict(row) for row in store.connection.execute(
        "SELECT * FROM workbench_audit_event WHERE matter_id=? AND action='source.role_update' ORDER BY occurred_at,event_id", (matter_id,))]
    return json.dumps(dict(sources=rows, audit_events=events), sort_keys=True).encode()


@pytest.fixture
def case(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite')
    matter = seed(store)
    yield store, matter
    store.close()


def test_mark_noop_clear_independent_organization_and_content_free_audit(case):
    store, matter = case
    service = SourceRoleService(store)
    assert service.read(matter.matter_id, ACTOR, TOKEN)['source_role'] == 'unclassified'
    saved = update(store, matter)
    assert saved['revision'] == 1 and saved['updated_by'] == ACTOR
    assert update(store, matter, 1) == saved
    collection = store.create_source_collection(matter.matter_id, 'Synthetic moved', 'upload', ACTOR)
    store.move_sources_to_collection(matter.matter_id, [DOCUMENT], collection.collection_id, ACTOR)
    store.update_source_review_state(matter.matter_id, [DOCUMENT], 'reviewed', ACTOR)
    store.register_source_organizations(matter.matter_id, collection.collection_id, [(DOCUMENT, 'renamed.txt')], ACTOR)
    assert service.read(matter.matter_id, ACTOR, TOKEN) == saved
    assert store.source_catalog_record(matter.matter_id, DOCUMENT).source_role == 'background'
    assert store.source_catalog_page(matter.matter_id).items[0].source_role == 'background'
    assert store.source_catalog_for_export(matter.matter_id, ACTOR)[0].source_role == 'background'
    assert update(store, matter, 1, 'unclassified')['revision'] == 2
    events = [e for e in store.audit_events(matter.matter_id) if e.action == 'source.role_update']
    assert len(events) == 2 and all(e.details == {} and e.actor_principal_id == ACTOR for e in events)
    assert events[0].occurred_at == saved['updated_at']
    assert 'synthetic-charge.txt' not in repr(events) and 'background' not in repr(events)


def test_separate_connection_conflict_and_late_authority_rollback(case):
    store, matter = case
    second = WorkspaceStore(Path(store.connection.execute('PRAGMA database_list').fetchone()[2]))
    try:
        update(store, matter)
        before = snapshot(store, matter.matter_id)
        with pytest.raises(SourceRoleConflict):
            update(second, matter)
        calls = []
        def revoke():
            calls.append(1)
            if len(calls) == 2:
                raise KeyError(ACTOR)
        with pytest.raises(KeyError):
            update(second, matter, 1, 'unclassified', check_authority=revoke)
        assert snapshot(second, matter.matter_id) == before
    finally:
        second.close()


@pytest.mark.parametrize('revision,role', [(True, 'background'), (-1, 'background'), (0, 'discovery'), (0, None), (0, [])])
def test_bad_inputs_are_atomic(case, revision, role):
    store, matter = case
    before = snapshot(store, matter.matter_id)
    with pytest.raises(WorkspaceProblem):
        update(store, matter, revision, role)
    assert snapshot(store, matter.matter_id) == before


@pytest.mark.parametrize('state', ['purging', 'purge_failed', 'deleted'])
def test_inactive_lifecycle_denies_role_access(case, state):
    store, matter = case
    with store._lock, store.connection:
        store.connection.execute('UPDATE workbench_matter_lifecycle SET state=? WHERE matter_id=?', (state, matter.matter_id))
    for admin in (False, True):
        with pytest.raises(KeyError):
            SourceRoleService(store, administrator_override=admin).read(matter.matter_id, ACTOR, TOKEN)
    with pytest.raises(KeyError):
        update(store, matter)


def test_revocation_disabled_provider_cross_matter_and_stale_token(case, monkeypatch):
    store, matter = case
    other = store.create_matter('Other synthetic role scope', '', ACTOR)
    with pytest.raises(KeyError):
        SourceRoleService(store).read(other.matter_id, ACTOR, TOKEN)
    with pytest.raises(KeyError):
        SourceRoleService(store).read(matter.matter_id, ACTOR, 'd' * 32)
    reviewer = 'synthetic-role-second'
    store.upsert_principal('test', reviewer, 'Synthetic second', reviewer, preferred_principal_id=reviewer)
    service = SourceRoleService(store, administrator_override=True)
    with pytest.raises(KeyError):
        service.update(matter.matter_id, reviewer, TOKEN, expected_revision=0, source_role='background', request_id='synthetic-denied')
    store.add_member(matter.matter_id, reviewer, ACTOR)
    assert service.read(matter.matter_id, reviewer, TOKEN)['revision'] == 0
    store.revoke_member(matter.matter_id, reviewer, ACTOR)
    with pytest.raises(KeyError):
        SourceRoleService(store).read(matter.matter_id, reviewer, TOKEN)
    monkeypatch.setattr(store, 'principal_enabled', lambda *_: False)
    with pytest.raises(KeyError):
        update(store, matter)


def test_sqlite_backup_restore_export_remove_and_purge(case, tmp_path):
    store, matter = case
    update(store, matter)
    expected = snapshot(store, matter.matter_id)
    path = tmp_path / 'clean' / 'workspace.sqlite'
    path.parent.mkdir()
    with sqlite3.connect(path) as target:
        store.connection.backup(target)
    for _ in range(2):
        restored = WorkspaceStore(path)
        assert snapshot(restored, matter.matter_id) == expected
        artifacts = []
        add_source_role_export(restored, matter.matter_id, ACTOR, False, lambda **a: artifacts.append(a))
        data = json.loads(artifacts[0]['artifact'].body)
        assert data['sources'] == [SourceRoleService(restored).read(matter.matter_id, ACTOR, TOKEN)]
        assert len(data['audit_events']) == 1 and data['audit_events'][0]['details_json'] == '{}'
        assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
        restored.close()
    store.remove_source_organization(matter.matter_id, DOCUMENT)
    with pytest.raises(KeyError):
        SourceRoleService(store).read(matter.matter_id, ACTOR, TOKEN)
    restored = WorkspaceStore(path)
    _, lifecycle = claim_synthetic_exit(restored, matter, ACTOR, source_count=1)
    restored.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert not restored.connection.execute('SELECT * FROM workbench_source_organization').fetchall()
    assert len([e for e in restored.audit_events(matter.matter_id) if e.action == 'source.role_update']) == 1
    restored.close()


def test_migration_defaults_old_rows_and_reopens(case, tmp_path):
    store, matter = case
    store.close()
    path = tmp_path / 'workspace.sqlite'
    # Reconstruct the exact old table by removing only the four new columns.
    with sqlite3.connect(path) as conn:
        for field in ('role_updated_at', 'role_updated_by', 'role_revision', 'source_role'):
            conn.execute(f'ALTER TABLE workbench_source_organization DROP COLUMN {field}')
    reopened = WorkspaceStore(path)
    assert SourceRoleService(reopened).read(matter.matter_id, ACTOR, TOKEN) == dict(
        document_id=DOCUMENT, version_id=VERSION, source_role='unclassified', revision=0, updated_by=None, updated_at=None)
    reopened.close()
    root = Path(__file__).resolve().parents[1]
    assert (root / 'migrations/sqlite/0039_source_roles.sql').read_bytes() == (root / 'src/case_intelligence/migrations/sqlite/0039_source_roles.sql').read_bytes()


def test_complete_export_refuses_missing_version_and_excess_history(case):
    store, matter = case
    update(store, matter)
    artifacts = []
    with store._lock, store.connection:
        store.connection.executemany(
            "INSERT INTO workbench_audit_event(event_id,actor_principal_id,matter_id,request_id,action,outcome,object_type,object_id,details_json,occurred_at) VALUES (?,?,?,'synthetic-bounded-history','source.role_update','success','source',?,'{}','2026-01-01T00:00:00Z')",
            [(f'audit-synthetic-{i}', ACTOR, matter.matter_id, DOCUMENT) for i in range(10000)])
    with pytest.raises(WorkspaceProblem, match='complete export limit'):
        add_source_role_export(store, matter.matter_id, ACTOR, False, lambda **a: artifacts.append(a))
    assert artifacts == []


def test_complete_export_refuses_missing_version(case):
    store, matter = case
    artifacts = []
    store.delete_source_catalog(matter.matter_id, [DOCUMENT])
    with pytest.raises(WorkspaceProblem, match='no current source inventory'):
        add_source_role_export(store, matter.matter_id, ACTOR, False, lambda **a: artifacts.append(a))
    assert artifacts == []
