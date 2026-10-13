"""Synthetic terminal-ledger synthesis, provenance, recovery and authorization."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch
import zipfile

import pytest
from fastapi.testclient import TestClient

from case_intelligence.full_text_review import FullTextReviewLedger
from case_intelligence.hierarchical_synthesis import POLICY
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.workbench import create_workbench_app
from case_intelligence.work_product_exports import ExportProblem
from case_intelligence.workflow_jobs import WorkflowFailure
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('full_text_synthetic_fixture', ROOT / 'scripts/evaluate-full-text-synthesis.py')
fixture_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture_module)
ACTOR = fixture_module.ACTOR
SourceEcho = fixture_module.SourceEcho
seed_terminal_review = fixture_module.seed_terminal_review
REFUSAL = (KeyError, ValueError, WorkspaceProblem, WorkflowFailure, ExportProblem)


@pytest.fixture
def workspace(tmp_path):
    client_generator = SourceEcho()
    app = create_workbench_app(tmp_path / 'runtime', generator=client_generator,
        auth_mode='test', storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app) as client:
        bench = app.state.workbench
        bench.research.close()
        matter = bench.create_matter('Synthetic full-text synthesis', '', ACTOR)
        yield client, bench, matter, client_generator


def queue(bench, matter, run, letter='a'):
    job, created = bench.queue_full_text_synthesis(matter, ACTOR, run.run_id,
        'research-request-' + letter * 32)
    assert created
    claimed = bench.workspace.claim_research_job('synthetic-synthesis-worker')
    assert claimed.job_id == job.job_id
    return claimed


def finish(bench, job):
    result = bench._process_research_job(job, lambda: False)
    return bench._finish_research_job(job, result)


def human_edit(bench, matter, run, document):
    current = bench.workspace.review_decision(matter.matter_id, ACTOR, run.run_id, document.document_id)
    return bench.workspace.adjudicate_review_decision(matter.matter_id, ACTOR,
        run.run_id, document.document_id, human_decision='uncertain',
        expected_updated_at=current.updated_at, note='Synthetic frozen-context revision.')


def test_terminal_full_text_24_units_retains_both_accounts_without_search(workspace, monkeypatch):
    _, bench, matter, model = workspace
    run, documents, statements = seed_terminal_review(bench, matter)
    def forbidden(*args, **kwargs):
        raise AssertionError('Full-text synthesis must neither search nor materialize an entire source')
    monkeypatch.setattr(bench, '_answer_search', forbidden)
    monkeypatch.setattr(PilotDocument, 'parsed_units', forbidden)
    job = queue(bench, matter, run)
    completed = finish(bench, job)
    result = completed.result
    assert {item['text'] for item in result['answer']['claims']} == set(statements)
    receipt = result['full_text_synthesis_input']
    assert receipt['run_id'] == run.run_id and receipt['matter_id'] == matter.matter_id
    assert receipt['criterion_version_id'] == run.criterion_version_id
    assert receipt['human_decisions']['mode'] == 'frozen_context_not_evidence'
    assert receipt['version'] == 1 and receipt['decision_revision_digest']
    assert Counter(reason for _, reason in receipt['outcomes']) == {'admitted': 24}
    assert len(result['hierarchical_synthesis']['issue']) == 6
    assert len(result['hierarchical_synthesis']['matter']) == 6
    assert result['hierarchical_synthesis']['requests_spent'] == len(model.calls) == 12
    assert all('derived_findings_not_evidence' in call['working_context'] for call in model.calls)
    again, created = bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, 'research-request-' + 'a' * 32)
    assert not created and again.job_id == job.job_id


def test_coverage_accounts_for_every_candidate_and_negative_failed_empty_outcome(workspace):
    _, bench, matter, model = workspace
    run, _, statements = seed_terminal_review(bench, matter, extra_outcomes=True)
    completed = finish(bench, queue(bench, matter, run))
    receipt = completed.result['full_text_synthesis_input']
    reasons = Counter(reason for _, reason in receipt['outcomes'])
    assert reasons == {'admitted': 24, 'unsupported_finding': 1}
    original = FullTextReviewLedger(bench.workspace).coverage(matter.matter_id, ACTOR, run.run_id)
    assert receipt['coverage']['sources'] == original['sources']
    assert receipt['coverage']['units'] == original['units']
    assert receipt['coverage']['budget'] == original['budget']
    assert receipt['coverage']['ranges'] == {'no_finding': 1, 'failed': 1}
    assert original['sources']['unavailable'] == 1 and original['zero_unit_sources'] == 1
    assert original['units']['failed'] == 1 and original['units']['empty'] == 1
    with bench.workspace._lock:
        assert len(receipt['outcomes']) == bench.workspace.connection.execute(
            "SELECT count(*) FROM workbench_text_review_chunk WHERE run_id=? AND state='processed' AND decision='include'", (run.run_id,)).fetchone()[0]
    assert {row['text'] for row in completed.result['answer']['claims']} == set(statements)
    assert 'Jupiter' not in completed.result['summary']
    assert all('Jupiter' not in row['working_context'] for row in model.calls)
    assert receipt['partial'] and receipt['coverage']['has_gaps']


def test_long_original_retains_explicit_omission_for_decisive_accounts_after_6000(workspace):
    _, bench, matter, model = workspace
    run, documents, statements = seed_terminal_review(bench, matter, long_unit=True)
    long_text = tuple(documents[0].iter_parsed_units())[-1].text
    assert long_text.index('Synthetic late account states') > 6000
    assert long_text.index('Synthetic late competing account states') > 6000
    completed = finish(bench, queue(bench, matter, run))
    receipt = completed.result['full_text_synthesis_input']
    assert Counter(reason for _, reason in receipt['outcomes']) == {'admitted': 24, 'oversized_original': 1}
    cursor = next(cursor for cursor, reason in receipt['outcomes'] if reason == 'oversized_original')
    with bench.workspace._lock:
        row = bench.workspace.connection.execute('SELECT * FROM workbench_text_review_chunk WHERE run_id=? AND cursor=?', (run.run_id, cursor)).fetchone()
    assert row['unit_ordinal'] == 25 and row['packet_start'] > 0
    assert all('Synthetic late account' not in source.excerpt for call in model.calls for source in call['evidence'])
    assert {item['text'] for item in completed.result['answer']['claims']} == set(statements)
    for format_name in ('json', 'markdown', 'docx'):
        artifact = bench.export_research_work_product(matter, completed, format_name)
        assert artifact.body
        if format_name == 'json':
            exported = json.loads(artifact.body)['investigation']
            assert exported['full_text_synthesis_input']['outcomes'] == receipt['outcomes']


def test_admission_limit_counts_late_omissions_without_loading_ledger(workspace, monkeypatch):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=105)
    monkeypatch.setattr(PilotDocument, 'parsed_units', lambda *a: (_ for _ in ()).throw(AssertionError('Unbounded source read')))
    completed = finish(bench, queue(bench, matter, run))
    outcomes = completed.result['full_text_synthesis_input']['outcomes']
    assert len(outcomes) == 105 and len({cursor for cursor, _ in outcomes}) == 105
    assert Counter(reason for _, reason in outcomes) == {'admitted': 48, 'finding_limit': 57}
    assert completed.result['full_text_synthesis_input']['counts']
    assert 'not delivered' not in completed.result['summary']


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_active_review_is_rejected_before_any_synthesis_job(workspace, state):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute('UPDATE workbench_review_run SET state=? WHERE run_id=?', (state, run.run_id))
    with pytest.raises(REFUSAL):
        bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, 'research-request-' + 'a' * 32)
    assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)


def test_foreign_matter_and_non_full_text_run_are_rejected(workspace):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter, count=1)
    other = bench.create_matter('Synthetic foreign matter', '', ACTOR)
    with pytest.raises(REFUSAL):
        bench.queue_full_text_synthesis(other, ACTOR, run.run_id, 'research-request-' + 'a' * 32)
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute('DELETE FROM workbench_text_review WHERE run_id=?', (run.run_id,))
    with pytest.raises(REFUSAL):
        bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, 'research-request-' + 'a' * 32)


@pytest.mark.parametrize('change', ['missing', 'version', 'actual_text', 'run_revision'])
def test_stale_input_rejected_without_spend(workspace, change):
    _, bench, matter, model = workspace
    run, documents, _ = seed_terminal_review(bench, matter, count=1)
    job = queue(bench, matter, run)
    document = documents[0]
    if change == 'missing':
        bench.source_store(matter).remove(document.document_id)
    elif change == 'version':
        document.version_id = 'f' * 32
    elif change == 'actual_text':
        path = bench.source_store(matter).derived / document.units_file
        value = json.loads(path.read_text())
        value['units'][0]['text'] += ' Changed without updating the saved digest.'
        path.write_text(json.dumps(value))
    else:
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute("UPDATE workbench_review_run SET updated_at='2099-01-01T00:00:00Z' WHERE run_id=?", (run.run_id,))
    with pytest.raises(REFUSAL):
        bench._process_research_job(job, lambda: False)
    assert not model.calls


@pytest.mark.parametrize('boundary', ['before_processing', 'during_generation', 'before_finish'])
def test_human_decision_revision_without_run_time_change_cannot_change_basis(workspace, monkeypatch, boundary):
    _, bench, matter, _ = workspace
    run, documents, _ = seed_terminal_review(bench, matter, count=1)
    job = queue(bench, matter, run)
    timestamp = run.updated_at
    if boundary == 'before_processing':
        human_edit(bench, matter, run, documents[0])
    elif boundary == 'during_generation':
        original = bench.generator.answer
        def answer(*args, **kwargs):
            result = original(*args, **kwargs)
            human_edit(bench, matter, run, documents[0])
            return result
        monkeypatch.setattr(bench.generator, 'answer', answer)
    if boundary == 'before_finish':
        result = bench._process_research_job(job, lambda: False)
        human_edit(bench, matter, run, documents[0])
        with pytest.raises(REFUSAL):
            bench._finish_research_job(job, result)
    else:
        with pytest.raises(REFUSAL):
            bench._process_research_job(job, lambda: False)
    assert bench.workspace.review_run(matter.matter_id, ACTOR, run.run_id).updated_at == timestamp
    saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    assert saved.state == 'running' and saved.result_message_id is None


def test_interruption_clean_database_restore_reuses_nodes_and_retains_spend(workspace, tmp_path, monkeypatch):
    _, bench, matter, model = workspace
    run, _, _ = seed_terminal_review(bench, matter)
    job = queue(bench, matter, run)
    checkpoint = bench.workspace.checkpoint_research_job
    def interrupted(*args, **kwargs):
        saved = checkpoint(*args, **kwargs)
        hierarchy = saved.result.get('hierarchical_synthesis', {})
        if hierarchy.get('requests_spent') == 3 and len(hierarchy.get('issue', [])) == 2:
            raise RuntimeError('Synthetic interruption after durable charge')
        return saved
    monkeypatch.setattr(bench.workspace, 'checkpoint_research_job', interrupted)
    with pytest.raises(RuntimeError, match='Synthetic interruption'):
        bench._process_research_job(job, lambda: False)
    saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    initial = deepcopy(saved.result['hierarchical_synthesis'])
    target = tmp_path / 'clean-control.sqlite'
    with bench.workspace._lock, sqlite3.connect(target) as output:
        bench.workspace.connection.backup(output)
    restored = WorkspaceStore(target)
    old = bench.workspace
    try:
        assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
        assert restored.recover_running_research_jobs() == 1
        bench.workspace = restored
        resumed = restored.claim_research_job('synthetic-restored-worker')
        completed = finish(bench, resumed)
        state = completed.result['hierarchical_synthesis']
        assert state['issue'][:2] == initial['issue']
        assert state['requests_spent'] == 13
        assert len({row['id'] for row in state['issue']}) == len(state['issue'])
        assert len(model.calls) == 12
        for format_name in ('json', 'markdown', 'docx'):
            assert bench.export_research_work_product(matter, completed, format_name).body
    finally:
        bench.workspace = old
        restored.close()


@pytest.mark.parametrize('reason', ['generation_budget', 'time_budget'])
def test_exhausted_hierarchy_retains_explicit_partial(workspace, monkeypatch, reason):
    _, bench, matter, _ = workspace
    run, _, _ = seed_terminal_review(bench, matter)
    job = queue(bench, matter, run)
    checkpoint = bench.workspace.checkpoint_research_job
    def stop(*args, **kwargs):
        saved = checkpoint(*args, **kwargs)
        if saved.result.get('hierarchical_synthesis', {}).get('requests_spent') == 1:
            raise RuntimeError('Synthetic stopped dispatch')
        return saved
    monkeypatch.setattr(bench.workspace, 'checkpoint_research_job', stop)
    with pytest.raises(RuntimeError, match='Synthetic stopped'):
        bench._process_research_job(job, lambda: False)
    monkeypatch.setattr(bench.workspace, 'checkpoint_research_job', checkpoint)
    saved = bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id)
    payload = deepcopy(saved.result)
    hierarchy = payload['hierarchical_synthesis']
    if reason == 'generation_budget':
        hierarchy['requests_spent'] = POLICY['generation_requests']
    else:
        hierarchy['started_at'] = 1
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute('UPDATE workbench_research_job SET result_json=? WHERE job_id=?', (json.dumps(payload), job.job_id))
    completed = finish(bench, bench.workspace.research_job(matter.matter_id, ACTOR, job.job_id))
    result = completed.result['hierarchical_synthesis']
    assert result['partial'] and result['stop_reason'] == reason and result['omitted_groups']


@pytest.mark.parametrize('change', ['cancel', 'membership', 'source', 'source_set', 'attempt'])
def test_generation_boundary_refuses_late_save(workspace, monkeypatch, change):
    client, bench, matter, _ = workspace
    run, documents, _ = seed_terminal_review(bench, matter, count=2, selected_set=True)
    job = queue(bench, matter, run)
    original = bench.generator.answer
    def answer(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'cancel':
            bench.workspace.cancel_research_job(matter.matter_id, ACTOR, job.job_id)
        elif change == 'membership':
            with bench.workspace._lock, bench.workspace.connection:
                bench.workspace.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=?", (matter.matter_id,))
        elif change == 'source':
            bench.source_store(matter).remove(documents[0].document_id)
        elif change == 'source_set':
            bench.workspace.remove_source_organization(matter.matter_id, documents[0].document_id)
        else:
            with bench.workspace._lock, bench.workspace.connection:
                bench.workspace.connection.execute('UPDATE workbench_research_job SET attempts=attempts+1 WHERE job_id=?', (job.job_id,))
        return result
    monkeypatch.setattr(bench.generator, 'answer', answer)
    with pytest.raises(REFUSAL):
        bench._process_research_job(job, lambda: False)
    with bench.workspace._lock:
        row = bench.workspace.connection.execute('SELECT result_json,result_message_id FROM workbench_research_job WHERE job_id=?', (job.job_id,)).fetchone()
    saved = json.loads(row['result_json'])['hierarchical_synthesis']
    assert saved['requests_spent'] == 1 and not saved['issue'] and row['result_message_id'] is None

    if change == 'membership':
        for path in (f'/matters/{matter.slug}/research/{job.job_id}/status',
                     f'/matters/{matter.slug}/research/{job.job_id}/export?format=json'):
            response = client.get(path)
            assert response.status_code in (403, 404)


@pytest.mark.parametrize('change', ['source_set', 'membership', 'source'])
def test_final_transaction_and_export_revalidate_sources_and_access(workspace, change):
    _, bench, matter, _ = workspace
    run, documents, _ = seed_terminal_review(bench, matter, count=2, selected_set=True)
    job = queue(bench, matter, run)
    result = bench._process_research_job(job, lambda: False)
    if change == 'source_set':
        bench.workspace.remove_source_organization(matter.matter_id, documents[0].document_id)
    elif change == 'source':
        bench.source_store(matter).remove(documents[0].document_id)
    else:
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=?", (matter.matter_id,))
    # Observe the rejected save and its rollback before another live worker can
    # open an unrelated transaction. Preserve the source-cache/source/workspace
    # lock order when the finisher resolves and validates its source store again.
    store = bench.source_store(matter)
    with bench._store_lock, store.mutation_guard(), bench.workspace._lock:
        with pytest.raises(REFUSAL):
            bench._finish_research_job(job, result)
        row = bench.workspace.connection.execute('SELECT state,result_message_id FROM workbench_research_job WHERE job_id=?', (job.job_id,)).fetchone()
        assert row['state'] == 'running' and row['result_message_id'] is None
        assert not bench.workspace.connection.in_transaction


def test_export_fidelity_original_links_and_tampered_receipt_refusal(workspace):
    client, bench, matter, _ = workspace
    run, _, statements = seed_terminal_review(bench, matter)
    completed = finish(bench, queue(bench, matter, run))
    for format_name in ('json', 'markdown', 'docx'):
        response = client.get(f'/matters/{matter.slug}/research/{completed.job_id}/export?format={format_name}')
        assert response.status_code == 200
        if format_name == 'docx':
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                body = archive.read('word/document.xml').decode()
        else:
            body = response.text
        assert statements[0] in body and statements[-1] in body
        assert run.run_id in body
        assert '/matters/' not in body and 'support_token' not in body
    payload = deepcopy(completed.result)
    payload['full_text_synthesis_input']['version'] = 999
    with pytest.raises(REFUSAL):
        bench.export_research_work_product(matter, replace(completed, result=payload), 'json')


def test_default_and_evaluation_wire_settings_are_explicit(monkeypatch):
    calls = []
    monkeypatch.setattr(fixture_module, '_bounded_json_request', lambda url, payload, **kwargs:
        calls.append(payload) or {'message': {'content': '{"answerable":false,"claims":[],"missing_information":"Synthetic probe"}'}})
    fixture_module.EvaluationGenerator('http://127.0.0.1:11439', fixture_module.MODEL).generate(
        question='Synthetic question', evidence=[])
    assert calls[0]['think'] is False
    assert calls[0]['options']['num_ctx'] == 8192 and calls[0]['options']['num_predict'] == 1200


def test_completed_synthesis_keeps_frozen_human_context_after_edit_and_run_delete(workspace):
    client, bench, matter, _ = workspace
    run, documents, _ = seed_terminal_review(bench, matter, count=2)
    completed = finish(bench, queue(bench, matter, run))
    frozen = deepcopy(completed.result['full_text_synthesis_input'])
    human_edit(bench, matter, run, documents[0])
    for delete_run in (False, True):
        if delete_run:
            FullTextReviewLedger(bench.workspace).delete(matter.matter_id, ACTOR, run.run_id)
        response = client.get(f'/matters/{matter.slug}/research/{completed.job_id}/export?format=json')
        assert response.status_code == 200
        assert response.json()['investigation']['full_text_synthesis_input']['decision_revision_digest'] == frozen['decision_revision_digest']
        assert response.json()['investigation']['full_text_synthesis_input']['human_decisions'] == frozen['human_decisions']


@pytest.mark.parametrize('change', ['source', 'source_set', 'membership'])
def test_completed_synthesis_export_refuses_revoked_originals_or_access(workspace, change):
    client, bench, matter, _ = workspace
    run, documents, statements = seed_terminal_review(bench, matter, count=2, selected_set=True)
    completed = finish(bench, queue(bench, matter, run))
    if change == 'source':
        bench.source_store(matter).remove(documents[0].document_id)
    elif change == 'source_set':
        bench.workspace.remove_source_organization(matter.matter_id, documents[0].document_id)
    else:
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=?", (matter.matter_id,))
    response = client.get(f'/matters/{matter.slug}/research/{completed.job_id}/export?format=json')
    assert response.status_code in (403, 404, 409)
    assert not any(statement in response.text for statement in statements)


def test_queue_refuses_a_page_snapshot_after_human_adjudication(workspace):
    _, bench, matter, _ = workspace
    run, documents, _ = seed_terminal_review(bench, matter, count=2)
    prepared = bench._full_text_synthesis_service(matter).prepare(matter.matter_id, ACTOR, run.run_id)
    human_edit(bench, matter, run, documents[0])
    with pytest.raises(WorkspaceProblem, match='changed'):
        bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, 'research-request-' + 'a' * 32,
            expected_snapshot=prepared['full_text_synthesis_input']['snapshot_digest'])
    assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)


def test_evaluation_harness_checks_real_adapter_accounting_with_controlled_model(tmp_path, monkeypatch):
    """A harness contract test, expressly not a local-model quality receipt."""
    output = tmp_path / 'controlled-harness-receipt.json'
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    monkeypatch.setattr(fixture_module.sys, 'argv', ['evaluate-full-text-synthesis.py',
        '--disable-thinking', '--output', str(output)])
    monkeypatch.setattr(fixture_module, '_bounded_json_get', lambda url, **kwargs:
        {'models': [{'name': fixture_module.MODEL, 'digest': fixture_module.DIGEST}]}
        if url.endswith('/api/tags') else {'version': 'controlled-test-runtime'})
    monkeypatch.setattr(fixture_module, 'EvaluationGenerator', lambda *args, **kwargs: SourceEcho())
    with patch.dict(os.environ, dict(os.environ), clear=True):
        assert fixture_module.main() == 0
    receipt = json.loads(output.read_text())
    assert receipt['passed'] and receipt['omission_and_failure_accounting_verified']
    assert receipt['original_statement_recall'] == {'recalled': 24, 'total': 24}
    assert receipt['false_claims'] == 0 and receipt['result']['full_text_synthesis_input']['partial']
