"""Public synthetic #248 replay; recorded judgments are never invented for new survivors."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from case_intelligence.generation import EvidenceItem
from case_intelligence.question_draft_evaluation import DEFAULT_CASES, load_cases
from case_intelligence.question_explanations import replay_receipt
from case_intelligence.question_grounding import audit_attempts
from case_intelligence.question_judging import judge_candidates

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT / 'tests/fixtures/question-drafts-recorded-248.json'
JUDGES = ROOT / 'tests/fixtures/question-drafts-judges-248.json'
CASES = {case['id']: case for case in load_cases()[0]['cases']}
REPLIES = {case['id']: case['attempts'] for case in json.loads(RECORDED.read_text())['cases']}
HISTORICAL = {case['id']: case for case in json.loads(JUDGES.read_text())['cases']}


def evidence(case_id='transcript-meeting'):
    return tuple(EvidenceItem(p['id'], p['source_name'], p['location'], p['excerpt'],
                              'transcript' if p['source_name'].endswith('.wav') else 'document')
                 for p in CASES[case_id]['passages'])


def test_recorded_meeting_retains_source_question_without_combining_speaker_accounts():
    result = audit_attempts(REPLIES['transcript-meeting'], evidence())
    assert result.candidate_ids == ('a1q3',), result.drops
    assert {drop['candidate_id']: drop['reason'] for drop in result.drops} == {
        'a1q1': 'missing_transcript_attribution', 'a1q2': 'missing_transcript_attribution',
        'a2q1': 'missing_transcript_attribution', 'a2q2': 'missing_transcript_attribution',
        'a2q3': 'near_duplicate'}
    assert HISTORICAL['transcript-meeting']['calls'] == []


@pytest.mark.parametrize('case_id', [key for key in REPLIES if key != 'transcript-meeting'])
def test_other_nine_cases_preserve_recorded_questions_and_actual_judge_verdicts(case_id):
    result = audit_attempts(REPLIES[case_id], evidence(case_id))
    historical = HISTORICAL[case_id]
    assert list(result.candidate_ids) == historical['retained_candidate_ids']
    assert [q.text for q in result.questions] == [q['text'] for q in historical['questions']]
    candidates = dict(zip(result.candidate_ids, result.questions))
    judged = set()
    for call in historical['calls']:
        decisions = json.loads(call['response']['content'])['decisions']
        ids = [decision['candidate_id'] for decision in decisions]
        assert all(decision['reason'] == 'grounded' for decision in decisions)
        assert not judge_candidates([(key, candidates[key]) for key in ids], evidence(case_id),
                                    request=lambda *a, **kw: call['response'], tokens=1200,
                                    stage=call['stage'])
        judged.update(ids)
    assert judged == set(result.candidate_ids)


def test_replay_only_claims_deterministic_recovery_and_preserves_other_case_scores():
    replay = replay_receipt(RECORDED, DEFAULT_CASES, explain=True)
    assert replay['deterministic_checks_passed']
    assert replay['model_gate'] == 'not_run' and replay['model_judge_executed'] is False
    for case in replay['cases']:
        if case['id'] != 'transcript-meeting':
            assert case['questions'] == HISTORICAL[case['id']]['questions']
            assert case['outcome']['status'] == HISTORICAL[case['id']]['outcome']


def test_fake_judge_veto_is_separate_from_recorded_evidence():
    """Unit-only fake response: the real receipt has NO meeting judge calls."""
    result = audit_attempts(REPLIES['transcript-meeting'], evidence())
    assert result.candidate_ids == ('a1q3',)
    def fake_request(*args, **kwargs):
        return {'content': json.dumps({'decisions': [
            {'candidate_id': 'a1q3', 'reason': 'unsupported_other'}]})}
    drops = judge_candidates(list(zip(result.candidate_ids, result.questions)), evidence(),
                             request=fake_request, tokens=1200, stage='unit-fake-judge')
    assert drops == {'a1q3': 'model_judge:unsupported_other'}
    assert not audit_attempts(REPLIES['transcript-meeting'][:1], evidence(), drops).questions


@pytest.mark.parametrize('old,new', [
    ('00:05:40', '00:05:41'),
    ('00:05:40', '00:03:12'),
    ('meeting transcript', 'deposition transcript'),
    ('meeting recording', 'deposition recording'),
    ('00:03:12 and 00:05:40', '00:03:12 and 00:05:40 and 00:08:10'),
    ('00:03:12 and 00:05:40', '"00:03:12" and 00:05:40'),
    ('?', ', which proves Speaker 1 sent the contract?'),
    ('?', ', where Speaker 2 said "the contract was destroyed"?'),
    ('?', ', after the contract was sent at 00:05:40?'),
    ('?', ', showing the contract was sent on 2026-07-19?'),
    ('?', ', which appears to say Speaker 1 saw the final contract?'),
    ('?', ', which appears to say Speaker 1 had it emailed to the old address?'),
    ('Are there any other', 'Please provide the other'),
])
def test_record_query_cannot_launder_unknown_positions_quotes_or_assertions(old, new):
    candidate = dict(REPLIES['transcript-meeting'][0]['questions'][2])
    assert old in candidate['text']
    candidate['text'] = candidate['text'].replace(old, new)
    assert not audit_attempts(({'questions': [candidate]},), evidence()).questions


@pytest.mark.parametrize('identifiers', [['S1'], ['S2'], ['S1', 'S2', 'S3']])
def test_record_query_must_reference_every_and_only_cited_transcript_position(identifiers):
    candidate = dict(REPLIES['transcript-meeting'][0]['questions'][2], evidence_ids=identifiers)
    assert not audit_attempts(({'questions': [candidate]},), evidence()).questions


@pytest.mark.parametrize('field,value', [
    ('source_name', 'Synthetic other recording.wav'),
    ('location', '00:06:00'),
    ('location', '00:03:12'),
    ('evidence_kind', 'document'),
])
def test_record_query_cannot_borrow_positions_from_another_source_or_type(field, value):
    items = list(evidence())
    items[1] = replace(items[1], **{field: value})
    candidate = REPLIES['transcript-meeting'][0]['questions'][2]
    assert not audit_attempts(({'questions': [candidate]},), items).questions


def test_record_query_generalizes_to_another_supplied_source_without_reading_speaker_words():
    items = (EvidenceItem('S1', 'Synthetic Archive review recording.flac', '02:04:06',
                          'Speaker 3: The Archive review never happened. Speaker 4: I disagree.', 'transcript'),
             EvidenceItem('S2', 'Synthetic Archive review recording.flac', '03:05:07',
                          'Speaker 3: If the other versions exist, we can check them.', 'transcript'))
    candidate = {'text': 'Are there any other versions of the Archive review transcript besides the machine '
                        'transcript of the Archive review recording at 02:04:06 and 03:05:07?',
                 'evidence_ids': ['S1', 'S2']}
    assert audit_attempts(({'questions': [candidate]},), items).questions
    candidate['text'] = candidate['text'].replace('Archive review', 'Unrelated interview')
    assert not audit_attempts(({'questions': [candidate]},), items).questions


@pytest.mark.parametrize('second_id,retained', [('recording-a', True), ('recording-b', False), ('', False)])
def test_identical_source_names_do_not_merge_distinct_or_unidentified_documents(second_id, retained):
    items = list(evidence())
    items[0] = replace(items[0], document_id='recording-a')
    items[1] = replace(items[1], document_id=second_id)
    candidate = REPLIES['transcript-meeting'][0]['questions'][2]
    assert bool(audit_attempts(({'questions': [candidate]},), items).questions) is retained


@pytest.mark.parametrize('text,reason', [
    ('What does the machine transcript at 00:03:12 say about Speaker 1 having seen the final contract?',
     'transcript_negation_lost'),
    ('What does the machine transcript at 00:05:40 say about Speaker 1 having it emailed to the old address?',
     'transcript_condition_lost'),
    ('What does the machine transcript at 00:05:40 say about Speaker 1 saying "We can check the sent folder"?',
     'wrong_transcript_speaker_quotation'),
    ('What does the machine transcript at 00:05:40 say about Speaker 9 checking the sent folder?',
     'uncited_digit_bearing_detail'),
])
def test_new_say_framing_keeps_genuine_speaker_negation_condition_and_quote_checks(text, reason):
    identifiers = ['S1'] if '00:03:12' in text else ['S2']
    result = audit_attempts(({'questions': [{'text': text, 'evidence_ids': identifiers}]},), evidence())
    assert not result.questions
    assert result.drops[0]['reason'] == reason


def test_say_framing_preserves_supported_account_and_rejects_wrong_recording_name():
    candidate = {'text': 'What does the machine transcript of the meeting recording at 00:03:12 '
                        'say about Speaker 1 never seeing the final contract?', 'evidence_ids': ['S1']}
    assert audit_attempts(({'questions': [candidate]},), evidence()).questions
    candidate['text'] = candidate['text'].replace('meeting recording', 'other recording')
    assert not audit_attempts(({'questions': [candidate]},), evidence()).questions
