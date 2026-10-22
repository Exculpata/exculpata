"""Synthetic service acceptance for explicit identities and contradictory originals."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from tests.case_lifecycle_helpers import claim_synthetic_exit

from case_intelligence.assertion_repository import AssertionEditConflict, AssertionRepository, TABLES
from case_intelligence.assertion_service import AssertionService
from case_intelligence.entity_repository import EntityEditConflict
from case_intelligence.entity_service import EntityService, REFERENCE_FIELDS
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

ACTOR = 'synthetic-assertion-reviewer'
PASSAGES = (
    'Alex Example said Blair Sample delivered the synthetic parcel around the first Friday in May.',
    'Casey Specimen said Blair Sample never delivered the synthetic parcel.',
    'Alex Example, an unrelated same-name archivist, visited the library. Dana Demo was also named; no relationship was stated.',
)


def reference(index):
    letter = 'abc'[index]
    return dict(document_id='document-' + letter * 32, source_version_id='version-' + letter * 32,
        source_name='Synthetic account ' + letter + '.txt', location='Lines 1–1', unit_number=1,
        chunk_id='chunk-1', excerpt_digest=letter * 64, excerpt=PASSAGES[index], support_token=letter * 40)


def services(store, references=None, guard=nullcontext):
    refs = references if references is not None else {row['support_token']: row for row in map(reference, range(3))}
    entity = EntityService(store.entity_repository(), source_guard=guard,
        resolve_support=lambda token: refs[token], load_note=store.notebook_item, load_references=store.notebook_references,
        validate_references=lambda values: frozenset(index for index, value in enumerate(values)
            if value['support_token'] in refs and all(refs[value['support_token']][key] == value[key] for key in REFERENCE_FIELDS)))
    return entity, AssertionService(AssertionRepository(store.entity_repository()), entity)


def setup(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite')
    store.upsert_principal('test', ACTOR, 'Synthetic reviewer', ACTOR, preferred_principal_id=ACTOR)
    matter = store.create_matter('Synthetic conflicting event accounts', '', ACTOR)
    entities, assertions = services(store)
    first = entities.create(matter.matter_id, ACTOR, display_name='Alex Example')
    return store, matter, entities, assertions, first


def values(entity, **changes):
    return dict(title='Synthetic parcel account', statement='Alex reports a parcel delivery.',
        record_type='event', raw_date=' around the first Friday in May ', date_uncertainty='Year and calendar day are not stated.',
        support='a' * 40, attributed_to='Alex Example in account A',
        roles=[dict(entity_id=entity['entity_id'], expected_revision=entity['revision'], role='speaker')], **changes)


def update_values(**changes):
    return dict(title='Synthetic corrected account', statement='The delivery account remains contested.',
                raw_date='around the first Friday in May', date_uncertainty='Year unknown', **changes)


def test_manual_two_accounts_independent_human_review_and_frozen_same_name_roles(tmp_path):
    store, matter, entities, assertions, first = setup(tmp_path)
    same_name = entities.create(matter.matter_id, ACTOR, display_name='Alex Example', support='c' * 40)
    co_mentioned = entities.create(matter.matter_id, ACTOR, display_name='Dana Demo', support='c' * 40)
    assert assertions.list(matter.matter_id, ACTOR) == ([], 0)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    assert record['raw_date'] == ' around the first Friday in May ' and record['sort_date'] == ''
    assert record['origin'] == 'manual'
    record = assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
        support='b' * 40, stance='competing', attributed_to='Casey Specimen in account B')
    assert record['status'] == 'needs_review'
    record = assertions.update(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=2,
                               **update_values(status='confirmed'))
    detail = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])
    assert {account['stance'] for account in detail['accounts']} == {'supporting', 'competing'}
    assert all(account['available'] for account in detail['accounts'])
    assert {role['entity_id'] for role in detail['roles']} == {first['entity_id']}
    for unrelated in (same_name, co_mentioned):
        assert assertions.list(matter.matter_id, ACTOR, entity_id=unrelated['entity_id']) == ([], 0)
    assert len(assertions.chronology_export(matter.matter_id, ACTOR)['records']) == 1
    exported = assertions.export(matter.matter_id, ACTOR, record['assertion_id'])
    assert exported['record']['status'] == 'confirmed'
    assert exported['accounts'] == detail['accounts']
    entities.update(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1, display_name='Alex Corrected')
    role = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])['roles'][0]
    assert role['identity_state'] == 'changed' and role['display_name'] == 'Alex Example'
    assert role['current_display_name'] == 'Alex Corrected' and role['entity_revision'] == 1
    entities.delete(matter.matter_id, ACTOR, first['entity_id'], expected_revision=2)
    role = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])['roles'][0]
    assert role['identity_state'] == 'missing' and role['entity_id'] == first['entity_id']
    assert assertions.list(matter.matter_id, ACTOR, entity_id=first['entity_id'])[1] == 1
    store.close()


def test_corrections_keep_one_account_and_role_with_linear_excerpt_history(tmp_path):
    store, matter, entities, assertions, first = setup(tmp_path)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    assertion_id = record['assertion_id']
    original = assertions.detail(matter.matter_id, ACTOR, assertion_id)
    account_id, role_id = original['accounts'][0]['account_id'], original['roles'][0]['role_id']
    for mutation in (
        lambda: assertions.remove_account(matter.matter_id, ACTOR, assertion_id, expected_revision=1, account_id=account_id),
        lambda: assertions.revise_account(matter.matter_id, ACTOR, assertion_id, expected_revision=1,
            account_id=account_id, stance='competing', attributed_to='Corrected attribution'),
        lambda: assertions.remove_role(matter.matter_id, ACTOR, assertion_id, expected_revision=1, role_id=role_id),
    ):
        with pytest.raises(WorkspaceProblem, match='at least one'):
            mutation()
        assert assertions.detail(matter.matter_id, ACTOR, assertion_id) == original
    repeated = assertions.attach(matter.matter_id, ACTOR, assertion_id, expected_revision=1,
        support='a' * 40, attributed_to='Alex Example in account A')
    assert repeated == record
    with pytest.raises(WorkspaceProblem, match='already attached'):
        assertions.attach(matter.matter_id, ACTOR, assertion_id, expected_revision=1,
            support='a' * 40, attributed_to='Alex Example in account A', stance='competing')
    record = assertions.attach(matter.matter_id, ACTOR, assertion_id, expected_revision=1,
        support='a' * 40, attributed_to='A separately attributed claim', stance='competing')
    record = assertions.revise_account(matter.matter_id, ACTOR, assertion_id, expected_revision=2,
        account_id=account_id, stance='supporting', attributed_to='Alex, as quoted by account A')
    for revision in range(3, 8):
        record = assertions.update(matter.matter_id, ACTOR, assertion_id, expected_revision=revision,
            **update_values(status='disputed'))
    detail = assertions.detail(matter.matter_id, ACTOR, assertion_id)
    # Exactly the two explicit additions carry the excerpt, not every review edit.
    histories = [json.loads(row['snapshot_json']) for row in detail['history']]
    assert sum(len(row.get('added_accounts', [])) for row in histories) == 2
    assert sum(PASSAGES[0] in row['snapshot_json'] for row in detail['history']) == 2
    assert histories[-3]['corrected_accounts'][0]['after']['attributed_to'] == 'Alex, as quoted by account A'
    replacement = entities.create(matter.matter_id, ACTOR, display_name='Blair Sample')
    record = assertions.add_role(matter.matter_id, ACTOR, assertion_id, expected_revision=8,
        entity_id=replacement['entity_id'], entity_revision=1, role='subject')
    record = assertions.remove_role(matter.matter_id, ACTOR, assertion_id, expected_revision=9, role_id=role_id)
    competing = next(row for row in detail['accounts'] if row['stance'] == 'competing')
    record = assertions.remove_account(matter.matter_id, ACTOR, assertion_id, expected_revision=10, account_id=competing['account_id'])
    detail = assertions.detail(matter.matter_id, ACTOR, assertion_id)
    assert len(detail['roles']) == len(detail['accounts']) == 1
    assert json.loads(detail['history'][0]['snapshot_json'])['removed_accounts'][0]['excerpt'] == PASSAGES[0]
    assertions.delete(matter.matter_id, ACTOR, assertion_id, expected_revision=11)
    assert all(store.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0 for table in TABLES)
    assert entities.list(matter.matter_id, ACTOR)[1] == 2
    store.close()


def test_stale_entity_source_and_interrupted_commit_fail_atomically(tmp_path, monkeypatch):
    store, matter, entities, assertions, first = setup(tmp_path)
    entities.update(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1, display_name='Alex Corrected')
    with pytest.raises(EntityEditConflict):
        assertions.create(matter.matter_id, ACTOR, **values(first))
    assert assertions.list(matter.matter_id, ACTOR)[1] == 0
    first = entities.detail(matter.matter_id, ACTOR, first['entity_id'])[0]
    refs = {reference(0)['support_token']: reference(0)}
    _, assertions = services(store, refs)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    before = assertions.export(matter.matter_id, ACTOR, record['assertion_id'])
    with pytest.raises(WorkspaceProblem, match='unavailable'):
        assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
            support='b' * 40, attributed_to='Missing source')
    assert assertions.export(matter.matter_id, ACTOR, record['assertion_id']) == before
    refs['a' * 40] = dict(reference(0), source_version_id='changed-original')
    detail = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])
    assert not detail['accounts'][0]['available'] and detail['accounts'][0]['excerpt'] == PASSAGES[0]
    # An interruption after all writes but before transaction commit cannot
    # leave a newer revision, attribution, history, or role behind.
    def interrupted(*args):
        raise RuntimeError('Synthetic interruption before commit')
    monkeypatch.setattr(assertions.repository, 'check_limits', interrupted)
    with pytest.raises(RuntimeError, match='interruption'):
        assertions.revise_account(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
            account_id=detail['accounts'][0]['account_id'], stance='supporting', attributed_to='Unsaved attribution')
    assert assertions.detail(matter.matter_id, ACTOR, record['assertion_id']) == detail
    store.close()
    restarted = WorkspaceStore(tmp_path / 'workspace.sqlite')
    assert services(restarted, refs)[1].detail(matter.matter_id, ACTOR, record['assertion_id']) == detail
    restarted.close()


def test_source_guard_spans_commit_and_new_role_checks_entity_revision(tmp_path):
    store, matter, entities, assertions, first = setup(tmp_path)
    states = []
    @contextmanager
    def guard():
        states.append(('entered', store.connection.in_transaction))
        yield
        states.append(('exited', store.connection.in_transaction))
    entities, assertions = services(store, guard=guard)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    assert states == [('entered', False), ('exited', False)]
    entities.update(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1, display_name='Changed label')
    with pytest.raises(EntityEditConflict):
        assertions.add_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
            entity_id=first['entity_id'], entity_revision=1, role='subject')
    assert assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])['record']['revision'] == 1
    store.close()


def test_sole_role_can_be_refreshed_or_replaced_without_losing_original_identity(tmp_path):
    store, matter, entities, assertions, first = setup(tmp_path)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    role_id = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])['roles'][0]['role_id']
    entities.update(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1, display_name='Alex Corrected')
    record = assertions.revise_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
        role_id=role_id, entity_id=first['entity_id'], entity_revision=2, role='speaker')
    detail = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])
    assert len(detail['roles']) == 1 and detail['roles'][0]['identity_state'] == 'current'
    corrected = json.loads(detail['history'][0]['snapshot_json'])['corrected_roles'][0]
    assert corrected['before']['display_name'] == 'Alex Example' and corrected['after']['display_name'] == 'Alex Corrected'
    replacement = entities.create(matter.matter_id, ACTOR, display_name='Separate Alex Example')
    record = assertions.revise_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=2,
        role_id=role_id, entity_id=replacement['entity_id'], entity_revision=1, role='subject')
    assert assertions.list(matter.matter_id, ACTOR, entity_id=first['entity_id']) == ([], 0)
    assert assertions.list(matter.matter_id, ACTOR, entity_id=replacement['entity_id'])[1] == 1
    record = assertions.add_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=3,
        entity_id=first['entity_id'], entity_revision=2, role='speaker')
    before = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])
    with pytest.raises(WorkspaceProblem, match='another attachment'):
        assertions.revise_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=4,
            role_id=role_id, entity_id=first['entity_id'], entity_revision=2, role='speaker')
    assert assertions.detail(matter.matter_id, ACTOR, record['assertion_id']) == before
    store.close()


def test_two_connection_shared_edits_and_revoked_authority(tmp_path):
    store, matter, entities, assertions, first = setup(tmp_path)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    member = 'synthetic-second-reviewer'
    store.upsert_principal('test', member, 'Synthetic second reviewer', member, preferred_principal_id=member)
    store.add_member(matter.matter_id, member, ACTOR)
    other = store.create_matter('Synthetic other matter', '', ACTOR)
    other_entity = entities.create(other.matter_id, ACTOR, display_name='Synthetic other identity')
    with pytest.raises(KeyError):
        assertions.detail(other.matter_id, ACTOR, record['assertion_id'])
    with pytest.raises(KeyError):
        assertions.add_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
            entity_id=other_entity['entity_id'], entity_revision=1, role='subject')
    second = WorkspaceStore(store.path)
    second_service = services(second)[1]
    barrier = threading.Barrier(2)
    def save(svc, actor, status):
        barrier.wait(timeout=5)
        try:
            svc.update(matter.matter_id, actor, record['assertion_id'], expected_revision=1, **update_values(status=status))
            return 'saved'
        except AssertionEditConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(save, svc, actor, status) for svc, actor, status in
            ((assertions, ACTOR, 'confirmed'), (second_service, member, 'disputed'))]
        assert sorted(job.result(timeout=10) for job in jobs) == ['conflict', 'saved']
    detail = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])
    for mutate in (
        lambda: assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1, support='b' * 40, attributed_to='Stale'),
        lambda: assertions.remove_account(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1, account_id=detail['accounts'][0]['account_id']),
        lambda: assertions.remove_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1, role_id=detail['roles'][0]['role_id']),
        lambda: assertions.delete(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1),
    ):
        with pytest.raises(AssertionEditConflict):
            mutate()
    store.revoke_member(matter.matter_id, member, ACTOR)
    for operation in (
        lambda: second_service.list(matter.matter_id, member),
        lambda: second_service.detail(matter.matter_id, member, record['assertion_id']),
        lambda: second_service.export(matter.matter_id, member, record['assertion_id']),
        lambda: second_service.chronology_export(matter.matter_id, member),
        lambda: second_service.update(matter.matter_id, member, record['assertion_id'], expected_revision=2, **update_values()),
        lambda: second_service.delete(matter.matter_id, member, record['assertion_id'], expected_revision=2),
    ):
        with pytest.raises(KeyError):
            operation()
    second.close()
    store.close()


@pytest.mark.parametrize('changes', [
    {'title': ''}, {'statement': ''}, {'status': 'suggested'}, {'record_type': 'relationship_inference'},
    {'sort_date': '2026-02-30'}, {'sort_date': '20260912'}, {'sort_date': '2026-09'},
    {'sort_date': '2026-09-12 '}, {'sort_date': '٢٠٢٦-٠٩-١٢'}, {'raw_date': 'May\tFriday'},
    {'statement': 'Claim\x00text'}, {'attributed_to': ''}, {'roles': []},
    {'title': '\tSynthetic title'}, {'raw_date': 'May\x85Friday'},
])
def test_validation_refuses_unsupported_normalization_or_provenance(tmp_path, changes):
    store, matter, entities, assertions, first = setup(tmp_path)
    with pytest.raises(WorkspaceProblem):
        assertions.create(matter.matter_id, ACTOR, **(values(first) | changes))
    assert assertions.list(matter.matter_id, ACTOR)[1] == 0
    store.close()


def test_paginated_chronology_explicit_dates_no_inference_and_export_bounds(tmp_path, monkeypatch):
    import case_intelligence.assertion_service as service_module
    store, matter, entities, assertions, first = setup(tmp_path)
    for index in range(53):
        assertions.create(matter.matter_id, ACTOR, **(values(first) | dict(
            title='Synthetic record ' + str(index), sort_date='2026-09-12' if index % 2 else '')))
    page1, count = assertions.list(matter.matter_id, ACTOR)
    page2, _ = assertions.list(matter.matter_id, ACTOR, page=2)
    assert count == 53 and len(page1) == 50 and len(page2) == 3
    rows = page1 + page2
    assert all(row['sort_date'] for row in rows[:26]) and not any(row['sort_date'] for row in rows[26:])
    assert assertions.list(matter.matter_id, ACTOR, date_group='undated')[1] == 27
    assert assertions.list(matter.matter_id, ACTOR, date_group='dated')[1] == 26
    assert len(assertions.chronology_export(matter.matter_id, ACTOR)['records']) == 53
    monkeypatch.setattr(service_module, 'MAX_RECORDS', 52)
    with pytest.raises(WorkspaceProblem, match='No partial export'):
        assertions.chronology_export(matter.matter_id, ACTOR)
    monkeypatch.setattr(service_module, 'MAX_RECORDS', 5000)
    monkeypatch.setattr(service_module, 'MAX_STORAGE_BYTES', 100)
    with pytest.raises(WorkspaceProblem, match='No partial export'):
        assertions.chronology_export(matter.matter_id, ACTOR)
    store.close()


def test_storage_caps_rollback_all_new_writes_but_allow_record_removal(tmp_path, monkeypatch):
    import case_intelligence.assertion_repository as repository_module
    store, matter, entities, assertions, first = setup(tmp_path)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    before = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])
    monkeypatch.setattr(repository_module, 'MAX_STORAGE_BYTES', 1)
    with pytest.raises(WorkspaceProblem, match='storage limit'):
        assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
            support='b' * 40, stance='competing', attributed_to='Unsaved account')
    assert assertions.detail(matter.matter_id, ACTOR, record['assertion_id']) == before
    assertions.delete(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1)
    assert assertions.list(matter.matter_id, ACTOR)[1] == 0
    store.close()


def test_multiline_statement_and_same_name_reconciliation_never_rebind_roles(tmp_path):
    store, matter, entities, assertions, first = setup(tmp_path)
    target = entities.create(matter.matter_id, ACTOR, display_name='Alex Example')
    record = assertions.create(matter.matter_id, ACTOR, **(values(first) | {'statement': 'Account A claims delivery.\r\nAccount B denies it.'}))
    assert record['statement'] == 'Account A claims delivery.\nAccount B denies it.'
    operation = entities.reconcile(matter.matter_id, ACTOR, first['entity_id'], expected_revision=1,
        target_id=target['entity_id'], target_revision=1, action='merge')
    detail = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])
    assert detail['roles'][0]['entity_id'] == first['entity_id'] and detail['roles'][0]['identity_state'] == 'changed'
    assert assertions.list(matter.matter_id, ACTOR, entity_id=target['entity_id']) == ([], 0)
    entities.undo(matter.matter_id, ACTOR, operation)
    role = assertions.detail(matter.matter_id, ACTOR, record['assertion_id'])['roles'][0]
    assert role['entity_id'] == first['entity_id'] and role['entity_revision'] == 1
    assert role['current_revision'] == 3 and role['identity_state'] == 'changed'
    store.close()


def test_migration_mirror_backup_clean_restore_export_and_purge(tmp_path):
    root = Path(__file__).parents[1]
    migration = 'migrations/sqlite/0034_evidence_assertions.sql'
    assert (root / migration).read_bytes() == (root / 'src/case_intelligence' / migration).read_bytes()
    store, matter, entities, assertions, first = setup(tmp_path)
    record = assertions.create(matter.matter_id, ACTOR, **values(first))
    record = assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
        support='b' * 40, stance='competing', attributed_to='Casey Specimen')
    expected = assertions.export(matter.matter_id, ACTOR, record['assertion_id'])
    backup = tmp_path / 'backup.sqlite'
    with sqlite3.connect(backup) as target:
        with store._lock:
            store.connection.backup(target)
    restored_path = tmp_path / 'clean-restore' / 'workspace.sqlite'
    restored_path.parent.mkdir()
    with sqlite3.connect(backup) as source, sqlite3.connect(restored_path) as target:
        source.backup(target)
    restored = WorkspaceStore(restored_path)
    restored_assertions = services(restored)[1]
    assert restored_assertions.export(matter.matter_id, ACTOR, record['assertion_id']) == expected
    repository = AssertionRepository(restored.entity_repository())
    with repository.transaction(matter.matter_id, ACTOR):
        retained = list(repository.export_records(matter.matter_id))[0]
    assert 'not revalidated' in retained['source_support']
    assert retained['history'] == expected['history'] and 'available' not in retained['accounts'][0]
    assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
    _, lifecycle = claim_synthetic_exit(restored, matter, ACTOR)
    with pytest.raises(KeyError):
        restored_assertions.list(matter.matter_id, ACTOR)
    restored.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
    assert all(restored.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0 for table in TABLES)
    restored.close()
    store.close()
