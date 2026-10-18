"""Synthetic approval, ownership, bounds and durable reference acceptance."""

from tests.fixture_write import fixture_write
import json
from pathlib import Path
import sqlite3

import pytest

from tests.case_lifecycle_helpers import claim_synthetic_exit

from case_intelligence.matter_context import ContextConflict, MatterContextService
from case_intelligence.matter_context_repository import ContextLimit
from case_intelligence.workspace_store import WorkspaceStore
from tests.test_evidence_assertions import ACTOR, reference, services, setup, values


@pytest.fixture
def case(tmp_path):
    result = setup(tmp_path)
    try:
        yield result
    finally:
        result[0].close()


def add(service, matter, kind, identifier, actor=ACTOR):
    selection, candidate = service.inspect(matter.matter_id, actor, kind=kind, object_id=identifier)
    service.change(matter.matter_id, actor, expected_revision=selection['revision'], action='add',
                   kind=kind, object_id=identifier, approval=candidate['approval'])
    return candidate


def test_two_reviewers_same_names_all_statuses_no_fact_copies_and_no_get_writes(case):
    store, matter, entities, assertions, first = case
    service = MatterContextService(assertions)
    second = 'synthetic-second-reviewer'
    store.upsert_principal('test', second, 'Second reviewer', second, preferred_principal_id=second)
    store.add_member(matter.matter_id, second, ACTOR)
    assert service.inspect(matter.matter_id, ACTOR)[0]['revision'] == 0
    assert not store.connection.execute('SELECT * FROM workbench_context_selection').fetchall()
    for status in ('suggested', 'needs_review', 'confirmed', 'disputed', 'dismissed'):
        item = entities.create(matter.matter_id, ACTOR, display_name='Alex Example', status=status)
        add(service, matter, 'entity', item['entity_id'])
        note, _ = store.create_notebook_item(matter.matter_id, ACTOR, item_type='note', title='Orientation', body='Synthetic', status=status)
        add(service, matter, 'notebook_item', note.item_id)
    for status in ('needs_review', 'confirmed', 'disputed', 'dismissed'):
        record = assertions.create(matter.matter_id, ACTOR, **values(first, status=status))
        add(service, matter, 'assertion', record['assertion_id'])
    before = store.connection.total_changes
    selection, _ = service.inspect(matter.matter_id, ACTOR)
    assert store.connection.total_changes == before
    assert len(selection['rows']) == 14
    assert all(row['state'] == 'Unchanged' for row in selection['rows'])
    assert {row['current']['record']['status'] for row in selection['rows']} == {'suggested','needs_review','confirmed','disputed','dismissed'}
    assert service.inspect(matter.matter_id, second)[0]['entries'] == []
    add(service, matter, 'entity', first['entity_id'], actor=second)
    assert len(service.inspect(matter.matter_id, ACTOR)[0]['entries']) == 14
    stored = json.dumps(service.repository.export(matter.matter_id))
    assert 'Alex Example' not in stored and 'Synthetic' not in stored
    assert second in stored and ACTOR in stored


def test_note_away_and_back_and_explicit_reconciliation(case):
    store, matter, _, assertions, _ = case
    service = MatterContextService(assertions)
    note, _ = store.create_notebook_item(matter.matter_id, ACTOR, item_type='note', title='Original', body='Synthetic', status='needs_review')
    approved = add(service, matter, 'notebook_item', note.item_id)
    for title in ('Changed', 'Original'):
        note = store.update_notebook_item(matter.matter_id, ACTOR, note.item_id, expected_updated_at=note.updated_at,
                                         item_type='note', status='needs_review', title=title, body='Synthetic')
    current, candidate = service.inspect(matter.matter_id, ACTOR, kind='notebook_item', object_id=note.item_id)
    assert current['rows'][0]['state'] == 'Changed'
    assert candidate['approval']['version'] != approved['approval']['version']
    with pytest.raises(ContextConflict):
        service.change(matter.matter_id, ACTOR, expected_revision=1, action='reconcile', kind='notebook_item',
                       object_id=note.item_id, approval=approved['approval'])
    service.change(matter.matter_id, ACTOR, expected_revision=1, action='reconcile', kind='notebook_item',
                   object_id=note.item_id, approval=candidate['approval'])
    assert service.inspect(matter.matter_id, ACTOR)[0]['rows'][0]['state'] == 'Unchanged'


def test_role_and_late_competing_support_changes_are_separate(case):
    store, matter, entities, assertions, first = case
    service = MatterContextService(assertions)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    for i in range(3):
        record = assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=record['revision'],
            support='b'*40, stance='competing', attributed_to=f'Late competing {i}')
    candidate = add(service, matter, 'assertion', record['assertion_id'])
    assert len(candidate['references']) == 4
    entities.update(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1, display_name='Alex corrected')
    row = service.inspect(matter.matter_id, ACTOR)[0]['rows'][0]
    assert row['warning'] == 'Review changes: roles'
    refs = {row['support_token']: row for row in map(reference, range(3))}
    refs['b'*40]['source_version_id'] = 'new-synthetic-version'
    changed_service = MatterContextService(services(store, refs)[1])
    row = changed_service.inspect(matter.matter_id, ACTOR)[0]['rows'][0]
    assert row['warning'] == 'Review changes: support, roles'
    assert row['current']['support_state'] == 'Source unavailable or changed'
    assert len([r for r in row['current']['references'] if not r['available']]) == 3
    assert row['approval'] == candidate['approval']


def test_concurrent_tabs_remove_reorder_missing_and_no_readd(case):
    store, matter, entities, assertions, first = case
    service = MatterContextService(assertions)
    other = entities.create(matter.matter_id, ACTOR, display_name=first['display_name'])
    add(service, matter, 'entity', first['entity_id'])
    add(service, matter, 'entity', other['entity_id'])
    original = service.inspect(matter.matter_id, ACTOR)[0]['entries']
    service.change(matter.matter_id, ACTOR, expected_revision=2, action='up', kind='entity', object_id=other['entity_id'])
    assert service.inspect(matter.matter_id, ACTOR)[0]['entries'][0]['approval'] == original[1]['approval']
    with pytest.raises(ContextConflict):
        service.change(matter.matter_id, ACTOR, expected_revision=2, action='clear')
    entities.delete(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1)
    state = service.inspect(matter.matter_id, ACTOR)[0]
    assert state['rows'][1]['state'] == 'Missing'
    service.change(matter.matter_id, ACTOR, expected_revision=3, action='remove', kind='entity', object_id=first['entity_id'])
    with pytest.raises(ContextConflict):
        service.change(matter.matter_id, ACTOR, expected_revision=4, action='reconcile', kind='entity',
                       object_id=first['entity_id'], approval=original[0]['approval'])
    assert len(service.inspect(matter.matter_id, ACTOR)[0]['entries']) == 1


def test_limits_before_materialization_and_removal_still_works(case, monkeypatch):
    store, matter, entities, assertions, first = case
    service = MatterContextService(assertions)
    add(service, matter, 'entity', first['entity_id'])
    with entities.repository.transaction(matter.matter_id, ACTOR):
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_entity SET aliases_json=? WHERE entity_id=?',
                (json.dumps(['Synthetic oversize ' * 50000]), first['entity_id']))
    monkeypatch.setattr(assertions.repository.entities, 'get', lambda *args: pytest.fail('Unbounded materialization'))
    selection, _ = service.inspect(matter.matter_id, ACTOR)
    assert selection['rows'][0]['state'] == 'Inspection unavailable'
    service.change(matter.matter_id, ACTOR, expected_revision=1, action='remove', kind='entity', object_id=first['entity_id'])
    assert not service.inspect(matter.matter_id, ACTOR)[0]['entries']


def test_cross_matter_and_revocation_roll_back(case):
    store, matter, entities, assertions, first = case
    service = MatterContextService(assertions)
    other = store.create_matter('Other synthetic matter', '', ACTOR)
    with pytest.raises(KeyError):
        service.inspect(other.matter_id, ACTOR, kind='entity', object_id=first['entity_id'])
    candidate = service.inspect(matter.matter_id, ACTOR, kind='entity', object_id=first['entity_id'])[1]
    calls = []
    def denied_late():
        calls.append(1)
        if len(calls) > 1:
            raise KeyError(ACTOR)
    with pytest.raises(KeyError):
        service.change(matter.matter_id, ACTOR, expected_revision=0, action='add', kind='entity',
                       object_id=first['entity_id'], approval=candidate['approval'], check_authority=denied_late)
    assert service.inspect(matter.matter_id, ACTOR)[0]['revision'] == 0


def test_migration_backup_clean_restore_restart_export_and_purge(case, tmp_path):
    store, matter, entities, assertions, first = case
    root = Path(__file__).resolve().parents[1]
    filename = '0035_matter_context_selection.sql'
    assert (root / 'migrations/sqlite' / filename).read_bytes() == (root / 'src/case_intelligence/migrations/sqlite' / filename).read_bytes()
    service = MatterContextService(assertions)
    add(service, matter, 'entity', first['entity_id'])
    expected = service.repository.export(matter.matter_id)
    backup = tmp_path / 'stopped-backup.sqlite'
    with sqlite3.connect(backup) as target:
        with store._lock:
            store.connection.backup(target)
    restore = tmp_path / 'clean' / 'workspace.sqlite'
    restore.parent.mkdir()
    with sqlite3.connect(backup) as source, sqlite3.connect(restore) as target:
        source.backup(target)
    for _ in range(2):
        restored = WorkspaceStore(restore)
        rs = MatterContextService(services(restored)[1])
        assert rs.repository.export(matter.matter_id) == expected
        assert rs.inspect(matter.matter_id, ACTOR)[0]['rows'][0]['state'] == 'Unchanged'
        assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
        restored.close()
    restored = WorkspaceStore(restore)
    _, lifecycle = claim_synthetic_exit(restored, matter, ACTOR)
    restored.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    for table in ('workbench_context_selection','workbench_context_entry'):
        assert restored.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
    restored.close()


def test_count_and_dependency_scan_bounds(case, monkeypatch):
    store, matter, entities, assertions, first = case
    service = MatterContextService(assertions)
    # Save fifty exact references without quadratic preview work in the fixture.
    entries = []
    for number in range(50):
        item = entities.create(matter.matter_id, ACTOR, display_name=f'Synthetic {number}')
        current = service.inspect(matter.matter_id, ACTOR, kind='entity', object_id=item['entity_id'])[1]
        entries.append(dict(kind='entity',object_id=item['entity_id'],approval=current['approval'],selected_by=ACTOR,selected_at=store._now()))
    with assertions.repository.transaction(matter.matter_id,ACTOR):
        service.repository.save(matter.matter_id,ACTOR,dict(revision=0),entries)
    candidate = service.inspect(matter.matter_id,ACTOR,kind='entity',object_id=first['entity_id'])[1]
    with pytest.raises(ContextLimit,match='50'):
        service.change(matter.matter_id,ACTOR,expected_revision=1,action='add',kind='entity',object_id=first['entity_id'],approval=candidate['approval'])
    assert len(service.inspect(matter.matter_id,ACTOR)[0]['entries']) == 50
    # Reference counts are checked before the existing unbounded mention loader.
    with entities.repository.transaction(matter.matter_id,ACTOR):
        for number in range(201):
            ref = dict(reference(0),support_token=f'{number:040x}')
            entities.repository.add_mention(matter.matter_id,ACTOR,first['entity_id'],ref,'manual')
    original_mentions = assertions.repository.entities.mentions
    def bounded_mentions(matter_id, entity_id):
        assert entity_id != first['entity_id'], 'Reference scan exceeded the preflight limit'
        return original_mentions(matter_id, entity_id)
    monkeypatch.setattr(assertions.repository.entities,'mentions',bounded_mentions)
    with pytest.raises(ContextLimit):
        service.inspect(matter.matter_id,ACTOR,kind='entity',object_id=first['entity_id'])


def test_exact_reference_metadata_and_source_guard_transaction(case):
    from contextlib import contextmanager
    store,matter,_,_,first = case
    guarded = []
    @contextmanager
    def guard():
        assert not store.connection.in_transaction
        guarded.append(True)
        try:
            yield
        finally:
            guarded.pop()
    entities,assertions = services(store,guard=guard)
    first = entities.attach(matter.matter_id,ACTOR,first['entity_id'],expected_revision=1,support='a'*40)
    validate = entities.validate_references
    def protected(references):
        assert guarded and store.connection.in_transaction
        return validate(references)
    entities.validate_references = protected
    service = MatterContextService(assertions)
    add(service,matter,'entity',first['entity_id'])
    with assertions.repository.transaction(matter.matter_id,ACTOR):
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_entity_mention SET excerpt_digest=? WHERE entity_id=?',('f'*64,first['entity_id']))
    row = service.inspect(matter.matter_id,ACTOR)[0]['rows'][0]
    assert row['state'] == 'Changed' and row['current']['support_state'] == 'Source unavailable or changed'
    assert not row['current']['references'][0]['available']


def test_real_separate_connections_cannot_overwrite_selection(case):
    store,matter,_,assertions,first = case
    service = MatterContextService(assertions)
    add(service,matter,'entity',first['entity_id'])
    filename = store.connection.execute('PRAGMA database_list').fetchone()[2]
    other = WorkspaceStore(Path(filename))
    try:
        second = MatterContextService(services(other)[1])
        stale = second.inspect(matter.matter_id,ACTOR)[0]
        service.change(matter.matter_id,ACTOR,expected_revision=1,action='clear')
        with pytest.raises(ContextConflict):
            second.change(matter.matter_id,ACTOR,expected_revision=stale['revision'],action='up',kind='entity',object_id=first['entity_id'])
        assert second.inspect(matter.matter_id,ACTOR)[0]['entries'] == []
    finally:
        other.close()
