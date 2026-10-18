from __future__ import annotations

from tests.fixture_write import fixture_write
from dataclasses import asdict, replace
import hashlib
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

from case_intelligence.full_text_review import FullTextReviewLedger, TextReviewPolicy, text_ranges
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.unit_stream import iter_unit_records
from case_intelligence.workspace_store import WorkspaceProblem, WorkspaceStore

ACTOR = 'development-taylor-morgan'


def test_every_character_has_one_canonical_range_and_context_overlap():
    text = 'x' * 25_000
    ranges = list(text_ranges(text))
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
    assert all(end - start <= 6_000 for _, _, start, end in ranges)
    boundary = ranges[0][1]
    phrase = 'The amber bicycle arrived at noon.'
    text = text[:boundary - 10] + phrase + text[boundary + len(phrase) - 10:]
    assert sum(phrase in text[start:end] for _, _, start, end in ranges) >= 2
    assert sum(end - start for start, end, _, _ in ranges) == len(text)
    assert list(text_ranges('')) == []
    with pytest.raises(ValueError):
        TextReviewPolicy(packet_chars=6_001)


@pytest.mark.parametrize('state', ['running', 'queued'])
def test_repeated_cancellation_preserves_one_event_and_ledger_charge(frozen, state):
    store, matter, run, _ = frozen
    if state == 'queued':
        with fixture_write(store):
            store.connection.execute("UPDATE workbench_review_run SET state='queued',worker_id=NULL WHERE run_id=?", (run.run_id,))
    def snapshot():
        events = tuple(tuple(row) for row in store.connection.execute(
            'SELECT * FROM workbench_review_event WHERE run_id=? ORDER BY rowid', (run.run_id,)))
        budget = tuple(store.connection.execute(
            'SELECT * FROM workbench_text_review_budget WHERE run_id=?', (run.run_id,)).fetchone())
        return events, budget
    before, _ = snapshot()
    first = store.cancel_review_run(matter.matter_id, ACTOR, run.run_id)
    saved = snapshot()
    assert len(saved[0]) == len(before) + 1
    for _ in range(100):
        assert store.cancel_review_run(matter.matter_id, ACTOR, run.run_id) == first
    assert snapshot() == saved


def test_concurrent_cancellation_refreshes_state_inside_write_transaction(frozen, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    store, matter, run, _ = frozen
    other = WorkspaceStore(store.path)
    barrier = threading.Barrier(2)
    before = store.connection.execute('SELECT count(*) FROM workbench_review_event WHERE run_id=?', (run.run_id,)).fetchone()[0]
    for instance in (store, other):
        original = instance.review_run
        def stale_admission(*args, _original=original, _seen=[False], **kwargs):
            result = _original(*args, **kwargs)
            if not _seen[0]:
                _seen[0] = True
                barrier.wait(timeout=3)
            return result
        monkeypatch.setattr(instance, 'review_run', stale_admission)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(instance.cancel_review_run, matter.matter_id, ACTOR, run.run_id)
                       for instance in (store, other)]
            assert all(result.result(timeout=5).cancellation_requested for result in results)
        after = store.connection.execute('SELECT count(*) FROM workbench_review_event WHERE run_id=?', (run.run_id,)).fetchone()[0]
        assert after == before + 1
    finally:
        other.close()


def test_unit_stream_reads_existing_format_incrementally_and_validates_tail():
    units = [dict(number=i, text=('Synthetic text ' * 5_000 if i == 2 else 'Synthetic text')) for i in range(1, 16)]
    class Stream(io.StringIO):
        def read(self, count=-1):
            assert 0 < count <= 65_536
            return super().read(min(count, 17))
    assert list(iter_unit_records(Stream(json.dumps({'version': 1, 'units': units})))) == units
    assert list(iter_unit_records(Stream(json.dumps({'units': units, 'version': 1})))) == units
    with pytest.raises(ValueError):
        list(iter_unit_records(Stream(json.dumps({'version': 2, 'units': units}))))
    with pytest.raises(ValueError):
        list(iter_unit_records(Stream('{"version":1,"units":[]} trailing')))


@pytest.fixture
def frozen(tmp_path):
    store = WorkspaceStore(tmp_path / 'workspace.sqlite')
    store.upsert_principal('test', ACTOR, 'Synthetic text reviewer', ACTOR, preferred_principal_id=ACTOR)
    matter = store.create_matter('Synthetic text review', '', ACTOR)
    source = dict(document_id='a' * 32, version_id='c' * 32, action_token='d' * 32,
        display_name='Synthetic extracted text', relative_path='synthetic.txt', media_type='text/plain', kind='TXT',
        source_state='ready', tone='ready', state_label='Searchable', count_label='15 units', processing_stage='',
        completed_units=15,total_units=15,page_count=0,duration_ms=0,byte_size=120,origin='upload',retryable=False,
        removable=True,has_video=False,content_basis_digest='b' * 64)
    store.upsert_source_catalog(matter.matter_id, [source])
    _criterion, version = store.create_review_criterion(matter.matter_id, ACTOR,
        title='Find the bicycle', instructions='Include passages describing the amber bicycle.')
    run = store.queue_review_run(matter.matter_id, ACTOR, version.criterion_version_id, run_kind='full', review_mode='full_text')
    run = store.claim_review_run('synthetic-text-worker')
    decision = store.next_review_decision(run.run_id)
    yield store, matter, run, decision
    store.close()


def test_durable_coverage_resume_attempt_fencing_and_clean_restore(frozen, tmp_path):
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    units = [PilotUnit(i, 'Amber bicycle' if i == 15 else 'Synthetic routine entry') for i in range(1, 16)]
    ledger.inventory(run, decision, iter(units), current_source=lambda: True)
    for ordinal in range(1, 15):
        chunk = ledger.unit_chunks(run.run_id, decision.document_id, ordinal)[0]
        ledger.record(run, decision, chunk, state='failed' if ordinal == 3 else 'processed',
            label='' if ordinal == 3 else 'exclude', rationale='Synthetic outcome', current_source=lambda: True)
    coverage = ledger.coverage(matter.matter_id, ACTOR, run.run_id)
    assert coverage['units'] == {'failed': 1, 'pending': 1, 'processed': 13}
    store.fail_review_run(run.run_id, 'Synthetic restart')
    store.retry_review_run(matter.matter_id, ACTOR, run.run_id)
    resumed = store.claim_review_run('synthetic-resumed-worker')
    last = ledger.unit_chunks(run.run_id, decision.document_id, 15)[0]
    with pytest.raises(WorkspaceProblem, match='no longer active'):
        ledger.record(run, decision, last, state='processed', label='include', rationale='Stale worker', current_source=lambda: True)
    assert ledger.record(resumed, decision, last, state='processed', label='include', rationale='The amber bicycle appears.', current_source=lambda: True)
    assert not ledger.record(resumed, decision, last, state='processed', label='include', rationale='Duplicate', current_source=lambda: True)
    rows = []
    while page := ledger.rows(matter.matter_id, ACTOR, run.run_id, after=rows[-1]['cursor'] if rows else 0, limit=4):
        rows.extend(page)
    assert len(rows) == 15 and rows[-1]['rationale'] == 'The amber bicycle appears.'
    restored_path = tmp_path / 'clean-restore.sqlite'
    with sqlite3.connect(restored_path) as backup:
        store.connection.backup(backup)
    restored = WorkspaceStore(restored_path)
    try:
        assert FullTextReviewLedger(restored).coverage(matter.matter_id, ACTOR, run.run_id)['units'] == {'failed':1, 'processed':14}
        assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        restored.close()


def test_cancel_revocation_and_source_invalidation_refuse_late_findings(frozen):
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    ledger.inventory(run, decision, [PilotUnit(1, 'Synthetic source')], current_source=lambda: True)
    chunk = ledger.unit_chunks(run.run_id, decision.document_id, 1)[0]
    store.cancel_review_run(matter.matter_id, ACTOR, run.run_id)
    with pytest.raises(WorkspaceProblem, match='no longer active'):
        ledger.record(run, decision, chunk, state='processed', label='include', current_source=lambda: True)
    store.fail_review_run(run.run_id, 'Review cancelled.')
    store.retry_review_run(matter.matter_id, ACTOR, run.run_id)
    resumed = store.claim_review_run('synthetic-resumed-worker')
    assert not ledger.record(resumed, decision, chunk, state='processed', label='include', current_source=lambda: False)
    assert ledger.coverage(matter.matter_id, ACTOR, run.run_id)['units'] == {'invalidated': 1}
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_matter_membership SET state='revoked' WHERE matter_id=?", (matter.matter_id,))
    with pytest.raises(KeyError):
        ledger.rows(matter.matter_id, ACTOR, run.run_id)
    with pytest.raises(KeyError):
        ledger.record(resumed, decision, chunk, state='processed', label='include', current_source=lambda: True)


def test_workbench_full_text_finds_late_unit_and_records_failure_without_search(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator, GroundedGenerationService, GenerationRejected
    from case_intelligence.workbench import create_workbench_app
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='test')
    with TestClient(app) as client:
        bench = app.state.workbench
        bench.full_review.close()
        bench.research.close()
        principal = bench.workspace.get_principal(ACTOR)
        matter = bench.create_matter('Synthetic complete text', '', principal.principal_id)
        store = bench.source_store(matter)
        document, _ = store.store_stream('synthetic-complete.txt', 'text/plain', io.BytesIO(b'Initial synthetic record.'))
        document.units = [asdict(PilotUnit(i, 'The amber bicycle arrived at noon.' if i == 15 else
            'Synthetic failed unit.' if i == 4 else f'Routine synthetic entry {i}.', excerpt_digest=hashlib.sha256(str(i).encode()).hexdigest())) for i in range(1, 16)]
        for item in document.units:
            item['excerpt_digest'] = hashlib.sha256(item['text'].encode()).hexdigest()
        store._write_units(document)
        bench._sync_source_catalog(matter, [document])
        _criterion, version = bench.workspace.create_review_criterion(matter.matter_id, ACTOR,
            title='Bicycle', instructions='Find the amber bicycle.')
        queued = bench.workspace.queue_review_run(matter.matter_id, ACTOR, version.criterion_version_id,
            run_kind='full', review_mode='full_text')
        run = bench.workspace.claim_review_run('synthetic-text-worker')
        assert run.run_id == queued.run_id
        decision = bench.workspace.next_review_decision(run.run_id)
        def no_search(*args, **kwargs):
            raise AssertionError('Full text must not select search snippets')
        monkeypatch.setattr(bench, 'search', no_search)
        seen = []
        def classify(**kwargs):
            excerpt = kwargs['evidence'][0].excerpt
            seen.append(excerpt)
            if 'failed unit' in excerpt:
                raise GenerationRejected('Synthetic unit failure')
            included = 'amber bicycle' in excerpt
            return {'decision': 'include' if included else 'not_identified',
                'rationale': 'The amber bicycle arrived at noon.' if included else 'No bicycle appears in this range.',
                'evidence_ids': ['S1'] if included else []}
        bench.generator = GroundedGenerationService(SimpleNamespace(available=True, classify_source=classify))
        from case_intelligence.pilot_uploads import PilotDocument
        monkeypatch.setattr(PilotDocument, 'parsed_units', lambda *a: (_ for _ in ()).throw(AssertionError('No whole-document citation loading')))

        outcome = bench._process_review_decision(run, decision, lambda: False)
        assert len(seen) == 15 and outcome.decision == 'included'
        assert '1 failed ranges' in outcome.rationale
        assert outcome.citations[0]['unit_number'] == 15
        bench._record_review_decision(run, decision, outcome)
        bench._finish_review_run(run)
        coverage = FullTextReviewLedger(bench.workspace).coverage(matter.matter_id, ACTOR, run.run_id)
        assert coverage['units'] == {'failed': 1, 'processed': 14}
        assert coverage['inventory_complete']

        response = client.get(f'/matters/{matter.slug}/full-review?criterion={run.criterion_id}&run={run.run_id}')
        assert response.status_code == 200 and 'Review all extracted text' in response.text and 'Check selected passages' in response.text
        response = client.get(f'/matters/{matter.slug}/full-review/{run.run_id}/text')
        assert response.status_code == 200 and '14</strong> units fully processed' in response.text
        assert 'Review finished with gaps' in response.text
        assert '1</strong> unit with failed ranges' in response.text
        response = client.get(f'/matters/{matter.slug}/full-review/{run.run_id}/text/export?format=json')
        assert response.status_code == 200
        records = response.json()['records']
        ranges = [row for row in records if row['record_type'] == 'range']
        assert len(ranges) == 15 and ranges[-1]['decision'] == 'include'
        assert len([row for row in records if row['record_type'] == 'unit']) == 15
        assert any(row.get('state') == 'failed' for row in ranges)
        csv_response = client.get(f'/matters/{matter.slug}/full-review/{run.run_id}/text/export?format=csv')
        assert csv_response.status_code == 200 and 'amber bicycle' in csv_response.text
        monkeypatch.undo()
        import zipfile
        response = client.get(f'/matters/{matter.slug}/export')
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            name = next(name for name in bundle.namelist() if name.endswith('-full-text-ledger.json'))
            assert len([row for row in json.loads(bundle.read(name))['records'] if row['record_type'] == 'range']) == 15


def test_streaming_basis_matches_existing_digest_and_reads_no_whole_document(tmp_path):
    from case_intelligence.pilot_uploads import PilotDocument
    from case_intelligence.workbench import CaseIntelligenceWorkbench
    units = tuple(PilotUnit(i, 'Synthetic record', excerpt_digest=f'{i:064x}') for i in range(1, 41))
    document = PilotDocument('a' * 32, 'Synthetic source', 'synthetic.txt', 'text/plain', 100, 'ready', '', [], version_id='c' * 32,
        units_file='a' * 32 + '.json', _units_iterator=lambda _: iter(units),
        _units_loader=lambda _: (_ for _ in ()).throw(AssertionError('Whole-document loading is forbidden here')))
    expected = json.dumps({'source_version': document.version_id, 'units': [
        {'number': unit.number, 'digest': unit.excerpt_digest, 'line_start': unit.line_start,
         'line_end': unit.line_end, 'start_ms': unit.start_ms, 'end_ms': unit.end_ms} for unit in units
    ]}, separators=(',', ':'), sort_keys=True)
    assert CaseIntelligenceWorkbench._document_content_basis(document) == hashlib.sha256(expected.encode()).hexdigest()


def test_changed_catalog_invalidates_uninventoried_source_and_cascades(frozen):
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    # Later source readiness cannot rewrite the frozen extraction state.
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_source_catalog SET source_state='failed',state_label='OCR unavailable' WHERE matter_id=?", (matter.matter_id,))
    before = ledger.extraction_rows(matter.matter_id, ACTOR, run.run_id)[0]
    assert before['source_state'] == 'ready'
    ledger.inventory(run, decision, [PilotUnit(1, 'Synthetic record')], current_source=lambda: True)
    coverage = ledger.coverage(matter.matter_id, ACTOR, run.run_id)
    assert coverage['sources'] == {'invalidated': 1}
    assert not coverage['inventory_complete']
    assert coverage['unresolved_inventory_sources'] == 1
    with fixture_write(store):
        store.connection.execute('DELETE FROM workbench_review_run WHERE run_id=?', (run.run_id,))
    for table in ('workbench_text_review', 'workbench_text_review_source', 'workbench_text_review_unit', 'workbench_text_review_chunk'):
        assert store.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_incremental_exports_keep_partial_checkpoint_and_refuse_foreign_actor(frozen):
    from case_intelligence.full_text_review import iter_text_export
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    ledger.inventory(run, decision, [PilotUnit(1, 'Synthetic first range'), PilotUnit(2, 'Synthetic pending range')], current_source=lambda: True)
    first = ledger.unit_chunks(run.run_id, decision.document_id, 1)[0]
    ledger.record(run, decision, first, state='processed', label='include', rationale='Saved before cancellation', current_source=lambda: True)
    store.cancel_review_run(matter.matter_id, ACTOR, run.run_id)
    store.fail_review_run(run.run_id, 'Review cancelled.')
    output = json.loads(b''.join(iter_text_export(store, matter.matter_id, ACTOR, run.run_id)))
    assert output['records'][0]['state'] == 'cancelled'
    assert [row['state'] for row in output['records'] if row['record_type'] == 'range'] == ['processed', 'pending']
    with pytest.raises(KeyError):
        b''.join(iter_text_export(store, matter.matter_id, 'foreign-synthetic-reviewer', run.run_id))



def test_admission_freezes_unavailable_extraction_separately_from_eligible_units(frozen):
    store, matter, run, _decision = frozen
    with fixture_write(store):
        original = store.connection.execute('SELECT * FROM workbench_source_catalog WHERE matter_id=?', (matter.matter_id,)).fetchone()
        columns = original.keys()
        values = [({'document_id': 'e' * 32, 'version_id': 'f' * 32, 'action_token': '9' * 32,
                    'source_state': 'failed', 'state_label': 'Synthetic OCR unavailable'}.get(key, original[key])) for key in columns]
        store.connection.execute('INSERT INTO workbench_source_catalog (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', values)
    store.fail_review_run(run.run_id, 'Synthetic terminal checkpoint')
    next_run = store.queue_review_run(matter.matter_id, ACTOR, run.criterion_version_id, run_kind='full', review_mode='full_text')
    ledger = FullTextReviewLedger(store)
    assert next_run.snapshot_count == 1
    coverage = ledger.coverage(matter.matter_id, ACTOR, next_run.run_id)
    assert coverage['sources'] == {'pending': 1, 'unavailable': 1}
    frozen_source = next(row for row in ledger.extraction_rows(matter.matter_id, ACTOR, next_run.run_id) if row['state'] == 'unavailable')
    assert frozen_source['extraction_note'] == 'Synthetic OCR unavailable'
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_source_catalog SET source_state='ready' WHERE document_id=?", ('e' * 32,))
    assert ledger.coverage(matter.matter_id, ACTOR, next_run.run_id)['sources']['unavailable'] == 1


def test_workbench_resume_skips_saved_ranges_and_source_change_clears_findings(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator, VerifiedReviewDecision
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.workflow_jobs import WorkflowFailure
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='test')
    with TestClient(app):
        bench = app.state.workbench
        bench.full_review.close(); bench.research.close()
        matter = bench.create_matter('Synthetic resume', '', ACTOR)
        source_store = bench.source_store(matter)
        document, _ = source_store.store_stream('resume.txt', 'text/plain', io.BytesIO(b'Synthetic resumable text.'))
        document.units = [asdict(PilotUnit(i, f'Synthetic unit {i}', excerpt_digest=hashlib.sha256(f'Synthetic unit {i}'.encode()).hexdigest())) for i in range(1, 5)]
        source_store._write_units(document); bench._sync_source_catalog(matter, [document])
        _criterion, version = bench.workspace.create_review_criterion(matter.matter_id, ACTOR, title='Synthetic units', instructions='Include synthetic units.')
        queued = bench.workspace.queue_review_run(matter.matter_id, ACTOR, version.criterion_version_id, run_kind='full', review_mode='full_text')
        run = bench.workspace.claim_review_run('synthetic-first-worker')
        decision = bench.workspace.next_review_decision(run.run_id)
        seen = []
        stopped = [False]
        def classify(**kwargs):
            text = kwargs['evidence'][0].excerpt; seen.append(text)
            if text == 'Synthetic unit 2':
                stopped[0] = True
            return VerifiedReviewDecision('include', text, ('S1',), True, 1)
        monkeypatch.setattr(bench.generator, 'classify_source', classify)
        with pytest.raises(WorkflowFailure, match='cancelled'):
            bench._process_review_decision(run, decision, lambda: stopped[0])
        ledger = FullTextReviewLedger(bench.workspace)
        assert ledger.coverage(matter.matter_id, ACTOR, run.run_id)['units'] == {'pending': 3, 'processed': 1}
        bench.workspace.fail_review_run(run.run_id, 'Review cancelled.')
        bench.workspace.retry_review_run(matter.matter_id, ACTOR, run.run_id)
        resumed = bench.workspace.claim_review_run('synthetic-second-worker')
        def replace_during_model(**kwargs):
            text = kwargs['evidence'][0].excerpt; seen.append(text)
            document.version_id = 'f' * 32
            bench._sync_source_catalog(matter, [document])
            return VerifiedReviewDecision('include', text, ('S1',), True, 1)
        monkeypatch.setattr(bench.generator, 'classify_source', replace_during_model)
        outcome = bench._process_review_decision(resumed, decision, lambda: False)
        assert seen == ['Synthetic unit 1', 'Synthetic unit 2', 'Synthetic unit 2']
        assert outcome.decision == 'needs_attention' and outcome.citations == ()
        assert ledger.coverage(matter.matter_id, ACTOR, run.run_id)['units'] == {'invalidated': 4}
        assert all(not row['rationale'] and not row['finding_key'] for row in ledger.rows(matter.matter_id, ACTOR, run.run_id))


@pytest.mark.parametrize('scope', ['actor', 'matter', 'instance'])
def test_admission_is_serialized_between_connections(frozen, monkeypatch, scope):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from case_intelligence import full_text_review_budget as budget
    store, matter, run, decision = frozen
    store.fail_review_run(run.run_id, 'Synthetic terminal checkpoint')
    monkeypatch.setattr(budget, 'DEFAULT_TEXT_ADMISSION', replace(budget.DEFAULT_TEXT_ADMISSION,
        actor_active=10, matter_active=10, instance_active=10,
        actor_calls=100_000,matter_calls=100_000,instance_calls=100_000,
        actor_ledger_bytes=1024**3,matter_ledger_bytes=1024**3,instance_ledger_bytes=1024**3,
        **{name+'_characters': 10_000_000 if name==scope else 100_000_000 for name in ('actor','matter','instance')}))
    others = [WorkspaceStore(store.path), WorkspaceStore(store.path)]
    barrier = Barrier(2)
    def queue(connection):
        barrier.wait()
        try:
            return connection.queue_review_run(matter.matter_id, ACTOR, run.criterion_version_id,
                run_kind='full', review_mode='full_text').run_id
        except WorkspaceProblem as exc:
            return str(exc)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(queue, others))
        assert sum(value.startswith('review-run-') for value in results) == 1
        assert any(scope + ' characters capacity' in value for value in results)
    finally:
        for connection in others:
            connection.close()


def test_frozen_policy_limits_inventory_and_calls_without_refunding_restart(frozen):
    from case_intelligence.full_text_review_budget import TextReviewLimit
    store, matter, run, decision = frozen
    policy = TextReviewPolicy(packet_chars=512, overlap_chars=0, max_calls=2, max_characters=600)
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_text_review SET policy_json=? WHERE run_id=?', (json.dumps(asdict(policy)),run.run_id))
        store.connection.execute('UPDATE workbench_text_review_budget SET max_calls=2,max_characters=600 WHERE run_id=?', (run.run_id,))
    ledger = FullTextReviewLedger(store)
    with pytest.raises(TextReviewLimit, match='character limit'):
        ledger.inventory(run, decision, [PilotUnit(1, 'x'*512), PilotUnit(2, 'x'*100)], current_source=lambda:True)
    assert ledger.coverage(matter.matter_id, ACTOR,run.run_id)['budget']['characters_used'] == 512
    assert not ledger.coverage(matter.matter_id, ACTOR,run.run_id)['inventory_complete']
    ledger.charge_call(run)
    store.fail_review_run(run.run_id, 'Synthetic interrupted dispatch')
    reopened = WorkspaceStore(store.path)
    try:
        reopened.retry_review_run(matter.matter_id, ACTOR, run.run_id)
        resumed = reopened.claim_review_run('synthetic-after-restart')
        ledger = FullTextReviewLedger(reopened)
        ledger.charge_call(resumed)
        with pytest.raises(TextReviewLimit, match='classifier-call limit'):
            ledger.charge_call(resumed)
        assert ledger.coverage(matter.matter_id, ACTOR,run.run_id)['budget']['calls_used'] == 2
    finally:
        reopened.close()


def test_non_pdf_frozen_review_keeps_pdf_caution_conditional(frozen):
    from case_intelligence.full_text_review import iter_text_export
    from case_intelligence.pdf_coverage import PDF_COVERAGE_NOTICE, PDF_SAVED_RESULT_NOTICE
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    ledger.inventory(run, decision, [PilotUnit(1, 'Synthetic text-only source.')], current_source=lambda: True)
    chunk = ledger.unit_chunks(run.run_id, decision.document_id, 1)[0]
    ledger.record(run, decision, chunk, state='processed', label='exclude',
                  rationale='Synthetic text checked.', current_source=lambda: True)
    before = tuple(store.connection.execute(
        'SELECT source_kind,source_version_id,source_basis_digest,extraction_note '
        'FROM workbench_text_review_source WHERE run_id=?', (run.run_id,)).fetchone())
    assert before[0] == 'TXT'
    coverage = ledger.coverage(matter.matter_id, ACTOR, run.run_id)
    assert not coverage['has_gaps'] and coverage['inventory_complete']
    assert PDF_COVERAGE_NOTICE not in coverage['notice']
    assert PDF_SAVED_RESULT_NOTICE in coverage['notice']
    assert 'If this saved result used PDFs,' in coverage['notice']
    for format_name in ('json', 'csv'):
        exported = b''.join(iter_text_export(store, matter.matter_id, ACTOR, run.run_id, format_name)).decode()
        assert PDF_COVERAGE_NOTICE not in exported
        assert PDF_SAVED_RESULT_NOTICE in exported
    after = tuple(store.connection.execute(
        'SELECT source_kind,source_version_id,source_basis_digest,extraction_note '
        'FROM workbench_text_review_source WHERE run_id=?', (run.run_id,)).fetchone())
    assert after == before


def test_compact_locator_utf8_charge_zero_units_and_constant_poll(frozen):
    from case_intelligence.full_text_review import compact_locator
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    ledger.inventory(run, decision, [], current_source=lambda:True)
    coverage = ledger.coverage(matter.matter_id,ACTOR,run.run_id)
    assert coverage['zero_unit_sources'] == 1 and coverage['has_gaps']
    assert coverage['inventory_complete']
    trace = []
    with store._lock:
        store.connection.set_trace_callback(trace.append)
    ledger.coverage(matter.matter_id,ACTOR,run.run_id)
    with store._lock:
        store.connection.set_trace_callback(None)
    assert not any('FROM workbench_text_review_unit ' in sql or 'FROM workbench_text_review_chunk ' in sql or 'FROM workbench_text_review_source ' in sql for sql in trace)
    text = 'Synthetic long text ' * 10000
    locator = compact_locator({'excerpt':text, 'source_name':'café🟠'}, 1, text)
    assert 'excerpt' not in locator and len(json.dumps(locator)) < 300
    before = coverage['budget']['ledger_bytes']
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_text_review_source SET source_name=source_name || '🟠' WHERE run_id=?",(run.run_id,))
    assert ledger.coverage(matter.matter_id,ACTOR,run.run_id)['budget']['ledger_bytes'] == before + 4


def test_terminal_delete_releases_retained_capacity_and_preserves_sources(frozen, monkeypatch):
    from case_intelligence import full_text_review_budget as budget
    store, matter, run, decision = frozen
    ledger = FullTextReviewLedger(store)
    with pytest.raises(WorkspaceProblem, match='Cancel'):
        ledger.delete(matter.matter_id,ACTOR,run.run_id)
    store.cancel_review_run(matter.matter_id,ACTOR,run.run_id)
    with pytest.raises(WorkspaceProblem, match='Cancel'):
        ledger.delete(matter.matter_id,ACTOR,run.run_id)
    store.fail_review_run(run.run_id,'Synthetic stopped worker')
    monkeypatch.setattr(budget,'DEFAULT_TEXT_ADMISSION',replace(budget.DEFAULT_TEXT_ADMISSION,actor_retained=1))
    with pytest.raises(WorkspaceProblem, match='retained capacity'):
        store.queue_review_run(matter.matter_id,ACTOR,run.criterion_version_id,run_kind='full',review_mode='full_text')
    with pytest.raises(KeyError):
        ledger.delete(matter.matter_id,'foreign-synthetic-actor',run.run_id)
    ledger.delete(matter.matter_id,ACTOR,run.run_id)
    assert store.connection.execute('SELECT count(*) FROM workbench_source_catalog').fetchone()[0] == 1
    for table in ('budget','counter','source','unit','chunk'):
        assert store.connection.execute('SELECT count(*) FROM workbench_text_review_'+table).fetchone()[0] == 0
    store.queue_review_run(matter.matter_id,ACTOR,run.criterion_version_id,run_kind='full',review_mode='full_text')


def test_legacy_backfill_is_concurrent_idempotent_and_preserves_malformed_evidence(frozen, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from case_intelligence import full_text_review
    from case_intelligence.full_text_review import iter_text_export
    store,matter,run,decision = frozen
    ledger = FullTextReviewLedger(store)
    ledger.inventory(run,decision,[PilotUnit(1,'Synthetic legacy text')],current_source=lambda:True)
    malformed = '{synthetic invalid locator'
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_text_review_unit SET citation_json=? WHERE run_id=?',(malformed,run.run_id))
        store.connection.execute('DELETE FROM workbench_text_review_budget WHERE run_id=?',(run.run_id,))
        store.connection.execute('DELETE FROM workbench_text_review_counter WHERE run_id=?',(run.run_id,))
    monkeypatch.setattr(full_text_review,'DEFAULT_TEXT_POLICY',TextReviewPolicy(max_ledger_bytes=4096))
    def reopen(_):
        connection=WorkspaceStore(store.path)
        try:
            return FullTextReviewLedger(connection).coverage(matter.matter_id,ACTOR,run.run_id)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(reopen,range(2)))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0]['budget']['legacy'] and outcomes[0]['budget']['ledger_bytes'] > 4096
    with pytest.raises(WorkspaceProblem,match='malformed'):
        ledger.citation(matter.matter_id,ACTOR,run.run_id,decision.document_id,1)
    export=json.loads(b''.join(iter_text_export(store,matter.matter_id,ACTOR,run.run_id)))
    assert next(row for row in export['records'] if row['record_type']=='unit')['citation_json']==malformed
    with pytest.raises(WorkspaceProblem,match='predates resource'):
        store.retry_review_run(matter.matter_id,ACTOR,run.run_id)
    assert not store.connection.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('adapter', ['ollama','openai'])
def test_frozen_output_limit_reaches_actual_adapter_request(tmp_path, monkeypatch, adapter):
    from fastapi.testclient import TestClient
    from case_intelligence import generation, full_text_review
    from case_intelligence.workbench import create_workbench_app
    model = 'synthetic-classifier'
    captured=[]
    def request(url,payload,**kwargs):
        captured.append(payload)
        raw=json.dumps({'decision':'not_identified','rationale':'No match.','evidence_ids':[]})
        return {'message':{'content':raw}} if adapter=='ollama' else {'choices':[{'message':{'content':raw}}]}
    monkeypatch.setattr(generation,'_bounded_json_request',request)
    monkeypatch.setattr(generation,'_bounded_json_get',lambda *a,**k:{'models':[{'name':model}], 'data':[{'id':model}]})
    client_type=generation.OllamaGenerator if adapter=='ollama' else generation.OpenAICompatibleGenerator
    app=create_workbench_app(tmp_path/'runtime',generator=client_type('http://127.0.0.1:8790',model),auth_mode='test')
    with TestClient(app):
        bench=app.state.workbench
        bench.full_review.close();bench.research.close()
        matter=bench.create_matter('Synthetic transport limit','',ACTOR)
        store=bench.source_store(matter)
        document,_=store.store_stream('limit.txt','text/plain',io.BytesIO(b'Synthetic passage without a match.'))
        bench._sync_source_catalog(matter,[document])
        _,version=bench.workspace.create_review_criterion(matter.matter_id,ACTOR,title='Bicycle',instructions='Find the amber bicycle.')
        queued=bench.workspace.queue_review_run(matter.matter_id,ACTOR,version.criterion_version_id,
            run_kind='full',review_mode='full_text',text_policy=TextReviewPolicy(output_tokens=321))
        monkeypatch.setattr(full_text_review,'DEFAULT_TEXT_POLICY',TextReviewPolicy(output_tokens=999))
        run=bench.workspace.claim_review_run('synthetic-output-worker')
        decision=bench.workspace.next_review_decision(run.run_id)
        result=bench._process_review_decision(run,decision,lambda:False)
        assert result.decision=='excluded'
        assert len(captured)==1
        assert (captured[0]['options']['num_predict'] if adapter=='ollama' else captured[0]['max_tokens'])==321
        assert FullTextReviewLedger(bench.workspace).coverage(matter.matter_id,ACTOR,run.run_id)['budget']['calls_used']==1


def test_storage_overflow_rolls_back_whole_unit_and_queue_failure_leaves_no_run(frozen):
    from case_intelligence.full_text_review_budget import TextReviewLimit
    store,matter,run,decision=frozen
    ledger=FullTextReviewLedger(store)
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_text_review_budget SET max_ledger_bytes=80000 WHERE run_id=?',(run.run_id,))
    with pytest.raises(TextReviewLimit,match='storage limit'):
        ledger.inventory(run,decision,[PilotUnit(1,'x'*100000)],current_source=lambda:True)
    assert ledger.coverage(matter.matter_id,ACTOR,run.run_id)['units']=={}
    assert ledger.coverage(matter.matter_id,ACTOR,run.run_id)['budget']['characters_used']==0
    store.fail_review_run(run.run_id,'Synthetic capacity reached')
    before=store.connection.execute('SELECT count(*) FROM workbench_review_run').fetchone()[0]
    with pytest.raises(TextReviewLimit,match='storage reservation'):
        store.queue_review_run(matter.matter_id,ACTOR,run.criterion_version_id,run_kind='full',review_mode='full_text',
            text_policy=TextReviewPolicy(max_ledger_bytes=4096))
    assert store.connection.execute('SELECT count(*) FROM workbench_review_run').fetchone()[0]==before


def test_restart_recovery_preserves_spend_fairness_and_control_storage_bound(frozen):
    store,matter,run,decision=frozen
    ledger=FullTextReviewLedger(store)
    ledger.charge_call(run)
    before=ledger.coverage(matter.matter_id,ACTOR,run.run_id)['budget']['ledger_bytes']
    assert store.recover_running_review_runs()==1
    recovered=store.review_run(matter.matter_id,ACTOR,run.run_id)
    assert recovered.state=='queued'
    assert recovered.last_claimed_at==run.last_claimed_at
    assert ledger.coverage(matter.matter_id,ACTOR,run.run_id)['budget']['calls_used']==1
    assert ledger.coverage(matter.matter_id,ACTOR,run.run_id)['budget']['ledger_bytes']==before+2048
    store.claim_review_run('synthetic-recovered-worker')
    with fixture_write(store):
        store.connection.execute('UPDATE workbench_text_review_budget SET max_ledger_bytes=ledger_bytes WHERE run_id=?',(run.run_id,))
    assert store.recover_running_review_runs()==1
    assert store.review_run(matter.matter_id,ACTOR,run.run_id).state=='failed'
    assert ledger.coverage(matter.matter_id,ACTOR,run.run_id)['budget']['limit_reason']


def test_unit_reader_bounds_malformed_and_oversized_values_without_truncation():
    from case_intelligence.unit_stream import UnitRecordLimit
    class CountingStream(io.StringIO):
        count = 0
        def read(self, amount=-1):
            self.count += 1
            return super().read(amount)
    malformed = CountingStream('{"version":1,"units":[{"text":"' + 'x' * 200_000)
    with pytest.raises(UnitRecordLimit,match='inventory remains unresolved'):
        list(iter_unit_records(malformed,max_record_chars=1_024))
    assert malformed.count == 1
    oversized = io.StringIO(json.dumps({'version':1,'units':[{'number':1,'text':'x'*2_000}]}))
    with pytest.raises(UnitRecordLimit):
        list(iter_unit_records(oversized,max_record_chars=1_024))
    # A large container of individually bounded records remains streamable.
    rows = [{'number':number,'text':'🟠'*30} for number in range(1,1_000)]
    assert list(iter_unit_records(io.StringIO(json.dumps({'version':1,'units':rows})),max_record_chars=512)) == rows
    allowed = [{'number':1,'text':'🟠'*1_000}]
    assert list(iter_unit_records(io.StringIO(json.dumps({'version':1,'units':allowed})),max_record_chars=12_100)) == allowed


def test_report_hydrator_streams_once_bounds_selected_excerpts_and_checks_uncited_text():
    from contextlib import nullcontext
    from case_intelligence.full_text_review import compact_locator, resolve_text_report_citations
    from case_intelligence.workbench import CaseIntelligenceWorkbench
    from case_intelligence.workflow_jobs import WorkflowFailure
    units = [PilotUnit(number,text,excerpt_digest=hashlib.sha256(text.encode()).hexdigest())
        for number,text in [(1,'Synthetic chosen passage'),(2,'Synthetic uncited passage')]]
    seen=[]
    def stream():
        seen.append('stream')
        yield from units
    matter=SimpleNamespace(matter_id='synthetic-matter')
    document=SimpleNamespace(document_id='a'*32,version_id='b'*32,state='ready',iter_parsed_units=stream)
    store=SimpleNamespace(get=lambda identifier:document,mutation_guard=nullcontext)
    def candidate(matter,document,unit,ordinal):
        return dict(matter_id=matter.matter_id,document_id=document.document_id,
            source_version_id=document.version_id,source_name='Synthetic source',location=unit.location,
            excerpt=unit.text,href='/matters/synthetic?support='+'c'*40,support_token='c'*40,
            excerpt_digest=unit.excerpt_digest,chunk_id=f'chunk-{ordinal}',unit_number=unit.number,
            line_start=unit.line_start,line_end=unit.line_end,evidence_kind='document')
    bench=SimpleNamespace(source_store=lambda matter:store,_candidate=candidate,
        _citation=lambda matter,value:value,_workflow_citation_payload=lambda value:dict(value))
    value=compact_locator(candidate(matter,document,units[0],1),1,units[0].text)
    basis={document.document_id:CaseIntelligenceWorkbench._document_content_basis(document)}
    versions={document.document_id:document.version_id}
    seen.clear()
    result=resolve_text_report_citations(bench,matter,[value],source_basis_digests=basis,source_versions=versions)
    assert result[0]['excerpt']==units[0].text and seen==['stream']
    seen.clear()
    assert resolve_text_report_citations(bench,matter,[],source_basis_digests=basis,source_versions=versions)==()
    assert seen==['stream']
    units[1]=replace(units[1],text='Tampered synthetic text retaining its old digest')
    with pytest.raises(WorkflowFailure,match='stale digest'):
        resolve_text_report_citations(bench,matter,[value],source_basis_digests=basis)
    with pytest.raises(WorkflowFailure,match='100 full-text'):
        resolve_text_report_citations(bench,matter,[dict(value,unit_ordinal=i) for i in range(1,102)])
    with pytest.raises(WorkflowFailure,match='repeat'):
        resolve_text_report_citations(bench,matter,[value,value])
    text='x'*6001
    units[:]=[PilotUnit(1,text,excerpt_digest=hashlib.sha256(text.encode()).hexdigest())]
    oversized=compact_locator(candidate(matter,document,units[0],1),1,text)
    with pytest.raises(WorkflowFailure,match='6,000-character'):
        resolve_text_report_citations(bench,matter,[oversized])
