"""Synthetic recall, abstention, exact citation and source-window checks."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import pytest

from case_intelligence.review_bench_v2 import Candidate
from case_intelligence.statement_comparison import attribute_statements, compare, validate_candidates

ROOT = Path(__file__).resolve().parents[1]
PAIRS = json.loads((ROOT / 'tests/fixtures/synthetic/statements/pairs.json').read_text())


def unit(text, source):
    return Candidate('synthetic', source, 'chunk-1', source + '.txt', 1, text,
                     excerpt_digest=hashlib.sha256(text.encode()).hexdigest())


def comparisons(pair):
    return compare(attribute_statements([unit(pair['left'], pair['id']+'-left'), unit(pair['right'], pair['id']+'-right')], []))


def test_seeded_recall_and_consistent_false_positives():
    expected = [p for p in PAIRS if p['type']]
    hits = sum(any(c.difference_type == p['type'] for c in comparisons(p)) for p in expected)
    false_positives = sum(bool(comparisons(p)) for p in PAIRS if not p['type'])
    print(f'Seeded recall: {hits}/{len(expected)}; consistent false positives: {false_positives}')
    assert hits / len(expected) >= .8
    assert false_positives <= 1


def test_citation_validation_drops_missing_changed_and_outside_spans():
    pair = PAIRS[0]
    units = [unit(pair[s], pair['id']+'-'+s) for s in ('left', 'right')]
    candidates = compare(attribute_statements(units, []))
    def resolve(citation):
        u = next(u for u in units if u.document_id == citation['document_id'])
        return dict(citation, excerpt=u.text, support_token='synthetic-support')
    accepted, dropped = validate_candidates(candidates, resolve)
    assert accepted and dropped == 0
    for changed in [dict(candidates[0].right, highlight='outside excerpt'), {},
                    dict(candidates[0].right, window=dict(start=0, end=99999, original_chars=1, omitted_chars=0))]:
        accepted, dropped = validate_candidates([replace(candidates[0], right=changed)], resolve)
        assert not accepted and dropped == 1
    accepted, dropped = validate_candidates(candidates, lambda c: dict(resolve(c), source_version_id='changed'))
    assert not accepted and dropped == len(candidates)


def test_attribution_abstains_and_cross_witness_is_explicit():
    a, b = PAIRS[0]['left'], PAIRS[0]['right']
    assert not compare(attribute_statements([unit(a.replace('Alex Example said ', ''), 'a'), unit(b.replace('Alex Example said ', ''), 'b')], []))
    result = compare(attribute_statements([unit(a, 'a'), unit(b.replace('Alex Example', 'Riley Demo'), 'b')], []))
    assert result and all(c.scope == 'cross_witness' for c in result)
    ambiguous = [dict(entity_id=str(i), display_name='Alex Example') for i in range(2)]
    assert not compare(attribute_statements([unit(a, 'a'), unit(b, 'b')], ambiguous))


def test_order_and_source_versions_have_stable_signatures():
    units = [unit(PAIRS[0][s], s) for s in ('left', 'right')]
    first = compare(attribute_statements(units, []))
    assert first == compare(attribute_statements(list(reversed(units)), []))
    assert first[0].signature != compare(attribute_statements([replace(units[0], source_version_id='v2'), units[1]], []))[0].signature


def test_harbor_vela_golden():
    paths = [ROOT / 'demo_data/harbor-street/interviews' / name for name in
             ('09-vela-first-account.txt', '13-vela-follow-up.txt')]
    results = compare(attribute_statements([unit(p.read_text(), p.stem) for p in paths], []))
    assert results and any(c.scope == 'same_witness' for c in results)
    assert all(c.difference_type == 'object_description' for c in results)
    assert all(c.note.startswith('Object identification needs review:') for c in results)
    from case_intelligence.review_wording import prohibited_phrases
    assert all(not prohibited_phrases(c.note) for c in results)


def test_eval_loader_expected_pairs():
    payload = json.loads((ROOT / 'tests/fixtures/synthetic/workflow_eval/inconsistencies.json').read_text())
    assert 5 <= len(payload['items']) <= 8
    for item in payload['items']:
        pair = next(p for p in PAIRS if p['id'] == item['id'])
        rows = comparisons(pair)
        actual = {tuple(sorted([c.left['document_id'], c.right['document_id']])) for c in rows}
        assert actual == {tuple(sorted(p)) for p in item['expected_pairs']}


def test_document_subject_continues_across_units_without_name_guessing():
    header = unit('Interview record\nPerson: Alex Example\nRecord date: 2034-04-19', 'interview')
    body = replace(unit('The red parcel arrived at Cedar Depot at 14:10.', 'interview'), chunk_id='chunk-2')
    statements = attribute_statements([header, body], [dict(entity_id='synthetic-person', display_name='Alex Example')])
    assert any(s.text == body.text and s.speaker_entity_id == 'synthetic-person' and s.attribution == 'document' for s in statements)


def test_shared_boilerplate_does_not_align_unrelated_object_descriptions():
    footer = '\n\nAll names, locations, dates and identifiers in this record were invented.'
    a = 'Interview record\nPerson: Alex Example\n\nA red car remained in storage.' + footer
    b = 'Interview record\nPerson: Alex Example\n\nA blue car crossed the meadow.' + footer
    assert not compare(attribute_statements([unit(a, 'interview-a'), unit(b, 'interview-b')], []))


def test_explicit_presence_and_absence_compare_the_same_person():
    pair = dict(id='explicit-presence', left='Alex Example said Jordan Sample was present at Cedar Depot during delivery.',
                right='Alex Example said Jordan Sample was absent at Cedar Depot during delivery.')
    assert any(c.difference_type == 'people_present' for c in comparisons(pair))


def test_extraction_context_change_cannot_reuse_a_review_signature():
    left = unit(PAIRS[0]['left'] + '\n\nThe label was readable.', 'left')
    changed = unit(PAIRS[0]['left'] + '\n\nThe label was partly obscured.', 'left')
    right = unit(PAIRS[0]['right'], 'right')
    assert left.source_version_id == changed.source_version_id
    before = compare(attribute_statements([left, right], []))
    after = compare(attribute_statements([changed, right], []))
    assert before and after
    assert {c.signature for c in before}.isdisjoint(c.signature for c in after)


def test_curated_speaker_merge_and_attribution_change_invalidate_review_signature():
    units = [unit(PAIRS[0]['left'], 'left'),
             unit(PAIRS[0]['right'].replace('Alex Example', 'Riley Demo'), 'right')]
    separate = [dict(entity_id='person-a', display_name='Alex Example'),
                dict(entity_id='person-b', display_name='Riley Demo')]
    merged = [dict(entity_id='person-a', display_name='Alex Example', aliases=['Riley Demo'])]
    before = compare(attribute_statements(units, separate))
    statements = attribute_statements(units, merged)
    after = compare(statements)
    assert before and after
    assert all(c.scope == 'cross_witness' for c in before)
    assert all(c.scope == 'same_witness' for c in after)
    assert {c.signature for c in before}.isdisjoint(c.signature for c in after)
    changed_basis = compare([replace(s, attribution='document') for s in statements])
    assert {c.signature for c in after}.isdisjoint(c.signature for c in changed_basis)
    # Exercise actual persisted review application, not just hash inequality.
    import sqlite3
    from types import SimpleNamespace
    from case_intelligence.candidate_review import CandidateReviewRepository
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript((ROOT / 'migrations/sqlite/0044_candidate_review.sql').read_text())
    repository = CandidateReviewRepository(SimpleNamespace(connection=connection, now=lambda: '2034-04-19T10:00:00Z'))
    try:
        old = repository.save('synthetic', 'synthetic-reviewer', before[0], 'dismiss', 0)
        assert old.status == 'dismissed' and old.revision == 1
        current = repository.apply('synthetic', after[0])
        assert current.status == 'open' and current.revision == 0
    finally:
        connection.close()


@pytest.mark.parametrize('left,right', [
    ('Alex Example said he could not remember the arrival time, but Riley Demo said the parcel arrived at Cedar Depot at 14:10.',
     'Alex Example said the parcel arrived at Cedar Depot at 14:40.'),
    ('Alex Example said the red parcel arrived at Cedar Depot at 14:10 on the first delivery.',
     'Alex Example said the red parcel arrived at Cedar Depot at 14:40 on the second delivery.'),
    ('Alex Example said the sealed parcel arrived at Cedar Depot.',
     'Alex Example said the intact parcel arrived at Cedar Depot.'),
    ('Alex Example said the empty parcel arrived at Cedar Depot.',
     'Alex Example said the sealed parcel arrived at Cedar Depot.'),
    ('Alex Example said the sealed parcel arrived at Cedar Depot.',
     'Alex Example said the closed parcel arrived at Cedar Depot.'),
    ('Alex Example said Jordan Sample was not absent at Cedar Depot during delivery.',
     'Alex Example said Jordan Sample was present at Cedar Depot during delivery.'),
])
def test_adversarial_compatible_or_uncertain_accounts_abstain(left, right):
    assert not comparisons(dict(id='adversarial', left=left, right=right))


@pytest.mark.parametrize('left,right', [('open', 'sealed'), ('damaged', 'intact'), ('empty', 'full')])
def test_different_values_within_same_object_property_remain_candidates(left, right):
    results = comparisons(dict(id='property',
        left=f'Alex Example said the {left} parcel arrived at Cedar Depot.',
        right=f'Alex Example said the {right} parcel arrived at Cedar Depot.'))
    assert results and all(c.difference_type == 'object_description' for c in results)
