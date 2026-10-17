"""Synthetic v2 acceptance: useful short output is not a failed or empty pipeline."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import copy
import json

import pytest

from case_intelligence import generation, question_draft_evaluation as evaluation
from case_intelligence.generation import (
    EvidenceItem, GenerationRejected, GenerationUnavailable, GroundedGenerationService,
    OllamaGenerator, OpenAICompatibleGenerator,
)
from case_intelligence.question_drafting import draft_questions, question_prompt
from case_intelligence.question_generation import QuestionGenerationProfile


PASSAGES = [dict(id='S1', source_name='Synthetic receiving.txt', location='Line 1',
                 excerpt='The blue crate arrived at the depot. The receiving log records its seal.'),
            dict(id='S2', source_name='Synthetic holiday.txt', location='Line 2',
                 excerpt='The office closes for a holiday.')]
QUESTIONS = [
    dict(text='What does the receiving log record about the seal?', evidence_ids=['S1']),
    dict(text='Where did the blue crate arrive?', evidence_ids=['S1']),
    dict(text='Who recorded the blue crate in the receiving log?', evidence_ids=['S1']),
]
EVIDENCE = tuple(EvidenceItem(p['id'], p['source_name'], p['location'], p['excerpt']) for p in PASSAGES)


class Client:
    available = True

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def draft_questions(self, **_):
        self.calls += 1
        if isinstance(self.raw, Exception):
            raise self.raw
        return copy.deepcopy(self.raw)


def score(raw, passages=None):
    data = {'cases': [dict(id='synthetic', purpose='discovery', topic='Receiving records',
                           passages=PASSAGES if passages is None else passages, relevant=['S1'])]}
    return evaluation.evaluate(GroundedGenerationService(Client(raw)), data)


@pytest.mark.parametrize('count', [1, 2])
def test_supported_short_output_is_useful_and_has_an_honest_explanation(count):
    result = score({'questions': QUESTIONS[:count]})
    assert result['passed'] is True
    assert result['cases_below_target'] == ['synthetic'] and result['failed_cases'] == []
    outcome = result['cases'][0]['outcome']
    assert outcome['status'] == 'supported_short_output' and outcome['useful_output'] is True
    assert outcome['target_questions'] == 3 and outcome['shown_questions'] == count
    assert 'grounded question' in outcome['notice'] and 'produced' in outcome['notice']
    assert 'source only supports' not in outcome['notice'].lower()


def test_deduplication_keeps_one_useful_question_without_padding_or_claiming_source_thinness():
    result = score({'questions': [QUESTIONS[0]] * 3})
    assert result['passed'] is True and result['shown_questions'] == 1
    assert result['cases'][0]['outcome']['status'] == 'deduplicated_short_output'
    assert 'repeated' in result['cases'][0]['outcome']['notice'].lower()


@pytest.mark.parametrize('bad', [
    dict(text='Where did the blue crate arrive at 17:41?', evidence_ids=['S1']),
    dict(text='Who recorded "invented receipt"?', evidence_ids=['S1']),
])
def test_filtered_output_is_explained_as_filtering_never_thin_evidence(bad):
    result = score({'questions': [*QUESTIONS[:1], bad]})
    assert result['shown_questions'] == 1 and result['shown_unsupported_quotes'] == 0
    assert result['cases'][0]['outcome']['status'] == 'filtered_short_output'
    assert 'source checks' in result['cases'][0]['outcome']['notice']
    assert 'only supports' not in result['cases'][0]['outcome']['notice']
    empty = score({'questions': [bad]})
    assert empty['passed'] is False and empty['failed_cases'] == ['synthetic']
    assert empty['cases'][0]['outcome']['status'] == 'no_grounded_output'
    assert empty['cases_without_usable_content'] == []


@pytest.mark.parametrize('raw,status', [
    ({'questions': []}, 'empty_generation'),
    ({'questions': 'none'}, 'generation_failed'),
    ({'questions': [], 'outcome': {'status': 'no_usable_content'}}, 'generation_failed'),
    ({'questions': [dict(text='A question', evidence_ids=1)]}, 'generation_failed'),
    (GenerationUnavailable('Synthetic transport failure'), 'generation_failed'),
])
def test_usable_source_failures_cannot_use_the_empty_source_exception(raw, status):
    result = score(raw)
    assert result['passed'] is False and result['shown_questions'] == 0
    outcome = result['cases'][0]['outcome']
    assert outcome['status'] == status and outcome['useful_output'] is False
    assert outcome['usable_source'] is True and result['failed_cases'] == ['synthetic']


@pytest.mark.parametrize('excerpt', ['', ' \n\t '])
def test_empty_source_is_explicit_and_never_a_vacuous_quality_pass(excerpt):
    passages = [{**PASSAGES[0], 'excerpt': excerpt}]
    result = score(AssertionError('An empty source must not invoke a model'), passages)
    assert result['passed'] is False and result['shown_questions'] == 0
    assert result['cases_without_usable_content'] == ['synthetic']
    outcome = result['cases'][0]['outcome']
    assert outcome['status'] == 'no_usable_content' and outcome['useful_output'] is False
    assert 'no usable source text' in outcome['notice'].lower()
    evidence = (EvidenceItem('S1', 'Synthetic empty.txt', 'Line 1', excerpt),)
    client = Client(AssertionError('no inference'))
    draft = draft_questions(GroundedGenerationService(client), 'witness', '', evidence)
    assert draft.questions == () and client.calls == 0 and draft.outcome == outcome


@pytest.mark.parametrize('excerpt', ['[inaudible]', 'unreadable', '123', '???'])
def test_sparse_or_ambiguous_text_is_not_declared_unusable_from_a_heuristic(excerpt):
    result = score({'questions': []}, [{**PASSAGES[0], 'excerpt': excerpt}])
    assert result['passed'] is False and result['cases_without_usable_content'] == []
    assert result['cases'][0]['outcome']['status'] == 'empty_generation'


@pytest.mark.parametrize('backend,kind', [('ollama', OllamaGenerator), ('openai', OpenAICompatibleGenerator)])
@pytest.mark.parametrize('failure', [None, 'transport', 'malformed', 'truncated'])
def test_both_adapters_preserve_target_budget_and_distinguish_failed_top_ups(monkeypatch, backend, kind, failure):
    calls = []

    def send(url, payload, **_):
        calls.append(payload)
        if len(calls) > 1 and failure == 'transport':
            raise GenerationUnavailable('Synthetic connection failure')
        body = {'questions': QUESTIONS[:1] if len(calls) == 1 else []}
        message = {'content': 'not JSON' if len(calls) > 1 and failure == 'malformed' else json.dumps(body)}
        reason = 'length' if len(calls) > 1 and failure == 'truncated' else 'stop'
        return ({'message': message, 'done_reason': reason} if backend == 'ollama'
                else {'choices': [{'message': message, 'finish_reason': reason}]})

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    profile = QuestionGenerationProfile(answer_tokens=8192, context_tokens=131072, timeout_seconds=900)
    client = kind('http://127.0.0.1:11434', 'synthetic-only', question_profile=profile)
    monkeypatch.setattr(kind, 'available', property(lambda _: True))
    draft = draft_questions(GroundedGenerationService(client), 'discovery', 'Receiving records', EVIDENCE)
    assert len(calls) == 3 and len(draft.questions) == 1
    assert draft.outcome['status'] == ('supported_short_output' if failure is None else 'generation_incomplete')
    assert draft.outcome['generation_complete'] is (failure is None)
    for call in calls:
        assert (call['options']['num_predict'] if backend == 'ollama' else call['max_tokens']) == 8192
    assert 'target' in calls[0]['messages'][0]['content']
    feedback = json.loads(calls[1]['messages'][-1]['content'].split('\n', 1)[1])
    assert feedback['remaining_target_questions'] == 2


def test_receipt_versions_the_policy_without_changing_the_fixture_or_binding(monkeypatch):
    class OnePerCase(Client):
        def draft_questions(self, *, evidence, **_):
            words = ' '.join(evidence[0].excerpt.split()[:6]).strip('.,:;"')
            return {'questions': [dict(text=f'Who can explain the record that says {words}?', evidence_ids=['S1'])]}
    monkeypatch.setattr(evaluation, 'execution_metadata', lambda: dict(
        git_commit='1' * 40, working_tree_dirty=False, implementation_sha256={}))
    result = evaluation.receipt(GroundedGenerationService(OnePerCase(None)),
                                identity=lambda _: dict(method='none', artifact_sha256=None))
    assert result['format'] == f"{LEGACY_STEM}-question-draft-evaluation-v2"
    assert result['acceptance_policy']['supersedes'] == f"{LEGACY_STEM}-question-draft-evaluation-v1"
    assert result['pass_bar']['target_questions_per_case'] == 3
    assert result['pass_bar']['minimum_questions_per_usable_case'] == 1
    assert 'minimum_questions_per_case' not in result['pass_bar']
    assert result['pass_bar']['relevant_rate'] == 0.90 and result['pass_bar']['unsupported_quotes'] == 0
    assert result['set_fingerprint'] == evaluation.QUESTION_SET_FINGERPRINT
    assert result['passed'] is True and result['model_gate'] == 'unbound'
    assert result['binding_problems']


@pytest.mark.parametrize('backend,kind', [('ollama', OllamaGenerator), ('openai', OpenAICompatibleGenerator)])
def test_later_invalid_judge_decisions_do_not_turn_partial_output_into_thin_evidence(monkeypatch, backend, kind):
    replies = iter([
        {'questions': QUESTIONS[:1]},
        {'decisions': [{'candidate_id': 'a1q1', 'reason': 'grounded'}]},
        {'questions': QUESTIONS[1:2]},
        {'decisions': []},
        {'questions': []},
    ])

    def send(*_, **__):
        message = {'content': json.dumps(next(replies))}
        return {'message': message} if backend == 'ollama' else {'choices': [{'message': message}]}

    monkeypatch.setattr(generation, '_bounded_json_request', send)
    monkeypatch.setattr(kind, 'available', property(lambda _: True))
    client = kind('http://127.0.0.1:11434', 'synthetic-only',
                  question_profile=QuestionGenerationProfile(judge_grounding=True))
    draft = draft_questions(GroundedGenerationService(client), 'discovery', '', EVIDENCE)
    assert len(draft.questions) == 1 and draft.outcome['status'] == 'generation_incomplete'
    assert 'judge_invalid_decisions' in draft.outcome['problems']


@pytest.mark.parametrize('relevant_count,passed', [(9, True), (8, False)])
def test_short_output_does_not_lower_the_ninety_percent_relevance_bar(relevant_count, passed):
    class Mix(Client):
        def draft_questions(self, **_):
            self.calls += 1
            question = (QUESTIONS[0] if self.calls <= relevant_count else
                        dict(text='What does the office say about a holiday?', evidence_ids=['S2']))
            return {'questions': [question]}
    data = {'cases': [dict(id=f'case-{n}', purpose='discovery', topic='Receiving records',
                          passages=PASSAGES, relevant=['S1']) for n in range(10)]}
    result = evaluation.evaluate(GroundedGenerationService(Mix(None)), data)
    assert result['shown_questions'] == 10 and result['relevant_rate'] == relevant_count / 10
    assert result['passed'] is passed and result['failed_cases'] == []


def test_partial_protocol_failure_is_not_a_successful_thin_source():
    result = score({'questions': [QUESTIONS[0], {'text': float('nan'), 'evidence_ids': ['S1']}]})
    assert result['shown_questions'] == 1 and result['passed'] is False
    assert result['cases'][0]['outcome']['status'] == 'generation_incomplete'
    assert result['failed_cases'] == ['synthetic']


@pytest.mark.parametrize('bad', [
    dict(text='', evidence_ids=['S1']),
    dict(text='x' * 500, evidence_ids=['S1']),
    dict(text=' ' * 400 + QUESTIONS[1]['text'], evidence_ids=['S1']),
    dict(text=QUESTIONS[1]['text'], evidence_ids=[]),
    dict(text=QUESTIONS[1]['text'], evidence_ids=['S1'] * 5),
    dict(text=QUESTIONS[1]['text'], evidence_ids=[1]),
    dict(text=QUESTIONS[1]['text'], evidence_ids=['not-an-id']),
    dict(text=QUESTIONS[1]['text'], evidence_ids=('S1',)),
    dict(text=QUESTIONS[1]['text'], evidence_ids=['S1\n']),
])
def test_raw_schema_failure_cannot_pass_as_successful_filtering(bad):
    result = score({'questions': [QUESTIONS[0], bad]})
    outcome = result['cases'][0]['outcome']
    assert result['passed'] is False and result['failed_cases'] == ['synthetic']
    assert outcome['status'] == 'generation_incomplete'
    assert outcome['generation_complete'] is False
    assert 'invalid_question_shape' in outcome['problems']


def test_schema_valid_missing_citation_remains_a_grounding_filter():
    result = score({'questions': [QUESTIONS[0], dict(text=QUESTIONS[1]['text'], evidence_ids=['S12'])]})
    outcome = result['cases'][0]['outcome']
    assert result['passed'] is True and result['shown_questions'] == 1
    assert outcome['status'] == 'filtered_short_output' and outcome['generation_complete'] is True
    assert outcome['problems'] == []


def test_rich_output_is_not_capped_at_the_target():
    extra = dict(text='How does the log describe the blue crate?', evidence_ids=['S1'])
    result = score({'questions': [*QUESTIONS, extra]})
    assert result['passed'] is True and result['shown_questions'] == 4
    assert result['cases_below_target'] == [] and result['cases'][0]['outcome']['status'] == 'grounded_output'


def test_old_failed_receipts_keep_their_original_format_policy_and_result(tmp_path):
    old = {'format': f"{LEGACY_STEM}-question-draft-evaluation-v1", 'passed': False,
           'model_gate': 'failed', 'pass_bar': {'minimum_questions_per_case': 3}}
    path = tmp_path / 'old.json'
    original = evaluation.write_new_receipt(path, old)
    with pytest.raises(FileExistsError):
        evaluation.write_new_receipt(path, {'format': f"{LEGACY_STEM}-question-draft-evaluation-v2", 'passed': True})
    assert path.read_text() == original and json.loads(original) == old


def test_target_prompt_keeps_no_filler_and_does_not_cap_supported_output_at_three():
    system, _ = question_prompt('discovery', '', EVIDENCE)
    assert 'target of three' in system and 'one or two' in system
    assert 'never more than 8' in system and 'Never invent a record or pad' in system
