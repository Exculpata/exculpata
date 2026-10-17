"""Synthetic category quality floors, provenance and bounded no-model operation."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pytest
from case_intelligence.review_bench_v2 import Candidate
from case_intelligence.review_candidates import (CATEGORIES, FORMAT, MAX_CANDIDATES, NOTICE,
    PRIOR_STATEMENT_DIFFERENCE_ENABLED, add_verified_reason, detect, from_comparisons, validate)
from case_intelligence.generation import GroundedGenerationService, UnavailableGenerator

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests/fixtures/synthetic/review_candidates'
PASSAGES=json.loads((FIXTURES/'passages.json').read_text())


def unit(text, source='synthetic'):
    return Candidate('synthetic-matter', source, 'chunk-1', source+'.txt', 1, text,
        source_version_id='version-1', excerpt_digest=hashlib.sha256(text.encode()).hexdigest())


def resolve_for(units):
    def resolve(c):
        u=next(u for u in units if u.document_id==c['document_id'] and u.chunk_id==c['chunk_id'])
        return dict(document_id=u.document_id,source_version_id=u.source_version_id,chunk_id=u.chunk_id,
            excerpt_digest=u.excerpt_digest,location=u.citation,source_name=u.source_name,
            unit_number=u.page_number,excerpt=u.text,support_token='synthetic-support')
    return resolve


@pytest.mark.parametrize('category',[key for key in CATEGORIES if key!='prior_statement_difference'])
def test_category_precision_recall(category):
    fixtures=[r for r in PASSAGES if r['category']==category]
    tp=fp=fn=0
    for row in fixtures:
        u=unit(row['text'],row['id'])
        candidates,dropped=validate(detect([u]),resolve_for([u]))
        assert dropped==0
        actual=any(c.category==category for c in candidates)
        tp+=bool(actual and row['expected']); fp+=bool(actual and not row['expected']); fn+=bool(not actual and row['expected'])
        assert all(c.citation['highlight'] in c.citation['excerpt'] for c in candidates)
    precision=tp/max(1,tp+fp); recall=tp/max(1,tp+fn)
    print(f'{category}: precision={precision:.3f}, recall={recall:.3f}, TP={tp}, FP={fp}, FN={fn}')
    assert precision>=1.0 and recall>=1.0 and fp==0


def test_no_citation_changed_source_and_outside_trigger_dropped():
    u=unit(PASSAGES[0]['text'])
    candidate=detect([u])[0]
    resolve=resolve_for([u])
    for changed in ({},dict(candidate.citation, highlight='outside'),dict(candidate.citation, highlight_start=9999),dict(candidate.citation, source_version_id='v2'),
                    dict(candidate.citation,window=dict(start=0,end=9999,original_chars=1,omitted_chars=0))):
        rows,dropped=validate([replace(candidate,citation=changed)],resolve)
        assert rows==[] and dropped==1
    assert validate([candidate],lambda c: dict(resolve(c),excerpt='changed'))[0]==[]


def test_dedup_signature_and_order_use_source_version_and_extraction():
    u=unit(PASSAGES[0]['text'])
    first=detect([u])[0]
    assert len(detect([u,u]))==1
    assert detect([replace(u,source_version_id='changed')])[0].signature!=first.signature
    assert detect([replace(u,excerpt_digest='changed')])[0].signature!=first.signature
    assert first.reason=='Mentions another person in connection with the event.'


def test_prior_statement_feature_off_and_typed_adapter():
    assert PRIOR_STATEMENT_DIFFERENCE_ENABLED is False
    c=detect([unit(PASSAGES[0]['text'])])[0].citation
    for row in json.loads((FIXTURES/'prior_statements.json').read_text()):
        payload=dict(format='exculpata-inconsistency-candidates-v1',candidates=[dict(row,signature=row['id'],left=c,right=c)])
        assert from_comparisons(payload)==[]
        assert bool(from_comparisons(payload,enabled=True))==row['expected']
    assert from_comparisons(dict(format='untyped',candidates=[]),enabled=True)==[]


def test_unavailable_model_keeps_all_deterministic_rows():
    service=GroundedGenerationService(UnavailableGenerator())
    for r in PASSAGES:
        for candidate in detect([unit(r['text'])]):
            assert add_verified_reason(candidate,service)==candidate


class SyntheticGenerator:
    available=True
    def __init__(self,text,ids=('S1',)):
        self.text,self.ids=text,ids
    def generate(self,**kwargs):
        return dict(answerable=True,claims=[dict(text=self.text,evidence_ids=list(self.ids))],limitation=None,missing_information='')


def test_optional_reason_must_pass_verifier_and_wording_and_cite():
    u=unit('The witness said no deal was offered during the interview.')
    candidate=detect([u])[0]
    result=add_verified_reason(candidate,GroundedGenerationService(SyntheticGenerator(u.text)))
    assert result.additional_reason==u.text
    assert result.signature==candidate.signature and result.reason==candidate.reason and result.category==candidate.category
    for text,ids in [('The laboratory result describes a blue vehicle.',('S1',)),(u.text,('S9',)),('The witness lied.',('S1',))]:
        assert add_verified_reason(candidate,GroundedGenerationService(SyntheticGenerator(text,ids)))==candidate


def test_eval_hook_resolves_synthetic_sources():
    payload=json.loads((ROOT/'tests/fixtures/synthetic/workflow_eval/review_candidates.json').read_text())
    assert '#267' in payload['todo']
    fixtures={r['id']:r for r in PASSAGES}
    for item in payload['items']:
        row=fixtures[item['source_id']]
        assert {c.category for c in detect([unit(row['text'])])}==set(item['expected_categories'])


def test_harbor_street_golden_snapshot():
    paths=sorted((ROOT/'demo_data/harbor-street').rglob('*.txt'))
    units=[unit(p.read_text(),str(p.relative_to(ROOT/'demo_data/harbor-street'))) for p in paths]
    rows,dropped=validate(detect(units),resolve_for(units))
    assert dropped==0
    actual=[dict(category=c.category,source=c.citation['source_name'],excerpt=c.citation['excerpt']) for c in rows]
    assert actual==json.loads((FIXTURES/'harbor_street_golden.json').read_text())


def test_rule_change_cannot_reuse_review_state(monkeypatch):
    import case_intelligence.review_candidates as module
    u=unit(PASSAGES[0]['text'])
    first=detect([u])[0].signature
    changed=[dict(r,reason=r['reason']+' Read the original passage.') for r in module.RULES]
    monkeypatch.setattr(module,'RULES',changed)
    assert detect([u])[0].signature!=first


def test_candidate_accumulator_is_bounded_and_category_first():
    units=[unit('Another person was seen leaving the scene.\n\nThe witness said no deal was offered.',str(i)) for i in range(180)]
    rows=detect(units)
    assert len(rows)==2*(MAX_CANDIDATES+1)
    assert all(c.category=='alternate_person' for c in rows[:MAX_CANDIDATES+1])
    assert [c.source_order for c in rows[:MAX_CANDIDATES+1]]==list(range(MAX_CANDIDATES+1))


def test_model_cannot_remove_negation_or_uncertainty():
    c=detect([unit('The witness said no deal was offered during the interview.')])[0]
    assert add_verified_reason(c,GroundedGenerationService(SyntheticGenerator('deal was offered during the interview.')))==c


def test_adapter_signature_binds_the_same_person_identity():
    c=detect([unit(PASSAGES[0]['text'])])[0].citation
    row=dict(kind='possible_inconsistency',scope='same_witness',speaker_ids=['person-a'],signature='synthetic',left=c,right=c)
    def adapted(value):
        return from_comparisons(dict(format='exculpata-inconsistency-candidates-v1',candidates=[value]),enabled=True)[0]
    assert adapted(row).signature!=adapted(dict(row,speaker_ids=['person-b'])).signature


@pytest.mark.parametrize('boundary',[', but ', '; ', ' but '])
def test_negated_physical_trigger_does_not_hide_separate_mismatch(boundary):
    text='The DNA source could not be conclusively excluded'+boundary+'the fingerprint excluded the comparison source.'
    rows=[c for c in detect([unit(text)]) if c.category=='favorable_physical_evidence']
    assert len(rows)==1 and rows[0].citation['highlight']=='excluded'
    assert rows[0].citation['excerpt']==text
    assert rows[0].citation['highlight_start']==text.rindex('excluded')


def test_physical_negation_family_with_intervening_modifiers():
    auxiliaries=('was not','is not','has not been','had not been','could not be','cannot be',
        "couldn't be","can't be","wasn't","isn't","hasn't been","hadn't been",
        'never was','was never',"wasn't ever",'couldn’t be','was not yet','has not yet been',
        'could not, however, be','could not, based on the available analysis, be',
        'was neither','was unable to be','failed to be','was without being',
        "wouldn't be","shouldn't be","won't be","mustn't be","weren't","aren't",
        'isn’t',"doesn't appear to be","didn't appear to be")
    modifiers=('', 'conclusively ', 'reliably ', 'definitively ', 'independently ',
        'clearly and reliably ', 'conclusively, according to the laboratory report, ')
    predicates=('excluded','incompatible with the comparison','different from the comparison')
    for auxiliary in auxiliaries:
        for modifier in modifiers:
            for predicate in predicates:
                text=f'The DNA source {auxiliary} {modifier}{predicate}.'
                assert not any(c.category=='favorable_physical_evidence' for c in detect([unit(text)])),text


def test_physical_mismatch_family_preserves_trigger_internal_negation():
    passages=('The DNA source was conclusively excluded.',
        'The DNA source was reliably excluded.',
        'The DNA result did not match the comparison.',
        'The DNA result does not match the comparison.',
        "The DNA result didn't match the comparison.",
        'The DNA result was not consistent with the comparison.',
        'The DNA report recorded no match for the comparison.',
        'The DNA result was incompatible with the comparison.',
        'The DNA result was different from the comparison.')
    for text in passages:
        assert any(c.category=='favorable_physical_evidence' for c in detect([unit(text)])),text
