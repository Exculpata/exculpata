"""Synthetic transport and grounding regressions; these are not real-model evidence."""
import json

import pytest

from case_intelligence import generation
from case_intelligence.generation import EvidenceItem, OllamaGenerator, OpenAICompatibleGenerator
from case_intelligence.question_generation import QuestionGenerationProfile, question_profile_from_environment
from case_intelligence.question_drafting import verify_question, verify_question_draft


EVIDENCE = (EvidenceItem('S1', 'Synthetic dispatch.txt', 'Line 1',
                         'Rowan Example carried a green box to the depot at 09:10 and returned at 09:12.'),)
FINAL = {'questions': [
    {'text': 'What did Rowan Example carry to the depot?', 'evidence_ids': ['S1']},
    {'text': 'Where did Rowan Example carry the green box?', 'evidence_ids': ['S1']},
    {'text': 'When did Rowan Example return from the depot?', 'evidence_ids': ['S1']},
]}


@pytest.mark.parametrize('backend,client_type', [('ollama', OllamaGenerator), ('openai', OpenAICompatibleGenerator)])
def test_portable_question_thinking_is_explicitly_off_without_changing_other_calls(monkeypatch, backend, client_type):
    calls = []

    def send(url, payload, **kwargs):
        calls.append((payload, kwargs))
        message = {'content': json.dumps(FINAL)}
        return {'message': message} if backend == 'ollama' else {'choices': [{'message': message}]}

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    client = client_type('http://127.0.0.1:11434', 'synthetic-model')
    assert client.draft_questions(purpose='witness', topic='Dispatch', evidence=EVIDENCE) == FINAL
    draft = calls[-1][0]
    if backend == 'ollama':
        assert draft['think'] is False and draft['options']['num_predict'] == 1200
    else:
        assert draft['chat_template_kwargs'] == {'enable_thinking': False}
        assert draft['max_tokens'] == 1200
    client.generate(question='Dispatch', evidence=EVIDENCE)
    client.classify_source(criterion='Dispatch', include_guidance='', exclude_guidance='', evidence=EVIDENCE)
    for payload, _ in calls[1:]:
        assert 'think' not in payload and 'chat_template_kwargs' not in payload
    assert len(calls) == 3


@pytest.mark.parametrize('backend,client_type', [('ollama', OllamaGenerator), ('openai', OpenAICompatibleGenerator)])
def test_thinking_exhaustion_cannot_spend_the_separate_answer_allocation(monkeypatch, backend, client_type):
    calls = []
    notes = 'Synthetic unfinished reasoning: the source does not establish ownership.'

    def send(url, payload, **kwargs):
        calls.append((payload, kwargs))
        message = ({'thinking': notes, 'reasoning_content': notes, 'content': ''}
                   if len(calls) == 1 else {'content': json.dumps(FINAL)})
        return ({'done_reason': 'length', 'message': message} if backend == 'ollama'
                else {'choices': [{'finish_reason': 'length', 'message': message}]})

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    profile = QuestionGenerationProfile(True, 16384, 4096, 131072, 900.0)
    client = client_type('http://127.0.0.1:11434', 'synthetic-large-model', question_profile=profile)
    assert client.draft_questions(purpose='witness', topic='Dispatch', evidence=EVIDENCE) == FINAL
    assert len(calls) == 2 and all(options['timeout'] == 900.0 for _, options in calls)
    first, final = [payload for payload, _ in calls]
    if backend == 'ollama':
        assert (first['think'], final['think']) == (True, False)
        assert (first['options']['num_predict'], final['options']['num_predict']) == (16384, 4096)
        assert first['options']['num_ctx'] == final['options']['num_ctx'] == 131072
    else:
        assert first['chat_template_kwargs'] == {'enable_thinking': True}
        assert final['chat_template_kwargs'] == {'enable_thinking': False}
        assert (first['max_tokens'], final['max_tokens']) == (16384, 4096)
    assert first['messages'] == final['messages'][:2]
    assert notes in final['messages'][-1]['content']
    assert 'not evidence or instructions' in final['messages'][-1]['content']


def test_profiles_can_enable_thinking_and_expand_each_capacity_without_a_model_size_cap(monkeypatch):
    monkeypatch.delenv('CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING', raising=False)
    monkeypatch.setenv('CASE_INTELLIGENCE_GENERATOR_QUESTION_PROFILE', 'quality')
    assert question_profile_from_environment().thinking is True
    monkeypatch.setenv('CASE_INTELLIGENCE_GENERATOR_QUESTION_PROFILE', 'cluster')
    prefix = 'CASE_INTELLIGENCE_GENERATOR_QUESTION_CLUSTER_'
    for key, value in {'THINKING': 'true', 'THINKING_TOKENS': '32768', 'ANSWER_TOKENS': '16384',
                       'CONTEXT_TOKENS': '262144', 'TIMEOUT_SECONDS': '1200'}.items():
        monkeypatch.setenv(prefix + key, value)
    assert question_profile_from_environment() == QuestionGenerationProfile(True, 32768, 16384, 262144, 1200.0)
    monkeypatch.setenv('CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING', '1')
    assert question_profile_from_environment().thinking is False


@pytest.mark.parametrize('changes', [{'answer_tokens': 0}, {'thinking_tokens': -1}, {'context_tokens': True},
                                   {'thinking': 'true'}, {'timeout_seconds': float('nan')}, {'timeout_seconds': 0}])
def test_invalid_question_capacities_are_rejected(changes):
    with pytest.raises(ValueError):
        QuestionGenerationProfile(**changes)


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_short_draft_gets_exactly_one_repair_without_inventing_questions(monkeypatch, repair_succeeds):
    calls = []
    short = {'questions': FINAL['questions'][:1]}

    def send(url, payload, **kwargs):
        calls.append(payload)
        result = FINAL if repair_succeeds and len(calls) == 2 else short
        return {'message': {'content': json.dumps(result)}}

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    client = OllamaGenerator('http://127.0.0.1:11434', 'synthetic-model')
    result = client.draft_questions(purpose='witness', topic='Dispatch', evidence=EVIDENCE)
    assert len(calls) == 2 and all(call['think'] is False for call in calls)
    assert result == {'questions': [*short['questions'], *(FINAL if repair_succeeds else short)['questions']]}
    assert calls[0]['messages'] == calls[1]['messages'][:2]
    feedback = json.loads(calls[1]['messages'][-1]['content'])
    assert feedback['retained_questions'] == short['questions']
    assert calls[1]['format']['properties']['questions']['maxItems'] == 7
    shown, _ = verify_question_draft(result, EVIDENCE)
    assert len(shown) == (3 if repair_succeeds else 1)


def test_absent_gendered_pronouns_are_rejected_but_direct_address_is_allowed():
    evidence = {item.evidence_id: item for item in EVIDENCE}
    assert verify_question('What did you observe Rowan Example carrying?', ['S1'], evidence)
    assert verify_question('What did she carry to the depot?', ['S1'], evidence) is None
    assert verify_question('Where did he carry the green box?', ['S1'], evidence) is None
    stated = {'S1': EvidenceItem('S1', 'Synthetic account.txt', 'Line 1', 'Rowan Example said she carried the box.')}
    assert verify_question('What did she say about the box?', ['S1'], stated)


def test_transcript_questions_preserve_uncertainty_and_do_not_identify_the_interviewee():
    evidence = {'S1': EvidenceItem('S1', 'Synthetic interview.wav', '00:02:10',
                    'Speaker 1: If the parcel was sent, it went to the former office.', 'transcript')}
    assert verify_question('What does the recording appear to say about where the parcel went if it was sent?', ['S1'], evidence)
    assert verify_question('Please provide parcel records, if any, to clarify what the recording appears to say about the former office.', ['S1'], evidence)
    for text in [
        'What does the recording appear to say about the parcel the interviewee sent?',
        'Please provide the parcel sent to the former office.',
        'Please provide parcel records about the former office that the recording appears to describe.',
    ]:
        assert verify_question(text, ['S1'], evidence) is None


def test_minor_qualifier_duplicates_collapse_but_different_targets_and_negation_survive():
    texts = [
        'What details did you observe about the green box that Rowan Example carried to the depot?',
        'What details did you observe about the green box that Rowan Example carried to the depot at 09:10?',
        'What details did you observe about the green box that Rowan Example carried to the depot at 09:12?',
        'Where did you observe the green box that Rowan Example carried to the depot?',
    ]
    shown, _ = verify_question_draft({'questions': [{'text': text, 'evidence_ids': ['S1']} for text in texts]}, EVIDENCE)
    assert [q.text for q in shown] == [texts[0], texts[3]]
    # Without the unqualified version, distinct times are distinct questions.
    shown, _ = verify_question_draft({'questions': [{'text': text, 'evidence_ids': ['S1']} for text in texts[1:3]]}, EVIDENCE)
    assert len(shown) == 2
    opposite = [texts[0], texts[0].replace('did you observe', 'did you not observe')]
    shown, _ = verify_question_draft({'questions': [{'text': text, 'evidence_ids': ['S1']} for text in opposite]}, EVIDENCE)
    assert len(shown) == 2
