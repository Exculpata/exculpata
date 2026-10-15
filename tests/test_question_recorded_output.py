"""Public synthetic #247 output replay; no model weights, network or changed benchmark."""
import json
from pathlib import Path
import runpy

import pytest

from case_intelligence.generation import EvidenceItem
from case_intelligence.question_draft_evaluation import DEFAULT_CASES, load_cases
from case_intelligence.question_explanations import explain_candidates, replay_receipt
from case_intelligence.question_grounding import QuestionDraftReceipt, audit_attempts

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT / 'tests/fixtures/question-drafts-recorded-247.json'
CASES = {case['id']: case for case in load_cases()[0]['cases']}
REPLIES = {case['id']: case['attempts'] for case in json.loads(RECORDED.read_text())['cases']}


def evidence(case_id):
    return tuple(EvidenceItem(p['id'], p['source_name'], p['location'], p['excerpt'],
                              'transcript' if p['source_name'].endswith('.wav') else 'document')
                 for p in CASES[case_id]['passages'])


@pytest.mark.parametrize('case_id', REPLIES)
def test_recorded_failure_has_useful_grounded_output_after_repair(case_id):
    result = audit_attempts(REPLIES[case_id], evidence(case_id))
    assert result.questions, [(drop['candidate_id'], drop['reason']) for drop in result.drops]


def test_recorded_transcript_negation_and_other_speaker_details_still_drop():
    meeting = audit_attempts(REPLIES['transcript-meeting'], evidence('transcript-meeting'))
    assert {d['candidate_id']: d['reason'] for d in meeting.drops} == {
        'a1q1': 'transcript_negation_lost', 'a1q3': 'cross_speaker_detail'}
    records = audit_attempts(REPLIES['transcript-records'], evidence('transcript-records'))
    assert records.candidate_ids == ('a1q3',)
    assert {d['candidate_id']: d['reason'] for d in records.drops} == {
        'a1q1': 'cross_speaker_detail', 'a1q2': 'cross_speaker_detail'}


@pytest.mark.parametrize('case_id,index,old,new', [
    ('crate-records', 0, '2026-03-05', '2026-03-06'),
    ('crate-records', 0, 'Line 4', 'Line 5'),
    ('crate-records', 1, 'Paragraph 2', 'Paragraph 3'),
    ('crate-records', 1, '90 days', '2 days'),
    ('crate-records', 0, "'seal intact'", "'seal destroyed'"),
    ('transcript-meeting', 1, '00:05:40', '00:05:41'),
    ('transcript-records', 2, 'Speaker 1', 'Speaker 2'),
    ('transcript-records', 2, 'old address', '"secret warehouse"'),
])
def test_recorded_candidate_mutations_cannot_invent_facts_or_quotes(case_id, index, old, new):
    value = dict(REPLIES[case_id][0]['questions'][index])
    assert old in value['text']
    value['text'] = value['text'].replace(old, new)
    assert not audit_attempts(({'questions': [value]},), evidence(case_id)).questions


def test_offline_replay_never_inherits_a_real_model_gate_or_judge():
    result = replay_receipt(RECORDED, DEFAULT_CASES, explain=True)
    assert result['deterministic_checks_passed'] and result['model_gate'] == 'not_run'
    assert result['model_judge_executed'] is False
    assert not {'model', 'model_pin', 'model_identity', 'runtime', 'passed'} & result.keys()
    for case in result['cases']:
        assert [r['check'] for r in case['explanation'][-2:]] == ['empty_batch'] * 2


def test_email_noun_does_not_establish_that_its_speaker_sent_it():
    items = (EvidenceItem('S1', 'Synthetic recording.wav', '00:02:30',
                          'Speaker 3: The email was archived. Speaker 4: It was sent on Tuesday.', 'transcript'),)
    candidate = {'text': 'What does the recording appear to say about Speaker 3 saying the email was sent?',
                 'evidence_ids': ['S1']}
    result = audit_attempts(({'questions': [candidate]},), items)
    assert not result.questions and result.drops[0]['reason'] == 'cross_speaker_detail'


@pytest.mark.parametrize('statement,reason,qualifier', [
    ('It never was emailed.', 'transcript_negation_lost', 'never '),
    ('If it was emailed, it might arrive.', 'transcript_condition_lost', 'possibly '),
])
def test_short_sending_paraphrase_keeps_the_email_clauses_polarity(statement, reason, qualifier):
    items = (EvidenceItem('S1', 'Synthetic recording.wav', '00:02:30',
                          f'Speaker 1: {statement} Speaker 2: The report was sent.', 'transcript'),)
    candidate = {'text': 'What does the recording appear to say about Speaker 1 sending it?', 'evidence_ids': ['S1']}
    result = audit_attempts(({'questions': [candidate]},), items)
    assert not result.questions and result.drops[0]['reason'] == reason
    # A narrow supported alternative still works; the fix does not ban sending.
    candidate['text'] = ('What does the recording appear to say about Speaker 1 never sending it?'
                         if qualifier == 'never ' else
                         'What does the recording appear to say about whether Speaker 1 was sending it?')
    assert audit_attempts(({'questions': [candidate]},), items).questions


@pytest.mark.parametrize('text', [
    'Please provide Synthetic register.txt file, specifically Paragraph 37, showing 37 cartons.',
    'Please provide Synthetic register.txt file, specifically Paragraph 37, showing "Paragraph 37".',
    'Please provide Synthetic other.txt file, specifically Paragraph 37, showing 18 cartons.',
])
def test_named_locator_never_becomes_a_count_quote_or_another_sources_location(text):
    items = (EvidenceItem('S1', 'Synthetic register.txt', 'Paragraph 37', 'The register recorded 18 cartons.'),
             EvidenceItem('S2', 'Synthetic other.txt', 'Paragraph 28', 'The register recorded 18 cartons.'))
    candidate = {'text': text, 'evidence_ids': ['S1', 'S2']}
    assert not audit_attempts(({'questions': [candidate]},), items).questions


def test_cli_explain_keeps_json_stdout_and_requires_a_real_model(monkeypatch, capsys):
    cli = runpy.run_path(str(ROOT / 'scripts/evaluate-question-drafts.py'))
    def forbidden():
        raise AssertionError('Offline replay must not read runtime configuration or instantiate a generator')
    monkeypatch.setitem(cli['main'].__globals__, 'generator_from_environment', forbidden)
    assert cli['main'](['--replay', str(RECORDED), '--explain', '--require-model']) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)['model_gate'] == 'not_run'
    assert all(case_id in output.err for case_id in REPLIES)


def test_cli_failed_replay_exits_nonzero_and_preserves_its_output(tmp_path, capsys):
    data = json.loads(RECORDED.read_text())
    for case in data['cases']:
        case['attempts'] = [{'questions': []}]
    path, output = tmp_path / 'empty.json', tmp_path / 'receipt.json'
    path.write_text(json.dumps(data))
    main = runpy.run_path(str(ROOT / 'scripts/evaluate-question-drafts.py'))['main']
    args = ['--replay', str(path), '--output', str(output), '--explain']
    assert main(args) == 1
    first = output.read_bytes()
    assert json.loads(first)['deterministic_checks_passed'] is False
    with pytest.raises(SystemExit):
        main(args)
    assert output.read_bytes() == first
    capsys.readouterr()


def test_explanations_distinguish_empty_quotes_duplicates_and_relevance():
    items = evidence('crate-records')
    good = {'text': 'Please provide the tracker report kept by the carrier for 90 days.', 'evidence_ids': ['S2']}
    bad = {'text': 'Please provide the tracker report saying "invented secret".', 'evidence_ids': ['S2']}
    irrelevant = {'text': 'What does the memo say about park permits renewed every January?', 'evidence_ids': ['S3']}
    raw = QuestionDraftReceipt(({'questions': [good, good, bad, irrelevant]}, {'questions': []}), profile={}, calls=[])
    rows = explain_candidates(raw, items, ['S1', 'S2'])
    assert [r['category'] for r in rows] == ['retained', 'duplicate', 'quote_not_found', 'relevance', 'no_candidate']
    assert rows[2]['unsupported_quotes'] == ['invented secret']


def test_speaker_quotation_diagnostic_identifies_selected_turn_and_unsupported_quote():
    candidate = {'text': 'What does the recording appear to say about Speaker 1 saying "We can check the sent folder"?',
                 'evidence_ids': ['S2']}
    rows = explain_candidates({'questions': [candidate]}, evidence('transcript-records'), ['S2'])
    assert rows[0]['reason'] == 'wrong_transcript_speaker_quotation'
    assert rows[0]['unsupported_speaker_quotes'] == ['We can check the sent folder']
    assert rows[0]['requested_speakers'] == ['speaker 1']


@pytest.mark.parametrize('change', ['unknown', 'duplicate', 'authority'])
def test_replay_rejects_unknown_cases_and_supplied_acceptance_authority(tmp_path, change):
    data = json.loads(RECORDED.read_text())
    if change == 'unknown':
        data['cases'][0]['id'] = 'invented-case'
    elif change == 'duplicate':
        data['cases'].append(data['cases'][0])
    else:
        data['cases'][0]['judge_drops'] = {}
    path = tmp_path / 'replay.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        replay_receipt(path, DEFAULT_CASES)


@pytest.mark.parametrize('topic', ['Same topic', ' Shared   synthetic\n topic ', 'x' * 601, None])
def test_replay_keeps_distinct_case_outputs_when_topics_or_entire_inputs_repeat(tmp_path, topic):
    cases, outputs = [], []
    # Include empty content, differing passages, then a repeated complete input.
    # Skipping an empty source must not shift a later recorded response.
    for index, excerpt in enumerate(('', 'The blue crate arrived.', 'The red parcel departed.', 'The blue crate arrived.')):
        key = f'case-{index}'
        cases.append(dict(id=key, purpose='witness', topic=topic, relevant=['S1'], passages=[
            dict(id='S1', source_name='Synthetic log.txt', location='Line 1', excerpt=excerpt)]))
        text = ('Why' if index == 3 else 'What') + ' does the record say about ' + excerpt
        outputs.append(dict(id=key, attempts=[{'questions': [{'text': text, 'evidence_ids': ['S1']}]}]))
    source, raw = tmp_path / 'cases.json', tmp_path / 'raw.json'
    source.write_text(json.dumps(dict(format='recordbench-question-draft-set-v1', synthetic_only=True, cases=cases)))
    raw.write_text(json.dumps(dict(format='recordbench-question-replay-v1', synthetic_only=True, cases=outputs)))
    result = replay_receipt(raw, source, explain=True)
    assert result['cases'][0]['raw'] is None
    for case, original in zip(result['cases'][1:], outputs[1:]):
        assert case['raw']['questions'] == original['attempts'][0]['questions']
