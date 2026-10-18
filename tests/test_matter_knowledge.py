"""Synthetic saved-knowledge projection: bounds, provenance and authority."""

from tests.fixture_write import fixture_write
from contextlib import contextmanager

import pytest

from case_intelligence.matter_knowledge import MatterKnowledgeService
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_evidence_assertions import ACTOR, reference, services, setup, values


@pytest.fixture
def knowledge_case(tmp_path):
    context = setup(tmp_path)
    try:
        yield context
    finally:
        context[0].close()


def test_same_names_aliases_dates_status_and_late_competing_accounts_without_writes(knowledge_case):
    store, matter, entities, assertions, center = knowledge_case
    other = entities.create(matter.matter_id, ACTOR, display_name=center['display_name'], support='a' * 40)
    entities.update(matter.matter_id, ACTOR, center['entity_id'], expected_revision=1,
                    display_name=center['display_name'], aliases='A. Example')
    center = entities.detail(matter.matter_id, ACTOR, center['entity_id'])[0]
    record = assertions.create(matter.matter_id, ACTOR, **(values(center) | dict(status='confirmed')))
    for number in range(5):
        record = assertions.attach(matter.matter_id, ACTOR, record['assertion_id'],
            expected_revision=record['revision'], support='a' * 40,
            stance='supporting', attributed_to=f'Synthetic supporter {number}')
    assertions.attach(matter.matter_id, ACTOR, record['assertion_id'],
        expected_revision=record['revision'], support='b' * 40,
        stance='competing', attributed_to='Late competing account')
    before = store.connection.total_changes
    result = MatterKnowledgeService(assertions).page(matter.matter_id, ACTOR)
    assert store.connection.total_changes == before
    identities = result['entities']['items']
    assert {row['entity_id'] for row in identities} == {center['entity_id'], other['entity_id']}
    assert len({row['display_name'] for row in identities}) == 1
    assert next(row for row in identities if row['entity_id'] == center['entity_id'])['aliases'] == ['A. Example']
    saved = result['assertions']['items'][0]
    assert saved['status'] == 'confirmed' and saved['raw_date'] == values(center)['raw_date']
    assert saved['date_uncertainty'] == values(center)['date_uncertainty'] and not saved['sort_date']
    assert len(saved['stances']['supporting']['accounts']) == 2
    assert saved['stances']['supporting']['total'] == 6
    assert saved['stances']['supporting']['omitted'] == 4
    assert saved['stances']['competing']['accounts'][0]['attributed_to'] == 'Late competing account'
    assert saved['stances']['competing']['omitted'] == 0
    assert {role['entity_id'] for role in assertions.repository.roles(matter.matter_id, record['assertion_id'])} == {center['entity_id']}


def test_independent_pages_clamp_after_deletion_and_no_histories(knowledge_case, monkeypatch):
    store, matter, entities, assertions, center = knowledge_case
    for number in range(10):
        entities.create(matter.matter_id, ACTOR, display_name=f'Synthetic identity {number:02d}')
    expected = []
    for day in range(1, 9):
        record = assertions.create(matter.matter_id, ACTOR,
            **(values(center) | dict(sort_date=f'2026-05-{day:02d}')))
        expected.append(record['assertion_id'])
    def forbidden(*args, **kwargs):
        pytest.fail('A preview must not load complete details or histories.')
    monkeypatch.setattr(entities, 'detail', forbidden)
    monkeypatch.setattr(assertions, 'detail', forbidden)
    projection = MatterKnowledgeService(assertions)
    traced = []
    with store._lock:
        store.connection.set_trace_callback(traced.append)
    first = projection.page(matter.matter_id, ACTOR)
    with store._lock:
        store.connection.set_trace_callback(None)
    second = projection.page(matter.matter_id, ACTOR, entity_page=2, assertion_page=2)
    assert len(first['entities']['items']) == 8 and first['entities']['omitted'] == 3
    assert len(second['entities']['items']) == 3 and second['entities']['omitted'] == 8
    assert {row['entity_id'] for row in first['entities']['items']}.isdisjoint(
        row['entity_id'] for row in second['entities']['items'])
    assert [row['assertion_id'] for row in first['assertions']['items']] == expected[:6]
    assert [row['assertion_id'] for row in second['assertions']['items']] == expected[6:]
    assert not any('_history' in sql for sql in traced)
    for identifier in expected[6:]:
        assertions.delete(matter.matter_id, ACTOR, identifier, expected_revision=1)
    clamped = projection.page(matter.matter_id, ACTOR, entity_page=100_000, assertion_page=2)
    assert clamped['entities']['page'] == 2 and clamped['assertions']['page'] == 1
    assert clamped['assertions']['total'] == 6


def test_reference_validation_batched_inside_source_guard_and_same_transaction(knowledge_case):
    store, matter, _, _, center = knowledge_case
    refs = {row['support_token']: row for row in map(reference, range(3))}
    guarded = []
    @contextmanager
    def guard():
        assert not store.connection.in_transaction
        guarded.append(True)
        try:
            yield
        finally:
            guarded.pop()
    entities, assertions = services(store, refs, guard=guard)
    center = entities.attach(matter.matter_id, ACTOR, center['entity_id'],
        expected_revision=center['revision'], support='a' * 40)
    record = assertions.create(matter.matter_id, ACTOR, **values(center))
    assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
        support='b' * 40, stance='competing', attributed_to='Synthetic competing speaker')
    # Keep the token, but invalidate its original version. Corrupt a second
    # reference sharing a valid token: it must not borrow the mention's validity.
    refs['b' * 40] = dict(refs['b' * 40], source_version_id='synthetic-replacement')
    with assertions.repository.transaction(matter.matter_id, ACTOR):
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_assertion_account SET excerpt_digest=? '
                'WHERE assertion_id=? AND stance=?', ('f' * 64, record['assertion_id'], 'supporting'))
    calls = []
    validate = entities.validate_references
    def checked(references):
        assert guarded and store.connection.in_transaction
        calls.append(references)
        return validate(references)
    entities.validate_references = checked
    result = MatterKnowledgeService(assertions).page(matter.matter_id, ACTOR)
    assert len(calls) == 1 and len(calls[0]) == 3
    assert result['entities']['items'][0]['mentions'][0]['available']
    for group in result['assertions']['items'][0]['stances'].values():
        assert not group['accounts'][0]['available']
    assert result['assertions']['items'][0]['stances']['competing']['accounts'][0]['excerpt'] == reference(1)['excerpt']


def test_mention_bounds_and_separate_review_status(knowledge_case):
    _, matter, entities, assertions, center = knowledge_case
    for token in ('a' * 40, 'b' * 40, 'c' * 40):
        center = entities.attach(matter.matter_id, ACTOR, center['entity_id'],
            expected_revision=center['revision'], support=token)
    projection = MatterKnowledgeService(assertions).page(matter.matter_id, ACTOR)
    identity = projection['entities']['items'][0]
    assert identity['reference_total'] == 3 and identity['references_omitted'] == 1
    assert len(identity['mentions']) == 2
    assert all('review_status' in mention and mention['available'] for mention in identity['mentions'])


def test_foreign_matter_and_revocation_fail_before_return(knowledge_case):
    store, matter, entities, assertions, center = knowledge_case
    assertions.create(matter.matter_id, ACTOR, **values(center))
    other = store.create_matter('Other synthetic matter', 'Synthetic', ACTOR)
    projection = MatterKnowledgeService(assertions)
    result = projection.page(other.matter_id, ACTOR)
    assert result['entities']['total'] == result['assertions']['total'] == 0
    with pytest.raises(KeyError):
        projection.page(matter.matter_id, 'synthetic-outsider')
    checks = []
    authorize = assertions.repository.entities.authorize
    def revoked_on_completion(*args):
        checks.append(True)
        if len(checks) > 1:
            raise KeyError('Synthetic revocation')
        return authorize(*args)
    assertions.repository.entities.authorize = revoked_on_completion
    with pytest.raises(KeyError):
        projection.page(matter.matter_id, ACTOR)
    assert len(checks) == 2


@pytest.mark.parametrize('page', [0, -1, 100_001, True, '1'])
def test_invalid_page_rejected(knowledge_case, page):
    _, matter, _, assertions, _ = knowledge_case
    with pytest.raises(WorkspaceProblem):
        MatterKnowledgeService(assertions).page(matter.matter_id, ACTOR, assertion_page=page)


def test_oversized_saved_excerpt_is_not_silently_truncated_to_valid_support(knowledge_case):
    store, matter, _, assertions, center = knowledge_case
    assertions.create(matter.matter_id, ACTOR, **values(center))
    with assertions.repository.transaction(matter.matter_id, ACTOR):
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_assertion_account SET excerpt=? WHERE matter_id=?',
                                     ('x' * 6001, matter.matter_id))
    with pytest.raises(WorkspaceProblem, match='preview limit'):
        MatterKnowledgeService(assertions).page(matter.matter_id, ACTOR)


def test_dismissed_records_remain_explicit_and_source_status_stays_independent(knowledge_case):
    _, matter, entities, assertions, center = knowledge_case
    center = entities.update(matter.matter_id, ACTOR, center['entity_id'],
        expected_revision=1, display_name=center['display_name'], status='dismissed')
    assertions.create(matter.matter_id, ACTOR, **(values(center) | dict(status='dismissed')))
    projection = MatterKnowledgeService(assertions).page(matter.matter_id, ACTOR)
    assert projection['entities']['items'][0]['status'] == 'dismissed'
    record = projection['assertions']['items'][0]
    assert record['status'] == 'dismissed'
    assert record['stances']['supporting']['accounts'][0]['available']
