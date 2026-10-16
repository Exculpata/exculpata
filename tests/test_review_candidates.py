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
    for changed in ({},dict(candidate.citation, highlight='outside'),dict(candidate.citation, source_version_id='v2'),
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
