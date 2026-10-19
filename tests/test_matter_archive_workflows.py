"""Preserve real synthetic workflow payloads, including unfinished research."""
import hashlib
import json
import uuid
import zipfile
from datetime import timedelta

from case_intelligence.matter_archive import build_matter_archive, cleanup_archive
from case_intelligence.matter_archive_projection import _SAFE_KEYS, _DROP_KEYS, _DYNAMIC_REFERENCE
from tests.test_full_text_synthesis import (workspace, ACTOR, seed_terminal_review,
                                           queue, finish)  # noqa: F401


def _close(bench, matter, actor):
    expires = bench.workspace._timestamp(bench.workspace.current_time() + timedelta(hours=1))
    session = bench.workspace.create_session(actor, hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        'test', expires, expires)
    return bench.workspace.case_lifecycle.begin_exit(matter.slug, actor, session_id=session.session_id)


def _unknown(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _DROP_KEYS:
                continue
            if key not in _SAFE_KEYS and not _DYNAMIC_REFERENCE.fullmatch(key):
                yield key
            yield from _unknown(child)
    elif isinstance(value, list):
        for child in value:
            yield from _unknown(child)


def test_preserves_full_text_ledger_and_synthesis_checkpoint(workspace):
    _, bench, matter, _ = workspace
    run, documents, statements = seed_terminal_review(bench, matter)
    result = finish(bench, queue(bench, matter, run))
    cancelled, _ = bench.queue_full_text_synthesis(matter, ACTOR, run.run_id, 'research-request-' + 'b'*32)
    bench.workspace.cancel_research_job(matter.matter_id, ACTOR, cancelled.job_id)
    failed = queue(bench, matter, run, letter='c')
    bench.workspace.fail_research_job(failed.job_id, 'Generated failed synthesis', expected_job=failed)
    from case_intelligence.matter_archive_schema import SCHEMA, EXCLUDED_TABLES, exported_columns, scope_query
    unknown = []
    for table in SCHEMA:
        if table in EXCLUDED_TABLES:
            continue
        # fixture-write: scope_query returns only read-only archive projection SELECT statements.
        for row in bench.workspace.connection.execute(scope_query(table), (matter.matter_id,)):
            for column in exported_columns(table):
                if column.endswith('_json') and row[column] is not None:
                    unknown.extend((table, column, key) for key in _unknown(json.loads(row[column])))
    assert sorted(set(unknown)) == []
    _close(bench, matter, ACTOR)
    store = bench.archive_source_store(matter)
    try:
        artifact = build_matter_archive(bench, matter, ACTOR, source_store=store)
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                research = [json.loads(row) for row in archive.read('records/workbench_research_job.jsonl').splitlines()]
                assert len(research) == 3
                assert {row['state'] for row in research} == {'succeeded', 'cancelled', 'failed'}
                assert all(row['plan_json']['full_text_synthesis_version'] == 1 for row in research)
                assert all(row['result_json']['full_text_synthesis_input'] for row in research)
                saved = next(row for row in research if row['state'] == 'succeeded')['result_json']
                assert {row['text'] for row in saved['answer']['claims']} == set(statements)
                assert saved['full_text_synthesis_input']['outcomes'] == result.result['full_text_synthesis_input']['outcomes']
                assert len(saved['hierarchical_synthesis']['issue']) == 6
                tokens = {source['support_token'] for source in saved['evidence']}
                assert all(token.startswith('reference-') for token in tokens)
                hierarchy = saved['hierarchical_synthesis']
                assert set(hierarchy['uncited_evidence']) <= tokens
                for node in [*hierarchy['issue'], *hierarchy['matter']]:
                    for claim in node['claims']:
                        assert set(claim['support_tokens']) <= tokens
                assert artifact.manifest['table_rows']['workbench_text_review_chunk'] >= 24
                assert artifact.manifest['table_rows']['workbench_review_decision'] == len(documents)
        finally:
            cleanup_archive(artifact)
    finally:
        store.close()


from tests.test_saved_answer_note_preview import long_answer  # noqa: F401,E402


def test_preserves_natural_saved_answer_payload(long_answer):
    _, bench, matter, _, _, _, message = long_answer
    unknown = sorted(set(_unknown(message.payload)))
    assert not unknown, repr(unknown)
    _close(bench, matter, ACTOR)
    store = bench.archive_source_store(matter)
    try:
        artifact = build_matter_archive(bench, matter, ACTOR, source_store=store)
        cleanup_archive(artifact)
    finally:
        store.close()


from tests.test_evidence_graph_workflow import connections  # noqa: F401,E402
from tests.test_answer_context import case, submit, execute  # noqa: F401,E402
from tests.test_matter_notebook import WEB_ACTOR  # noqa: E402


def test_preserves_natural_saved_context_and_removes_account_metadata(case):
    from case_intelligence.matter_archive_projection import reference
    bench, matter = case['bench'], case['matter']
    assert submit(case).status_code == 202
    job, _, message = execute(case)
    assert message is not None
    _close(bench, matter, WEB_ACTOR)
    store = bench.archive_source_store(matter)
    try:
        artifact = build_matter_archive(bench, matter, WEB_ACTOR, source_store=store)
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                get = lambda table: [json.loads(line) for line in archive.read('records/' + table + '.jsonl').splitlines()]
                context = get('workbench_answer_context')[0]
                saved_jobs = get('workbench_answer_job')
                assert context['job_id'] == saved_jobs[0]['job_id'] == reference(matter.matter_id, job.job_id)
                assert len(context['snapshot_json']['records']) == 4
                for record in context['snapshot_json']['records']:
                    assert 'created_by_name' not in record['record']
                    assert 'updated_by_name' not in record['record']
                attempts = get('workbench_answer_context_attempt')
                assert len(attempts) >= 2
                assert all(row['job_id'] == context['job_id'] for row in attempts)
                assert all('request' not in row['manifest_json'] for row in attempts)
                assert attempts[-1]['manifest_json']['admission']['selected_records']
        finally:
            cleanup_archive(artifact)
    finally:
        store.close()


def test_natural_compiled_report_preserves_basis_and_origin_links(workspace):
    from types import SimpleNamespace
    _, bench, matter, _ = workspace
    flow = bench.report_compilation
    flow.coordinator.close()
    bench.generator = SimpleNamespace(available=False)
    note, _ = bench.workspace.create_notebook_item(matter.matter_id, ACTOR, item_type='note', status='confirmed',
        title='Generated saved observation', body='The synthetic clock reading remains disputed.')
    job, created = flow.jobs.queue(matter.matter_id, ACTOR, 'timeline', '', ('note:' + note.item_id,), 'compile-request-' + 'd'*32)
    assert created
    claimed = flow.jobs.claim('generated-archive-test-worker')
    draft = flow.process(claimed, lambda: False)
    flow.finish(claimed, draft)
    _close(bench, matter, ACTOR)
    store = bench.archive_source_store(matter)
    try:
        artifact = build_matter_archive(bench, matter, ACTOR, source_store=store)
        try:
            with zipfile.ZipFile(artifact.path) as archive:
                get = lambda table: [json.loads(line) for line in archive.read('records/' + table + '.jsonl').splitlines()]
                compiled = get('workbench_report_compilation_job')[0]
                sections = get('workbench_report_section')
                assert compiled['state'] == 'succeeded'
                assert all(section['origin_id'] == compiled['job_id'] for section in sections)
                assert all(section['report_id'] == compiled['report_id'] for section in sections)
                assert {section['compilation_basis'] for section in sections} == {section['compilation_basis'] for section in draft.sections}
                assert compiled['selections_json'] == ['note:' + get('workbench_notebook_item')[0]['item_id']]
        finally:
            cleanup_archive(artifact)
    finally:
        store.close()
