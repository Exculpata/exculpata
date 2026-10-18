"""Synthetic graph neighborhoods preserve explicit roles and exact source accounts."""

from tests.fixture_write import fixture_write
from contextlib import contextmanager

import pytest

from case_intelligence.evidence_graph import EvidenceGraphService
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_evidence_assertions import ACTOR, reference, services, setup, values


@pytest.fixture
def graph_case(tmp_path):
    store, matter, entities, assertions, center = setup(tmp_path)
    try:
        yield store, matter, entities, assertions, center
    finally:
        store.close()


def test_explicit_neighborhood_keeps_same_names_competing_accounts_and_uncertain_dates(graph_case):
    store, matter, entities, assertions, center = graph_case
    same_name = entities.create(matter.matter_id, ACTOR, display_name=center['display_name'], support='c' * 40)
    neighbor = entities.create(matter.matter_id, ACTOR, display_name='Blair Sample', support='a' * 40)
    record = assertions.create(matter.matter_id, ACTOR, **values(center))
    record = assertions.add_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=record['revision'],
        entity_id=neighbor['entity_id'], entity_revision=neighbor['revision'], role='participant')
    assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=record['revision'],
        support='b' * 40, stance='competing', attributed_to='Casey Specimen')
    graph = EvidenceGraphService(assertions)
    before = store.connection.total_changes
    result = graph.neighborhood(matter.matter_id, ACTOR, center['entity_id'])
    assert store.connection.total_changes == before
    assert result['center']['entity_id'] == center['entity_id']
    assert (result['total_records'], result['shown_records'], result['records_omitted']) == (1, 1, 0)
    row = result['records'][0]
    assert row['record']['raw_date'] == ' around the first Friday in May '
    assert row['record']['date_uncertainty'] == 'Year and calendar day are not stated.'
    assert row['record']['sort_date'] == '' and row['record']['status'] == 'needs_review'
    assert row['center_identity_state'] == 'current'
    assert {role['entity_id'] for role in row['roles']} == {center['entity_id'], neighbor['entity_id']}
    assert all(role['traversable'] for role in row['roles'])
    assert {account['stance'] for account in row['accounts']} == {'supporting', 'competing'}
    assert all(account['available'] for account in row['accounts'])
    assert row['account_totals'] == {'supporting': 1, 'competing': 1}
    assert all('history' not in item for item in (row, result))
    assert graph.neighborhood(matter.matter_id, ACTOR, neighbor['entity_id'])['total_records'] == 1
    assert graph.neighborhood(matter.matter_id, ACTOR, same_name['entity_id'])['records'] == []


def test_changed_and_deleted_roles_keep_saved_identity_without_traversal(graph_case):
    _, matter, entities, assertions, center = graph_case
    neighbor = entities.create(matter.matter_id, ACTOR, display_name='Blair Sample')
    record = assertions.create(matter.matter_id, ACTOR, **values(center))
    assertions.add_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=record['revision'],
        entity_id=neighbor['entity_id'], entity_revision=neighbor['revision'], role='participant')
    entities.update(matter.matter_id, ACTOR, center['entity_id'], expected_revision=1, display_name='Alex Corrected')
    entities.update(matter.matter_id, ACTOR, neighbor['entity_id'], expected_revision=1, display_name='Blair Corrected')
    graph = EvidenceGraphService(assertions)
    row = graph.neighborhood(matter.matter_id, ACTOR, center['entity_id'])['records'][0]
    assert row['center_identity_state'] == 'changed'
    assert all(role['identity_state'] == 'changed' and not role['traversable'] for role in row['roles'])
    saved = next(role for role in row['roles'] if role['entity_id'] == neighbor['entity_id'])
    assert saved['display_name'] == 'Blair Sample' and saved['current_display_name'] == 'Blair Corrected'
    entities.delete(matter.matter_id, ACTOR, neighbor['entity_id'], expected_revision=2)
    row = graph.neighborhood(matter.matter_id, ACTOR, center['entity_id'])['records'][0]
    missing = next(role for role in row['roles'] if role['entity_id'] == neighbor['entity_id'])
    assert missing['identity_state'] == 'missing' and not missing['traversable']
    assert missing['display_name'] == 'Blair Sample'
    with pytest.raises(KeyError):
        graph.neighborhood(matter.matter_id, ACTOR, neighbor['entity_id'])


def test_each_account_reference_checked_once_under_source_guard_and_authorized_transaction(graph_case):
    store, matter, _, _, center = graph_case
    refs = {row['support_token']: row for row in map(reference, range(3))}
    guard_active = []
    @contextmanager
    def guard():
        assert not store.connection.in_transaction
        guard_active.append(True)
        try:
            yield
        finally:
            guard_active.pop()
    entities, assertions = services(store, refs, guard=guard)
    for _ in range(2):
        record = assertions.create(matter.matter_id, ACTOR, **values(center))
        assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=1,
            support='b' * 40, stance='competing', attributed_to='Casey Specimen')
    # One token still resolves but its exact saved version does not. Another
    # reference uses the same token with a different saved digest and must not
    # borrow availability from the first occurrence.
    refs['b' * 40] = dict(refs['b' * 40], source_version_id='replacement-source-version')
    with assertions.repository.transaction(matter.matter_id, ACTOR):
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_assertion_account SET excerpt_digest=? '
                'WHERE assertion_id=? AND stance=?', ('d' * 64, record['assertion_id'], 'supporting'))
    calls = []
    validate = entities.validate_references
    def checked(references):
        assert guard_active and store.connection.in_transaction
        calls.append([dict(row) for row in references])
        return validate(references)
    entities.validate_references = checked
    result = EvidenceGraphService(assertions).neighborhood(matter.matter_id, ACTOR, center['entity_id'])
    assert len(calls) == 1 and len(calls[0]) == 4
    accounts = [account for row in result['records'] for account in row['accounts']]
    assert sum(account['available'] for account in accounts) == 1
    assert all(account['excerpt'] == reference(1)['excerpt'] and not account['available']
               for account in accounts if account['stance'] == 'competing')


def test_record_pagination_is_stable_bounded_and_does_not_load_histories(graph_case, monkeypatch):
    store, matter, entities, assertions, center = graph_case
    expected = []
    for day in range(1, 10):
        record = assertions.create(matter.matter_id, ACTOR, **(values(center) | {'sort_date': f'2026-05-{day:02d}'}))
        expected.append(record['assertion_id'])
    def forbidden(*args, **kwargs):
        pytest.fail('Graph must not load a full detail, mention list or correction history.')
    monkeypatch.setattr(assertions, 'detail', forbidden)
    monkeypatch.setattr(assertions.repository, 'history', forbidden)
    monkeypatch.setattr(entities, 'detail', forbidden)
    monkeypatch.setattr(entities.repository, 'mentions', forbidden)
    graph = EvidenceGraphService(assertions)
    traced = []
    with store._lock:
        store.connection.set_trace_callback(traced.append)
    first = graph.neighborhood(matter.matter_id, ACTOR, center['entity_id'])
    with store._lock:
        store.connection.set_trace_callback(None)
    second = graph.neighborhood(matter.matter_id, ACTOR, center['entity_id'], page=2)
    assert [row['record']['assertion_id'] for row in first['records']] == expected[:8]
    assert [row['record']['assertion_id'] for row in second['records']] == expected[8:]
    assert first['total_records'] == second['total_records'] == 9 and first['pages'] == second['pages'] == 2
    assert (first['shown_records'], first['records_omitted']) == (8, 1)
    assert (second['shown_records'], second['records_omitted']) == (1, 8)
    assert not any('_history' in sql for sql in traced)
    assert sum(sql.startswith('WITH ranked AS') for sql in traced) == 2
    assert graph.neighborhood(matter.matter_id, ACTOR, center['entity_id'], page=3)['records'] == []


def test_role_and_per_stance_limits_preserve_focus_and_exact_omission_counts(graph_case):
    _, matter, entities, assertions, center = graph_case
    record = assertions.create(matter.matter_id, ACTOR, **values(center))
    for number in range(21):
        neighbor = entities.create(matter.matter_id, ACTOR, display_name=f'Synthetic neighbor {number:02d}')
        record = assertions.add_role(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=record['revision'],
            entity_id=neighbor['entity_id'], entity_revision=1, role='participant')
    for number in range(11):
        record = assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=record['revision'],
            support='a' * 40, stance='supporting', attributed_to=f'Synthetic supporting account {number:02d}')
    for number in range(12):
        record = assertions.attach(matter.matter_id, ACTOR, record['assertion_id'], expected_revision=record['revision'],
            support='b' * 40, stance='competing', attributed_to=f'Synthetic competing account {number:02d}')
    row = EvidenceGraphService(assertions).neighborhood(matter.matter_id, ACTOR, center['entity_id'])['records'][0]
    assert row['role_total'] == 22 and len(row['roles']) == 20 and row['roles_omitted'] == 2
    assert row['roles'][0]['entity_id'] == center['entity_id']
    assert row['account_total'] == 24 and len(row['accounts']) == 20 and row['accounts_omitted'] == 4
    assert row['account_totals'] == {'supporting': 12, 'competing': 12}
    assert row['account_omissions'] == {'supporting': 2, 'competing': 2}
    assert all(sum(account['stance'] == stance for account in row['accounts']) == 10
               for stance in ('supporting', 'competing'))


def test_matter_and_revoked_authority_fail_before_source_validation(graph_case):
    store, matter, entities, assertions, center = graph_case
    assertions.create(matter.matter_id, ACTOR, **values(center))
    other = store.create_matter('Synthetic separate graph', '', ACTOR)
    member = 'synthetic-graph-member'
    store.upsert_principal('test', member, 'Synthetic graph member', member, preferred_principal_id=member)
    store.add_member(matter.matter_id, member, ACTOR)
    graph = EvidenceGraphService(assertions)
    assert graph.neighborhood(matter.matter_id, member, center['entity_id'])['total_records'] == 1
    store.revoke_member(matter.matter_id, member, ACTOR)
    calls = []
    entities.validate_references = lambda refs: calls.append(refs)
    for scope, actor in ((other.matter_id, ACTOR), (matter.matter_id, member)):
        with pytest.raises(KeyError):
            graph.neighborhood(scope, actor, center['entity_id'])
    assert not calls


@pytest.mark.parametrize('page', [0, -1, True, 1.5, '1', 100001])
def test_invalid_pages_refuse_without_loading_graph(graph_case, page):
    _, matter, _, assertions, center = graph_case
    with pytest.raises(WorkspaceProblem, match='page'):
        EvidenceGraphService(assertions).neighborhood(matter.matter_id, ACTOR, center['entity_id'], page=page)


def test_oversized_saved_support_refuses_instead_of_silently_truncating(graph_case):
    store, matter, _, assertions, center = graph_case
    record = assertions.create(matter.matter_id, ACTOR, **values(center))
    with assertions.repository.transaction(matter.matter_id, ACTOR):
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_assertion_account SET excerpt=? WHERE assertion_id=?',
                                     ('Synthetic oversized excerpt. ' * 1000, record['assertion_id']))
    with pytest.raises(WorkspaceProblem, match='excerpt limit'):
        EvidenceGraphService(assertions).neighborhood(matter.matter_id, ACTOR, center['entity_id'])
