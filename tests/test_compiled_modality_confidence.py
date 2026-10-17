"""Compile historical generated coverage without changing raw saved-work receipts."""

from tests.fixture_write import fixture_write
import copy
import json

import pytest

from case_intelligence.report_compilation import CompilationProblem
from tests.test_guided_reports import finished, queue
from tests.test_modality_confidence_presentation import CASES, coverage, readable
from tests.test_report_review_basis import ACTOR, saved_research, workspace


def saved_selection(bench, matter, surface, mode, notice):
    job, document = saved_research(bench, matter)
    result = copy.deepcopy(job.result)
    result['answer']['modality_coverage'] = coverage(mode, notice)
    if surface == 'research':
        encoded = json.dumps(result)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute('UPDATE workbench_research_job SET result_json=? WHERE job_id=?',
                (encoded, job.job_id))
        return f'research:{job.job_id}', document, 'workbench_research_job', 'result_json', 'job_id', job.job_id, encoded
    conversation = bench.workspace.get_conversation(matter.matter_id)
    payload = {**result['answer'], 'kind': 'generated'}
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        'assistant', 'Synthetic saved answer.', payload)
    encoded = bench.workspace.connection.execute('SELECT payload_json FROM workbench_message WHERE message_id=?',
        (message.message_id,)).fetchone()[0]
    return f'conversation:{conversation.conversation_id}', document, 'workbench_message', 'payload_json', 'message_id', message.message_id, encoded


@pytest.mark.parametrize('surface', ['conversation', 'research'])
@pytest.mark.parametrize('mode,historical,expected', CASES)
def test_compiled_report_projects_generated_coverage_and_preserves_saved_work(workspace, surface, mode, historical, expected):
    client, bench, matter = workspace
    selected, document, table, column, key, identifier, encoded = saved_selection(bench, matter, surface, mode, historical)
    source_bytes = bench.source_store(matter).source_path(document.document_id).read_bytes()
    units = document.parsed_units()
    job_id = queue(client, matter, [selected])
    state = finished(client, matter, job_id)
    assert state['state'] == 'succeeded', state
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    sections = bench.workspace.report_sections(matter.matter_id, report.report_id)
    media = [section for section in sections if 'Saved media coverage' in section.heading]
    assert len(media) == 1
    assert expected in media[0].body
    if historical != expected:
        assert historical not in media[0].body  # Includes the displayed review-basis JSON.
    page = client.get(state['result_url'])
    assert page.status_code == 200 and expected in page.text
    for fmt in ('markdown', 'docx'):
        response = client.get(f'/matters/{matter.slug}/reports/{report.report_id}/export', params={'format': fmt})
        assert response.status_code == 200
        text = readable(response.content, fmt)
        assert expected in text
        if historical != expected:
            assert historical not in text
    raw = bench.workspace.connection.execute(f'SELECT {column} FROM {table} WHERE {key}=?', (identifier,)).fetchone()[0]
    assert raw == encoded
    assert document.parsed_units() == units
    assert bench.source_store(matter).source_path(document.document_id).read_bytes() == source_bytes


@pytest.mark.parametrize('surface', ['conversation', 'research'])
def test_normalization_equivalent_raw_coverage_change_still_blocks_compilation_finish(workspace, surface):
    _client, bench, matter = workspace
    historical, expected = CASES[0][1:]
    selected, _, table, column, key, identifier, encoded = saved_selection(bench, matter, surface, 'complete', historical)
    flow = bench.report_compilation
    flow.coordinator.close()
    flow.jobs.queue(matter.matter_id, ACTOR, 'timeline', '', (selected,), 'synthetic-coverage-fingerprint')
    claimed = flow.jobs.claim('synthetic-coverage-worker')
    draft = flow.process(claimed, lambda: False)
    updated = json.loads(encoded)
    payload = updated['answer'] if surface == 'research' else updated
    payload['modality_coverage']['notice'] = expected
    # Simulate changed saved work without changing any revision/locator/metric.
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute(f'UPDATE {table} SET {column}=? WHERE {key}=?',
            (json.dumps(updated), identifier))
    with pytest.raises(CompilationProblem, match='selected work changed'):
        flow.finish(claimed, draft)
    assert not bench.workspace.reports(matter.matter_id, ACTOR)


@pytest.mark.parametrize('kind', ['generated_fallback', 'rejected', 'omission', 'custom', 'human', 'claim'])
def test_compilation_projects_only_typed_generated_boilerplate(workspace, kind):
    from case_intelligence.answer_presentation import GENERATED_ANSWER_INTRODUCTION, REJECTED_ANSWER_NOTICE
    from case_intelligence.generation import LEGACY_VERIFICATION_OMISSION_NOTICE, VERIFICATION_OMISSION_NOTICE
    from tests.test_rejected_generation_presentation import LEGACY

    client, bench, matter = workspace
    old_intro = 'The searchable sources support this answer:'
    historical = LEGACY if kind == 'rejected' else LEGACY_VERIFICATION_OMISSION_NOTICE if kind == 'omission' else old_intro
    expected = {'generated_fallback': GENERATED_ANSWER_INTRODUCTION, 'rejected': REJECTED_ANSWER_NOTICE,
                'omission': VERIFICATION_OMISSION_NOTICE}.get(kind, historical)
    payload = {'kind': 'generated', 'introduction': old_intro, 'claims': []}
    if kind == 'rejected':
        payload = {'kind': 'not-supported', 'answerable': False, 'claims': [], 'missing_information': historical}
    elif kind == 'omission':
        payload = {'kind': 'generated', 'claims': [{'text': 'Synthetic retained finding.', 'citations': []}],
                   'omitted_claims': 1, 'verification_notice': historical, 'source_limitation': None,
                   'limitation': {'text': historical, 'citations': []}}
    elif kind == 'custom':
        payload['kind'] = 'manual'
    elif kind == 'claim':
        payload = {'kind': 'generated', 'claims': [{'text': historical, 'citations': []}]}
    if kind in {'omission', 'claim'}:
        research, _ = saved_research(bench, matter)
        payload['claims'][0]['citations'] = research.result['answer']['claims'][0]['citations']
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        'user' if kind == 'human' else 'assistant', historical, payload)
    raw = bench.workspace.connection.execute('SELECT payload_json FROM workbench_message WHERE message_id=?',
        (message.message_id,)).fetchone()[0]
    job_id = queue(client, matter, [f'conversation:{conversation.conversation_id}'])
    state = finished(client, matter, job_id)
    assert state['state'] == 'succeeded', state
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    body = '\n'.join(s.body for s in bench.workspace.report_sections(matter.matter_id, report.report_id))
    assert expected in body
    if expected != historical:
        assert historical not in body
    assert 'source_snapshot_digest' not in body
    assert bench.workspace.connection.execute('SELECT payload_json FROM workbench_message WHERE message_id=?',
        (message.message_id,)).fetchone()[0] == raw
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == message


@pytest.mark.parametrize('field', ['introduction', 'missing_information', 'verification_notice'])
def test_other_projected_boilerplate_keeps_original_snapshot_binding(workspace, field):
    from case_intelligence.answer_presentation import GENERATED_ANSWER_INTRODUCTION, REJECTED_ANSWER_NOTICE
    from case_intelligence.generation import LEGACY_VERIFICATION_OMISSION_NOTICE, VERIFICATION_OMISSION_NOTICE
    from tests.test_rejected_generation_presentation import LEGACY

    _client, bench, matter = workspace
    pairs = {'introduction': ('The searchable sources support this answer:', GENERATED_ANSWER_INTRODUCTION),
             'missing_information': (LEGACY, REJECTED_ANSWER_NOTICE),
             'verification_notice': (LEGACY_VERIFICATION_OMISSION_NOTICE, VERIFICATION_OMISSION_NOTICE)}
    old, new = pairs[field]
    payload = {'kind': 'not-supported' if field == 'missing_information' else 'generated', 'claims': [], field: old}
    if field == 'verification_notice':
        research, _ = saved_research(bench, matter)
        payload['claims'] = copy.deepcopy(research.result['answer']['claims'])
    conversation = bench.workspace.get_conversation(matter.matter_id)
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id, 'assistant', old, payload)
    flow = bench.report_compilation
    flow.coordinator.close()
    flow.jobs.queue(matter.matter_id, ACTOR, 'timeline', '', (f'conversation:{conversation.conversation_id}',),
                    'synthetic-other-confidence-fingerprint')
    claimed = flow.jobs.claim('synthetic-confidence-worker')
    draft = flow.process(claimed, lambda: False)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute('UPDATE workbench_message SET payload_json=? WHERE message_id=?',
            (json.dumps({**payload, field: new}), message.message_id))
    with pytest.raises(CompilationProblem, match='selected work changed'):
        flow.finish(claimed, draft)
    assert not bench.workspace.reports(matter.matter_id, ACTOR)


@pytest.mark.parametrize('surface', ['gap', 'pass'])
def test_malformed_saved_research_text_still_refuses_compilation(workspace, surface):
    from case_intelligence.report_materials import snapshot_report_materials
    from case_intelligence.work_product_exports import ExportProblem
    from case_intelligence.workspace_store import WorkspaceProblem

    _client, bench, matter = workspace
    job, _ = saved_research(bench, matter)
    result = copy.deepcopy(job.result)
    if surface == 'gap':
        result['gaps'][0]['note'] = {'malformed': 'synthetic'}
    else:
        result['passes'][1]['text'] = {'malformed': 'synthetic'}
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute('UPDATE workbench_research_job SET result_json=? WHERE job_id=?',
            (json.dumps(result), job.job_id))
    with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
        with pytest.raises((WorkspaceProblem, ExportProblem)):
            snapshot_report_materials(bench, matter, ACTOR, (f'research:{job.job_id}',))


@pytest.mark.parametrize('reviewed', [False, True])
def test_compiled_machine_rejection_preserves_human_note_and_raw_binding(reviewed):
    from types import SimpleNamespace
    from case_intelligence.answer_presentation import review_rejection_notice
    from case_intelligence.report_compilation import compile_report
    from case_intelligence.report_materials import material_snapshot_fingerprint
    from tests.test_report_materials import ACTOR as REVIEWER, MATTER, REVISION, bench_for, reference, selected, source

    old = 'Potentially relevant passages were found, but an inclusion decision did not pass source verification.'
    error = 'Source verification did not resolve an inclusion decision.'
    document = source(1)
    bench, _ = bench_for(document)
    decision = SimpleNamespace(document_id=document.document_id, source_version_id=document.version_id,
        source_name=document.display_name, source_basis_digest=bench._document_content_basis(document),
        machine_decision='needs_review', human_decision='include' if reviewed else '', reviewed_by=REVIEWER if reviewed else '',
        rationale=old, human_note=old if reviewed else '', citations=(reference(bench, document),),
        error_message=error, updated_at=REVISION)
    bench.workspace.runs['review-a'] = SimpleNamespace(run_id='review-a', state='succeeded', snapshot_count=1, updated_at=REVISION)
    bench.workspace.decisions['review-a'] = (decision,)
    materials = selected(bench, 'review:review-a')
    body = '\n'.join(s['body'] for s in compile_report('timeline', materials=materials).sections)
    assert 'Machine screening: needs_review.\n' + review_rejection_notice(old) in body
    assert 'Saved screening limitation: ' + review_rejection_notice(error) in body
    if reviewed:
        assert 'Human note: ' + old in body
    assert decision.rationale == old and decision.error_message == error
    before = material_snapshot_fingerprint(MATTER.matter_id, ('review:review-a',), materials)
    decision.rationale = review_rejection_notice(old)
    after_materials = selected(bench, 'review:review-a')
    assert material_snapshot_fingerprint(MATTER.matter_id, ('review:review-a',), after_materials) != before
    assert '\n'.join(s['body'] for s in compile_report('timeline', materials=after_materials).sections) == body


@pytest.mark.parametrize('field', ['missing_information', 'verification_notice', 'modality_coverage'])
def test_compilation_preserves_explicit_manual_notice_fields(workspace, field):
    from case_intelligence.generation import LEGACY_VERIFICATION_OMISSION_NOTICE
    from tests.test_rejected_generation_presentation import LEGACY
    client, bench, matter = workspace
    old = LEGACY if field == 'missing_information' else LEGACY_VERIFICATION_OMISSION_NOTICE if field == 'verification_notice' else CASES[0][1]
    value = coverage('complete', old) if field == 'modality_coverage' else old
    conversation = bench.workspace.get_conversation(matter.matter_id)
    bench.workspace.append_message(matter.matter_id, conversation.conversation_id, 'assistant', 'Synthetic manual response.',
        {'kind': 'manual', 'claims': [], 'introduction': 'Synthetic manual response.', field: value})
    state = finished(client, matter, queue(client, matter, [f'conversation:{conversation.conversation_id}']))
    assert state['state'] == 'succeeded', state
    report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
    body = '\n'.join(s.body for s in bench.workspace.report_sections(matter.matter_id, report.report_id))
    assert old in body
