"""Synthetic receipt-derived repairs; no model inference or frozen fixture edits."""
import json

import pytest

from case_intelligence import generation
from case_intelligence.generation import EvidenceItem, OllamaGenerator, OpenAICompatibleGenerator
from case_intelligence.question_generation import QuestionGenerationProfile
from case_intelligence.question_grounding import audit_attempts


DOCUMENTS = (
    EvidenceItem('S1', 'Synthetic inventory.txt', 'Row 27',
                 'The count on 2026-06-03 was 16 cartons of Q8 filters at the East Depot.'),
    EvidenceItem('S2', 'Synthetic loading memo.txt', 'Line 4',
                 'Badge readers at the loading door record entry times. Logs are exported weekly to the office.'),
)
TRANSCRIPT = (
    EvidenceItem('S1', 'Synthetic meeting.wav', '00:06:22',
                 'Speaker 3: I never saw the signed manifest. Speaker 4: It was sent on Thursday.', 'transcript'),
    EvidenceItem('S2', 'Synthetic followup.wav', '00:08:19',
                 'Speaker 3: If it was sent, it went to the former depot. Speaker 4: We can check the outbox.', 'transcript'),
)


def row(text, ids=('S1',)):
    return {'text': text, 'evidence_ids': list(ids)}


def audit(text, evidence=DOCUMENTS, ids=('S1',)):
    return audit_attempts(({'questions': [row(text, ids)]},), evidence)


@pytest.mark.parametrize('location', ['Row 27', 'Page 27', 'Line 27'])
def test_exact_typed_document_locator_is_a_reference_not_a_fact(location):
    evidence = (EvidenceItem('S1', 'Synthetic inventory.txt', location, DOCUMENTS[0].excerpt),)
    text = f'Please provide the inventory record for {location} showing 16 cartons of Q8 filters.'
    assert len(audit(text, evidence).questions) == 1


def test_exact_transcript_locator_keeps_attribution_and_negation():
    text = 'What does the recording at 00:06:22 appear to say about Speaker 3 never seeing the signed manifest?'
    assert len(audit(text, TRANSCRIPT).questions) == 1


@pytest.mark.parametrize('text,ids', [
    ('Please provide the inventory record for Row 28 showing 16 cartons.', ('S1',)),
    ('Please provide the inventory record for Row 27 showing 27 cartons.', ('S1',)),
    ('Please provide the inventory record for Row 27 showing "Row 27".', ('S1',)),
    ('Please provide the badge logs for 2026-06-03.', ('S2',)),
    ('Please provide the badge logs for the East Depot.', ('S2',)),
    ('Please provide the badge record for Row 27 showing entry times.', ('S2',)),
    ('Please provide the record in Synthetic loading memo.txt for Row 27 showing entry times.', ('S1', 'S2')),
])
def test_fabricated_factual_or_cross_passage_document_details_still_drop(text, ids):
    assert not audit(text, ids=ids).questions


@pytest.mark.parametrize('text,ids', [
    ('What does the recording at 00:06:23 appear to say about Speaker 3 never seeing the signed manifest?', ('S1',)),
    ('What does the recording at 00:06:22 appear to say about Speaker 3 never seeing the signed manifest at 00:06:22?', ('S1',)),
    ('What does the recording at 00:06:22 appear to say about Speaker 4 never seeing the signed manifest?', ('S1',)),
    ('What does the recording at 00:06:22 appear to say about Speaker 3 seeing the signed manifest?', ('S1',)),
    ('What does the recording at 00:08:19 appear to say about Speaker 3 sending it to the former depot?', ('S2',)),
    ('What does the recording at 00:06:22 appear to say about Speaker 3 never seeing the signed manifest?', ('S2',)),
    ('What does the recording in Synthetic followup.wav at 00:06:22 appear to say about Speaker 3 never seeing the signed manifest?', ('S1', 'S2')),
])
def test_locator_exemption_never_changes_speaker_polarity_or_event_time(text, ids):
    assert not audit(text, TRANSCRIPT, ids).questions


@pytest.mark.parametrize('text', [
    'What does the recording at 00:06:22 appear to say about Speaker 3 and the former depot if it was sent?',
    'What does the recording at 00:06:22 appear to say about Speaker 4 saying "We can check the outbox"?',
])
def test_a_locator_cannot_borrow_details_from_another_cited_passage(text):
    assert not audit(text, TRANSCRIPT, ('S1', 'S2')).questions


@pytest.mark.parametrize('backend,client_type', [('ollama', OllamaGenerator), ('vllm', OpenAICompatibleGenerator)])
def test_retry_feedback_identifies_actual_defects_and_retains_empty_attempt(monkeypatch, backend, client_type):
    bad = row('Please provide the badge logs for 2026-06-03 at the East Depot.', ('S2',))
    repaired = row('Please provide the weekly exports of badge reader logs to the office.', ('S2',))
    calls = []

    def send(url, payload, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            result = {'questions': [bad]}
        else:
            feedback = json.loads(payload['messages'][-1]['content'].split('\n', 1)[1])
            assert feedback['remaining_target_questions'] == 3
            assert feedback['repair_guidance'][0]['candidate_id'] == 'a1q1'
            assert feedback['repair_guidance'][0]['unsupported_digit_tokens'] == ['2026-06-03']
            assert feedback['repair_guidance'][0]['cited_evidence_ids'] == ['S2']
            assert 'Do not transplant citations' in payload['messages'][0]['content']
            assert bad['text'] not in payload['messages'][0]['content']
            if len(calls) == 3:
                assert feedback['attempt_retention'][1]['raw_candidates'] == 0
            result = {'questions': [] if len(calls) == 2 else [repaired]}
        message = {'content': json.dumps(result)}
        return {'message': message} if backend == 'ollama' else {'choices': [{'message': message}]}

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    client = client_type('http://127.0.0.1:11434', 'synthetic', question_profile=QuestionGenerationProfile())
    raw = client.draft_questions(purpose='discovery', topic='Badge records', evidence=DOCUMENTS)
    assert len(calls) == 3 and raw['questions'] == [bad, repaired]
    assert raw['retention']['target_gap'] == 2
    assert raw['retention']['attempts'][1]['raw_candidates'] == 0
    assert raw['retained_candidate_ids'] == ['a3q1']
    assert raw['questions'][0]['evidence_ids'] == ['S2']  # No automatic citation repair.


@pytest.mark.parametrize('backend,client_type', [('ollama', OllamaGenerator), ('vllm', OpenAICompatibleGenerator)])
def test_transcript_repair_gives_labelled_turns_not_guessed_attribution(monkeypatch, backend, client_type):
    calls = []
    bad = row('What does the recording appear to say about whether the signed manifest was seen?')
    good = row('What does the recording appear to say about Speaker 3 never seeing the signed manifest?')

    def send(url, payload, **kwargs):
        calls.append(payload)
        if len(calls) == 2:
            feedback = json.loads(payload['messages'][-1]['content'].split('\n', 1)[1])
            item = feedback['cited_support'][0]
            assert item['evidence_id'] == 'S1'
            assert item['speaker_turns'][0] == {'label': 'Speaker 3', 'text': 'I never saw the signed manifest.'}
            assert feedback['repair_guidance'][0]['reason'] == 'missing_transcript_attribution'
            assert 'which speaker' in payload['messages'][0]['content']
        result = {'questions': [bad if len(calls) == 1 else good]}
        message = {'content': json.dumps(result)}
        return {'message': message} if backend == 'ollama' else {'choices': [{'message': message}]}

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    client = client_type('http://127.0.0.1:11434', 'synthetic', question_profile=QuestionGenerationProfile(top_up_calls=1))
    raw = client.draft_questions(purpose='witness', topic='Manifest', evidence=TRANSCRIPT)
    assert len(calls) == 2 and raw['questions'] == [bad, good]
    assert raw['retained_candidate_ids'] == ['a2q1']


@pytest.mark.parametrize('backend,client_type', [('ollama', OllamaGenerator), ('vllm', OpenAICompatibleGenerator)])
def test_judge_veto_cannot_resurrect_unjudged_duplicates(monkeypatch, backend, client_type):
    candidate = row('Please provide the badge reader logs for entry times.', ('S2',))
    calls = []

    def send(url, payload, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            result = {'questions': [candidate] * 3}
        else:
            data = json.loads(payload['messages'][-1]['content'])
            assert [c['candidate_id'] for c in data['candidates']] == ['a1q1']
            result = {'decisions': [{'candidate_id': 'a1q1', 'reason': 'unsupported_other'}]}
        message = {'content': json.dumps(result)}
        return {'message': message} if backend == 'ollama' else {'choices': [{'message': message}]}

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    profile = QuestionGenerationProfile(top_up_calls=0, judge_grounding=True)
    raw = client_type('http://127.0.0.1:11434', 'synthetic', question_profile=profile).draft_questions(
        purpose='discovery', topic='Badge records', evidence=DOCUMENTS)
    assert len(calls) == 2 and raw['questions'] == [candidate] * 3
    assert raw['retained_candidate_ids'] == []
    assert raw['retention']['target_gap'] == 3
