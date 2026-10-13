"""Synthetic #206 regressions; mocked transport is not model-quality evidence."""
import json

import pytest

from case_intelligence import generation
from case_intelligence.generation import EvidenceItem, OllamaGenerator, OpenAICompatibleGenerator
from case_intelligence.question_drafting import DraftedQuestion, verify_question_draft
from case_intelligence.question_generation import QuestionGenerationProfile
from case_intelligence.question_grounding import audit_attempts, near_duplicate


RECORDS = (EvidenceItem('S1', 'Archive Intake Register', 'Appendix Alpha',
    'The dispatch logs list carton arrivals. The records contain entry dates and carton counts. '
    'Morgan Sample left the depot before noon.'),)
TRANSCRIPT = (EvidenceItem('S1', 'Synthetic conversation.wav', '00:08:23',
    'Speaker 3: I never saw the signed manifest. Speaker 4: It was sent on Tuesday.', 'transcript'),
    EvidenceItem('S2', 'Synthetic conversation.wav', '00:09:02',
    'Speaker 3: If it was sent, it went to the former depot. Speaker 4: We can check the outbox.', 'transcript'))


def row(text, ids=('S1',)):
    return {'text': text, 'evidence_ids': list(ids)}


def audit(text, evidence=RECORDS, ids=('S1',)):
    return audit_attempts(({'questions': [row(text, ids)]},), evidence)


@pytest.mark.parametrize('text', [
    'Please provide the dispatch logs from Archive Intake Register.',
    'Please provide the dispatch logs listed at Appendix Alpha.',
    'What carton counts appear according to Archive Intake Register?',
    'Please provide records showing what they contain about carton arrivals.',
    'Please provide the records and their entries about carton arrivals.',
    'What records document when Morgan Sample left the depot?',
])
def test_precise_record_references_and_object_pronouns_survive(text):
    result = audit(text)
    assert [question.text for question in result.questions] == [text]
    assert result.drops == []


@pytest.mark.parametrize('text,reason', [
    ('What did Archive say about carton arrivals?', 'unstated_identity_or_named_detail'),
    ('Please provide dispatch logs from Invented Register.', 'unstated_identity_or_named_detail'),
    ('What did they say about the dispatch logs?', 'unstated_personal_pronoun'),
    ('Please provide records showing what they carried to the depot.', 'unstated_personal_pronoun'),
    ('Please provide the dispatch logs from Appendix 9.', 'uncited_digit_bearing_detail'),
    ('Please provide records saying "Archive Intake Register" about carton arrivals.', 'unsupported_quotation'),
])
def test_metadata_and_record_words_do_not_authorize_facts(text, reason):
    result = audit(text)
    assert result.questions == () and result.drops[0]['reason'] == reason


def test_exact_numeric_locator_identifies_source_but_cannot_supply_a_fact():
    evidence = (EvidenceItem('S1', 'Archive Intake Register', 'Page 9', RECORDS[0].excerpt),)
    result = audit('Please provide dispatch logs from Page 9.', evidence)
    assert len(result.questions) == 1
    result = audit('Please provide dispatch logs from Page 9 showing 9 cartons.', evidence)
    assert result.drops[0]['reason'] == 'uncited_digit_bearing_detail'


@pytest.mark.parametrize('left,right', [
    ('What records could verify whether the carton arrived?', 'What records could check whether the carton arrived?'),
    ('Please provide the recording of the body-worn camera or dash camera assigned to the depot.',
     'Please provide the body-worn camera or dash camera recording assigned to the depot.'),
])
def test_observed_paraphrase_patterns_dedupe_across_citations(left, right):
    assert near_duplicate(DraftedQuestion(left, ('S1',)), DraftedQuestion(right, ('S2',)))


@pytest.mark.parametrize('left,right', [
    ('What records could verify whether the carton arrived?', 'Where could records verify whether the carton arrived?'),
    ('What records could verify whether Morgan sent Casey the carton?', 'What records could verify whether Casey sent Morgan the carton?'),
    ('What records could verify that the carton arrived?', 'What records could verify that the carton did not arrive?'),
    ('What records could verify the arrival at 09:12?', 'What records could verify the arrival at 09:15?'),
    ('What records could verify the pouch arrival?', 'What records could verify the carton arrival?'),
    ('What did Verify Example say about the carton?', 'What did Check Example say about the carton?'),
    ('What records say "verify the carton"?', 'What records say "check the carton"?'),
    ('What does Speaker 3 appear to say in the recording?', 'What does Speaker 4 appear to say in the recording?'),
])
def test_distinct_roles_polarity_details_and_literal_words_do_not_dedupe(left, right):
    assert not near_duplicate(DraftedQuestion(left, ('S1',)), DraftedQuestion(right, ('S1',)))


@pytest.mark.parametrize('text,ids,reason', [
    ('What does the recording appear to say about whether the speakers saw the signed manifest?', ('S1',), 'missing_transcript_attribution'),
    ('What does the recording appear to say about Speaker 3 seeing the signed manifest?', ('S1',), 'transcript_negation_lost'),
    ('What does the recording appear to say about Speaker 4 seeing the signed manifest?', ('S1',), 'cross_speaker_detail'),
    ('What does the recording appear to say about Speaker 3 checking the outbox?', ('S2',), 'cross_speaker_detail'),
    ('What does the recording appear to say about Speaker 3 sending it to the former depot?', ('S2',), 'transcript_condition_lost'),
    ('What does the recording appear to say about Speaker 4 saying "I never saw the signed manifest"?', ('S1',), 'wrong_transcript_speaker_quotation'),
])
def test_transcript_attribution_and_polarity_failures_are_visible(text, ids, reason):
    result = audit(text, TRANSCRIPT, ids)
    assert not result.questions and result.drops[0]['reason'] == reason


@pytest.mark.parametrize('text,ids', [
    ('What does the recording appear to say about Speaker 3 never seeing the signed manifest?', ('S1',)),
    ('What does the recording appear to say about Speaker 4 saying it was sent on Tuesday?', ('S1',)),
    ('What does the recording appear to say about Speaker 3 and the former depot if it was sent?', ('S2',)),
    ('Please provide the outbox records, if any, to clarify what Speaker 4 appears to say in the recording.', ('S2',)),
])
def test_attributed_transcript_questions_preserve_supported_angles(text, ids):
    result = audit(text, TRANSCRIPT, ids)
    assert [q.text for q in result.questions] == [text] and not result.drops


def test_a_cited_transcript_does_not_reassign_another_documents_quotation():
    evidence = (*TRANSCRIPT, EvidenceItem('S3', 'Synthetic weather note.txt', 'Line 1',
                                        'Light rain is expected in the afternoon.'))
    text = 'Who can explain the record that says "Light rain is expected"?'
    result = audit(text, evidence, ('S1', 'S3'))
    assert [q.text for q in result.questions] == [text] and not result.drops


@pytest.mark.parametrize('backend,client_type', [('ollama', OllamaGenerator), ('vllm', OpenAICompatibleGenerator)])
def test_each_adapter_retains_batches_and_judges_against_earlier_survivors(monkeypatch, backend, client_type):
    calls = []
    first = row('Please provide dispatch logs from Archive Intake Register.')
    # A paraphrase requiring semantic judgment, beyond deterministic word aliases.
    duplicate = row('Please provide the log records of dispatch carton arrivals.')
    second = row('What carton counts do the dispatch logs list?')
    third = row('When did Morgan Sample leave the depot?')
    batches = iter([[first], [duplicate, second], [third]])

    def send(url, payload, **options):
        calls.append(payload)
        if 'Audit each question' in payload['messages'][0]['content']:
            data = json.loads(payload['messages'][-1]['content'])
            ids = [candidate['candidate_id'] for candidate in data['candidates']]
            expected = [] if ids == ['a1q1'] else [first] if ids == ['a2q1', 'a2q2'] else [first, second]
            assert data['retained_questions'] == expected
            assert data['passages'][0]['source_name'] == RECORDS[0].source_name
            reply = {'decisions': [{'candidate_id': identifier,
                'reason': 'near_duplicate' if identifier == 'a2q1' else 'grounded'} for identifier in ids]}
        else:
            reply = {'questions': next(batches)}
        message = {'content': json.dumps(reply)}
        return {'message': message} if backend == 'ollama' else {'choices': [{'message': message}]}

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    profile = QuestionGenerationProfile(judge_grounding=True)
    raw = client_type('http://127.0.0.1:11434', 'synthetic', question_profile=profile).draft_questions(
        purpose='discovery', topic='Dispatch records', evidence=RECORDS)
    assert len(calls) == 6  # Three existing batch/judge pairs, no additional calls.
    assert raw['questions'] == [first, duplicate, second, third]
    assert raw['retained_candidate_ids'] == ['a1q1', 'a2q2', 'a3q1']
    assert [d['reason'] for d in raw['drops']] == ['model_judge:near_duplicate']
    assert [q.text for q in verify_question_draft(raw, RECORDS)[0]] == [first['text'], second['text'], third['text']]
