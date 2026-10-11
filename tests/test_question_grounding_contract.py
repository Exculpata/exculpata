"""Structural drafting checks on synthetic examples separate from the frozen gate."""
import json
import pytest
from case_intelligence import generation
from case_intelligence.generation import EvidenceItem, OllamaGenerator, GenerationRejected
from case_intelligence.question_drafting import verify_question_draft
from case_intelligence.question_generation import QuestionGenerationProfile, question_profile_from_environment
from case_intelligence.question_grounding import QuestionDraftReceipt, audit_attempts

EVIDENCE = (EvidenceItem('S1', 'Synthetic note.txt', 'Line 1',
    'Morgan Sample carried a silver pouch to the station. The pouch belonged to Casey Example. '
    'The note did not establish that Morgan Sample received the voucher.'),)

def candidate(text, ids=('S1',)):
    return {'text': text, 'evidence_ids': list(ids)}

def audit(*texts, evidence=EVIDENCE):
    return audit_attempts(({'questions': [candidate(t) for t in texts]},), evidence)

@pytest.mark.parametrize('text,reason', [
    ('What was in the voucher Morgan Sample had?', 'unsupported_possession_or_action'),
    ('What was in the voucher that Morgan Sample had?', 'unsupported_possession_or_action'),
    ("What was in Morgan Sample's pouch?", 'unsupported_possession'),
    ('What did the owner say about the silver pouch?', 'unestablished_role'),
    ('What did Jordan Example say about the pouch?', 'unstated_identity_or_named_detail'),
    ('What did they say about the pouch?', 'unstated_personal_pronoun'),
    ('What did you observe Morgan Sample carrying?', 'unestablished_interviewee_participation'),
    ('Please provide records showing that Morgan Sample received the voucher.', 'uncertainty_changed_to_assertion'),
    ('Please provide records proving that Morgan Sample did not receive the voucher.', 'uncertainty_changed_to_assertion'),
])
def test_detectable_unstated_assumptions_have_explicit_drop_reasons(text, reason):
    result = audit(text)
    assert not result.questions
    assert result.drops == [{'attempt': 1, 'candidate_id': 'a1q1', 'raw': candidate(text), 'reason': reason}]

@pytest.mark.parametrize('text', [
    'What was in the pouch Morgan Sample carried?',
    "What was in Casey Example's pouch?",
    'What records, if any, clarify whether Morgan Sample received the voucher?',
    'What did the note not establish about Morgan Sample receiving the voucher?',
])
def test_supported_action_and_preserved_uncertainty_are_not_dropped(text):
    result = audit(text)
    assert len(result.questions) == 1 and result.drops == []

def test_duplicates_across_citations_are_dropped_but_substitutions_survive():
    evidence = (*EVIDENCE, EvidenceItem('S2', 'Synthetic copy.txt', 'Line 1', EVIDENCE[0].excerpt))
    text = 'What was in the pouch Morgan Sample carried?'
    rows = [candidate(text), candidate(text, ('S2',)), candidate('Where did Morgan Sample carry the pouch?')]
    result = audit_attempts(({'questions': rows},), evidence)
    assert len(result.questions) == 2 and result.drops[0]['reason'] == 'near_duplicate'

@pytest.mark.parametrize('cap', [0, 1, 2])
def test_profile_cap_bounds_calls_and_preserves_all_raw_failures(monkeypatch, cap):
    calls = []
    bad = candidate('What did the note call "fabricated quotation"?')
    def send(url, payload, **kwargs):
        calls.append(payload)
        return {'message': {'content': json.dumps({'questions': [bad] * 8})}}
    monkeypatch.setattr(generation, '_bounded_json_request', send)
    client = OllamaGenerator('http://127.0.0.1:11434', 'synthetic',
                            question_profile=QuestionGenerationProfile(top_up_calls=cap))
    raw = client.draft_questions(purpose='witness', topic='Pouch', evidence=EVIDENCE)
    assert isinstance(raw, QuestionDraftReceipt)
    assert len(calls) == cap + 1 and raw['questions'] == [bad] * (8 * (cap + 1))
    assert len(raw['drops']) == 8 * (cap + 1)
    assert verify_question_draft(raw, EVIDENCE)[0] == ()
    with pytest.raises(GenerationRejected):
        verify_question_draft(dict(raw), EVIDENCE)  # Model JSON cannot forge an internal receipt.
    with pytest.raises(GenerationRejected):
        verify_question_draft({'questions': [bad] * 9}, EVIDENCE)

@pytest.mark.parametrize('value', [-1, 3, True])
def test_invalid_top_up_cap_is_rejected(value):
    with pytest.raises(ValueError):
        QuestionGenerationProfile(top_up_calls=value)

def test_profile_settings_are_explicit_and_independent(monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_GENERATOR_QUESTION_PROFILE', 'quality')
    monkeypatch.setenv('CASE_INTELLIGENCE_GENERATOR_QUESTION_QUALITY_TOP_UP_CALLS', '1')
    monkeypatch.setenv('CASE_INTELLIGENCE_GENERATOR_QUESTION_QUALITY_JUDGE_GROUNDING', 'true')
    monkeypatch.setenv('CASE_INTELLIGENCE_GENERATOR_QUESTION_QUALITY_JUDGE_TOKENS', '1800')
    profile = question_profile_from_environment()
    assert profile.top_up_calls == 1 and profile.judge_grounding and profile.judge_tokens == 1800

@pytest.mark.parametrize('verdict,reason', [
    ('unsupported_other', 'model_judge:unsupported_other'),
    ('invalid', 'judge_invalid_decisions'),
    ('unavailable', 'judge_unavailable'),
])
def test_optional_judge_only_vetoes_and_failures_are_visible(monkeypatch, verdict, reason):
    valid = candidate('What was in the pouch Morgan Sample carried?')
    invalid = candidate("What was in Morgan Sample's pouch?")
    calls = []
    def send(url, payload, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            content = {'questions': [valid, invalid]}
        else:
            data = json.loads(payload['messages'][-1]['content'])
            assert [row['candidate_id'] for row in data['candidates']] == ['a1q1']
            if verdict == 'unavailable':
                raise generation.GenerationUnavailable('Synthetic service unavailable.')
            content = {'decisions': [{'candidate_id': 'a1q1', 'reason': verdict}]} if verdict != 'invalid' else {'decisions': []}
        return {'message': {'content': json.dumps(content)}}
    monkeypatch.setattr(generation, '_bounded_json_request', send)
    profile = QuestionGenerationProfile(top_up_calls=0, judge_grounding=True)
    raw = OllamaGenerator('http://127.0.0.1:11434', 'synthetic', question_profile=profile).draft_questions(
        purpose='witness', topic='Pouch', evidence=EVIDENCE)
    assert len(calls) == 2
    assert raw['questions'] == [valid, invalid]
    assert [row['reason'] for row in raw['drops']] == [reason, 'unsupported_possession']
    assert verify_question_draft(raw, EVIDENCE)[0] == ()

def test_parse_and_transport_failures_are_not_erased_by_later_success(monkeypatch):
    replies = [None, 'not JSON', json.dumps({'questions': []})]
    def send(url, payload, **kwargs):
        reply = replies.pop(0)
        if reply is None:
            raise generation.GenerationUnavailable('Synthetic unavailable.')
        return {'message': {'content': reply}}
    monkeypatch.setattr(generation, '_bounded_json_request', send)
    raw = OllamaGenerator('http://127.0.0.1:11434', 'synthetic').draft_questions(
        purpose='witness', topic='Pouch', evidence=EVIDENCE)
    assert len(raw['attempts']) == 3 and len(raw['calls']) == 3
    assert raw['attempts'][0]['invalid_raw'] == {'generation_error': 'generation_unavailable'}
    assert raw['attempts'][1]['invalid_raw'] == {'invalid_content': 'not JSON'}
    assert len(raw['drops']) == 2 and all(row['reason'] == 'invalid_attempt_shape' for row in raw['drops'])
