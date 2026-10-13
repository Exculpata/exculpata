from tests.case_lifecycle_web_helpers import claim_bench_exit
"""Synthetic occurrence recall, explicit identity decisions and durable recovery."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from case_intelligence.entity_discovery import EntityDiscovery
from case_intelligence.entity_extractor import DeterministicEntityExtractor
from case_intelligence.entity_repository import EntityEditConflict
from case_intelligence.entity_service import EntityService
from case_intelligence.full_text_review import FullTextReviewLedger
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.workspace_store import WorkspaceStore, WorkspaceProblem
from tests.test_full_text_review import frozen, ACTOR
from tests.test_entity_workspace import setup


def discovery_fixture(frozen, texts, extractor=None):
    store, matter, run, decision = frozen
    FullTextReviewLedger(store).inventory(run, decision, [PilotUnit(i, text) for i, text in enumerate(texts, 1)], current_source=lambda: True)
    def load(unit):
        text = texts[unit['unit_ordinal'] - 1]
        digest = hashlib.sha256(text.encode()).hexdigest()
        return text, dict(document_id=decision.document_id, source_version_id=decision.source_version_id,
            source_name='Synthetic original.txt', location=f"Unit {unit['unit_ordinal']}", unit_number=unit['unit_ordinal'],
            chunk_id=f"chunk-{unit['unit_ordinal']}", excerpt_digest=digest, excerpt=text[:6000],
            support_token=hashlib.sha1(text.encode()).hexdigest())
    service = EntityService(store.entity_repository(), source_guard=nullcontext, resolve_support=lambda token: None,
        load_note=store.notebook_item, load_references=store.notebook_references,
        validate_references=lambda values: frozenset(range(len(values))))
    return EntityDiscovery(service, load_unit=load, extractor=extractor)


def test_bounded_recall_more_than_75_entities_and_no_false_merges(frozen):
    # Ground truth is declared independently of the extractor: 90 named people,
    # repeated accounts, an accented name, an OCR variant, organization, ID/date.
    texts = [f'Alex Example{i} arrived.' for i in range(90)]
    texts += ['Alex Example0 left. Alex Example0 returned.', 'José Álvarez arrived.',
              'J0sé Álvarez departed.', 'Amber Cooperative opened.', 'ID: ZX-204.', 'on 03/04/2026.']
    discovery = discovery_fixture(frozen, texts)
    store, matter, run, _ = frozen
    for _ in range(10):
        coverage = discovery.step(matter.matter_id, ACTOR, run.run_id)
    assert coverage['counts'] == dict(pending=0, processed=96, failed=0, invalidated=0)
    rows = list(store.connection.execute('SELECT * FROM workbench_entity_mention WHERE matter_id=?', (matter.matter_id,)))
    expected = [f'Alex Example{i}' for i in range(90)] + ['Alex Example0', 'Alex Example0', 'José Álvarez', 'J0sé Álvarez', 'Amber Cooperative', 'ZX-204', '03/04/2026']
    assert sorted(row['surface_text'] for row in rows) == sorted(expected)
    assert len({row['entity_id'] for row in rows}) == 97  # Zero automatic merges.
    assert all(row['review_status'] == 'suggested' for row in rows)
    for row in rows:
        original = texts[row['unit_number'] - 1]
        assert original[row['start_offset']:row['end_offset']] == row['surface_text']
    date = json.loads(next(row['date_json'] for row in rows if row['surface_text'] == '03/04/2026'))['date']
    assert date['normalized'] is None and date['timezone'] is None and 'unresolved' in date['ambiguity']
    assert len(discovery.service.list(matter.matter_id, ACTOR, page=2)[0]) == 47


def test_resume_failure_retry_and_reextraction_preserve_reviewer_corrections(frozen):
    class Failing(DeterministicEntityExtractor):
        def extract(self, text):
            if 'fail' in text:
                raise RuntimeError('Synthetic provider error must not enter stored note')
            return super().extract(text)
    texts = ['Alex Example arrived.', 'Jordan Sample failed.']
    discovery = discovery_fixture(frozen, texts, Failing())
    store, matter, run, _ = frozen
    coverage = discovery.step(matter.matter_id, ACTOR, run.run_id, limit=1)
    assert coverage['counts']['pending'] == 1
    entity = discovery.service.list(matter.matter_id, ACTOR)[0][0]
    discovery.service.update(matter.matter_id, ACTOR, entity['entity_id'], expected_revision=1,
        display_name='Reviewer corrected identity', status='confirmed')
    coverage = discovery.step(matter.matter_id, ACTOR, run.run_id)
    assert coverage['counts']['failed'] == 1
    assert 'provider error' not in json.dumps(coverage)
    discovery.extractor = DeterministicEntityExtractor()
    assert discovery.step(matter.matter_id, ACTOR, run.run_id, retry=True)['counts']['processed'] == 2
    discovery.extractor.version = 'synthetic-reextract-v2'
    assert discovery.step(matter.matter_id, ACTOR, run.run_id)['counts']['processed'] == 2
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 2
    assert discovery.service.detail(matter.matter_id, ACTOR, entity['entity_id'])[0]['display_name'] == 'Reviewer corrected identity'
    mentions = discovery.service.detail(matter.matter_id, ACTOR, entity['entity_id'])[1]
    discovery.service.remove_mention(matter.matter_id, ACTOR, entity['entity_id'], expected_revision=2, mention_id=mentions[0]['mention_id'])
    discovery.extractor.version = 'synthetic-reextract-v3'
    discovery.step(matter.matter_id, ACTOR, run.run_id)
    assert discovery.service.detail(matter.matter_id, ACTOR, entity['entity_id'])[1] == []
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 2


def test_source_replacement_is_invalidated_without_partial_mentions(frozen):
    texts = ['Alex Example arrived.']
    discovery = discovery_fixture(frozen, texts)
    store, matter, run, _ = frozen
    texts[0] = 'Jordan Sample replaced this unit.'
    assert discovery.step(matter.matter_id, ACTOR, run.run_id)['counts']['invalidated'] == 1
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 0
    other = store.create_matter('Other synthetic scope', '', ACTOR)
    with pytest.raises(KeyError):
        discovery.step(other.matter_id, ACTOR, run.run_id)


def test_merge_same_unit_distinct_mentions_split_reject_alias_and_undo(frozen):
    discovery = discovery_fixture(frozen, ['Alex Example met Alex Example.'])
    store, matter, run, _ = frozen
    discovery.step(matter.matter_id, ACTOR, run.run_id)
    service = discovery.service
    first, second = service.list(matter.matter_id, ACTOR)[0]
    args = dict(target_id=second['entity_id'], target_revision=1, expected_revision=1)
    operation = service.reconcile(matter.matter_id, ACTOR, first['entity_id'], action='merge', **args)
    target = service.detail(matter.matter_id, ACTOR, second['entity_id'])
    assert len(target[1]) == 2 and len({row['mention_id'] for row in target[1]}) == 2
    assert len({row['start_offset'] for row in target[1]}) == 2
    service.undo(matter.matter_id, ACTOR, operation)
    assert len(service.detail(matter.matter_id, ACTOR, first['entity_id'])[1]) == 1
    assert len(service.detail(matter.matter_id, ACTOR, second['entity_id'])[1]) == 1
    with pytest.raises(EntityEditConflict):
        service.undo(matter.matter_id, ACTOR, operation)
    args.update(expected_revision=3, target_revision=3)
    rejection = service.reconcile(matter.matter_id, ACTOR, first['entity_id'], action='reject', **args)
    assert service.reconciliation_detail(matter.matter_id, ACTOR, first['entity_id'])[0] == []
    service.undo(matter.matter_id, ACTOR, rejection)
    assert service.reconciliation_detail(matter.matter_id, ACTOR, first['entity_id'])[0]
    args.update(expected_revision=5, target_revision=5)
    alias = service.reconcile(matter.matter_id, ACTOR, first['entity_id'], action='alias', **args)
    assert len(service.detail(matter.matter_id, ACTOR, second['entity_id'])[1]) == 1
    service.undo(matter.matter_id, ACTOR, alias)
    args.update(expected_revision=7, target_revision=7)
    mention = service.detail(matter.matter_id, ACTOR, first['entity_id'])[1][0]
    split = service.reconcile(matter.matter_id, ACTOR, first['entity_id'], action='split', mention_ids=[mention['mention_id']], **args)
    assert len(service.detail(matter.matter_id, ACTOR, second['entity_id'])[1]) == 2
    service.undo(matter.matter_id, ACTOR, split)
    assert len(service.detail(matter.matter_id, ACTOR, first['entity_id'])[1]) == 1


def test_stale_merge_and_undo_refuse_later_shared_edits(tmp_path):
    store, matter, service = setup(tmp_path)
    from tests.test_entity_workspace import ACTOR as actor
    first = service.create(matter.matter_id, actor, display_name='Alex Example', support='a' * 40)
    second = service.create(matter.matter_id, actor, display_name='Alex Example', support='a' * 40)
    args = dict(target_id=second['entity_id'], target_revision=1, expected_revision=1)
    operation = service.reconcile(matter.matter_id, actor, first['entity_id'], action='merge', **args)
    with pytest.raises(EntityEditConflict):
        service.reconcile(matter.matter_id, actor, first['entity_id'], action='merge', **args)
    service.update(matter.matter_id, actor, second['entity_id'], expected_revision=2, display_name='Later human correction')
    with pytest.raises(EntityEditConflict):
        service.undo(matter.matter_id, actor, operation)
    assert len(service.detail(matter.matter_id, actor, second['entity_id'])[1]) == 2
    store.close()


def test_extraction_backup_reopen_preserves_coverage_and_decisions(frozen, tmp_path):
    discovery = discovery_fixture(frozen, ['Alex Example arrived.', 'Jordan Sample left.'])
    store, matter, run, _ = frozen
    discovery.step(matter.matter_id, ACTOR, run.run_id, limit=1)
    expected = store.entity_repository().discovery_export(matter.matter_id)
    target = tmp_path / 'restored.sqlite'
    with sqlite3.connect(target) as backup:
        store.connection.backup(backup)
    restored = WorkspaceStore(target)
    assert restored.entity_repository().discovery_export(matter.matter_id) == expected
    assert restored.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not restored.connection.execute('PRAGMA foreign_key_check').fetchall()
    restored.close()
    root = Path(__file__).parents[1]
    migration = Path('migrations/sqlite/0033_entity_discovery.sql')
    assert (root / migration).read_bytes() == (root / 'src/case_intelligence' / migration).read_bytes()


def test_web_discovery_full_source_restore_export_and_purge(tmp_path, monkeypatch):
    import io
    import shutil
    import zipfile
    from fastapi.testclient import TestClient
    from case_intelligence.workbench import create_workbench_app
    from tests.test_matter_notebook import _create_matter, WEB_ACTOR
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    runtime = tmp_path / 'original-runtime'
    original = b'Alex Example met Alex Example. Amber Cooperative opened. ID: ZX-204.'
    with TestClient(create_workbench_app(runtime, auth_mode='test')) as client:
        slug = _create_matter(client)
        client.post(f'/matters/{slug}/uploads', files=[('files', ('Generated discovery.txt', original, 'text/plain'))])
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        bench.full_review.close()
        store = bench.workspace
        document = next(iter(bench.source_store(matter).documents.values()))
        # Freeze and inventory using slice-12's public store/ledger contracts.
        _, version = store.create_review_criterion(matter.matter_id, WEB_ACTOR, title='Synthetic entity review', instructions='Review synthetic named people.')
        run = store.queue_review_run(matter.matter_id, WEB_ACTOR, version.criterion_version_id, run_kind='full', review_mode='full_text')
        run = store.claim_review_run('synthetic-entity-discovery-worker')
        decision = store.next_review_decision(run.run_id)
        FullTextReviewLedger(store).inventory(run, decision, document.parsed_units(), current_source=lambda: True)
        store.fail_review_run(run.run_id, 'Synthetic inventory fixture finished; no generation requested.')
        path = f'/matters/{slug}/entities'
        page = client.get(path)
        assert page.status_code == 200 and 'Check readiness and discover suggestions' in page.text
        response = client.post(path + '/actions', data=dict(action='discover', run_id=run.run_id, return_to=f'/matters/{slug}?q=Alex'))
        assert response.status_code == 200 and '1 units processed' in response.text
        events = [event for event in store.audit_events(matter.matter_id) if event.action == 'entity.discovery_unit']
        assert len(events) == 1 and events[0].outcome == 'success'
        assert 'data-discovery-unit' in response.text
        assert response.url.path == f'/matters/{slug}/entity-discovery/{run.run_id}'
        coverage_page = client.get(f'/matters/{slug}/entity-discovery/{run.run_id}')
        assert coverage_page.status_code == 200 and coverage_page.text.count('data-discovery-unit') == 1
        svc = bench.entity_service(matter)
        identities, total = svc.list(matter.matter_id, WEB_ACTOR)
        assert total == 4
        first, second = [row for row in identities if row['display_name'] == 'Alex Example']
        page = client.get(path + '/' + first['entity_id'])
        assert 'Machine occurrence:' in page.text and 'Confirm alias link' in page.text
        response = client.post(path + '/actions', data=dict(action='merge', entity_id=first['entity_id'], expected_revision=1,
            target_id=second['entity_id'], target_revision=1))
        assert response.status_code == 200
        assert len(svc.detail(matter.matter_id, WEB_ACTOR, second['entity_id'])[1]) == 2
        token = svc.detail(matter.matter_id, WEB_ACTOR, second['entity_id'])[1][0]['support_token']
        assert client.get(f'/matters/{slug}?support={token}').status_code == 200
        expected = store.entity_repository().discovery_export(matter.matter_id)
        expected_entity = svc.detail(matter.matter_id, WEB_ACTOR, second['entity_id'])
        bundle = client.get(f'/matters/{slug}/export')
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            exported = json.loads(archive.read('entities/discovery.json'))
            assert exported == expected
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    shutil.copytree(runtime, backup)
    shutil.copytree(backup, restored)
    shutil.move(runtime, tmp_path / 'original-offline')
    shutil.move(backup, tmp_path / 'backup-offline')
    with TestClient(create_workbench_app(restored, auth_mode='test')) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        assert bench.workspace.entity_repository().discovery_export(matter.matter_id) == expected
        svc = bench.entity_service(matter)
        assert svc.detail(matter.matter_id, WEB_ACTOR, second['entity_id']) == expected_entity
        assert client.get(f'/matters/{slug}?support={token}').status_code == 200
        source_store = bench.source_store(matter)
        restored_document = source_store.get(document.document_id)
        response = client.get(f'/matters/{slug}/sources/{source_store.action_token(restored_document)}/content')
        assert response.status_code == 200 and response.content == original
        operation = expected['reconciliations'][0]['operation_id']
        svc.undo(matter.matter_id, WEB_ACTOR, operation)
        assert len(svc.detail(matter.matter_id, WEB_ACTOR, first['entity_id'])[1]) == 1
        prepared, lifecycle = claim_bench_exit(bench, matter)
        bench.execute_matter_purge(prepared, lifecycle)
        for table in ('entity', 'entity_mention', 'entity_history', 'entity_discovery_unit', 'entity_discovery_seen', 'entity_reconciliation'):
            assert bench.workspace.connection.execute('SELECT COUNT(*) FROM workbench_' + table + ' WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0


def test_labelled_multilingual_alias_and_explicit_timezone_without_identity_inference():
    extractor = DeterministicEntityExtractor()
    text = 'person: 张伟\nperson: ليلى حسن\nalias: J. Sample\nname: jean dupont\norganization: 株式会社サンプル\nobject: amber bicycle\non 2026-09-12T10:30+02:00'
    rows = extractor.extract(text)
    assert [row.label for row in rows] == ['张伟', 'ليلى حسن', 'J. Sample', 'jean dupont', '株式会社サンプル', 'amber bicycle', '2026-09-12T10:30+02:00']
    assert rows[-1].date['timezone'] == '+02:00' and rows[-1].date['normalized'] is None
    assert all(text[row.start:row.end] == row.label for row in rows)
    assert extractor.extract('the synthetic bicycle is blue. no named people are stated here.') == []


def test_processed_coverage_reports_later_source_change_and_mention_review_survives(frozen):
    discovery = discovery_fixture(frozen, ['Alex Example arrived.'])
    store, matter, run, _ = frozen
    discovery.step(matter.matter_id, ACTOR, run.run_id)
    entity = discovery.service.list(matter.matter_id, ACTOR)[0][0]
    mention = discovery.service.detail(matter.matter_id, ACTOR, entity['entity_id'])[1][0]
    discovery.service.review_mention(matter.matter_id, ACTOR, entity['entity_id'], expected_revision=1,
        mention_id=mention['mention_id'], status='disputed')
    discovery.extractor.version = 'synthetic-reextract-v2'
    discovery.step(matter.matter_id, ACTOR, run.run_id)
    assert discovery.service.detail(matter.matter_id, ACTOR, entity['entity_id'])[1][0]['review_status'] == 'disputed'
    with store.connection:
        store.connection.execute("UPDATE workbench_source_catalog SET version_id='synthetic-replacement' WHERE matter_id=?", (matter.matter_id,))
    assert discovery.coverage(matter.matter_id, ACTOR, run.run_id)['counts']['invalidated'] == 1


def test_two_connections_process_each_occurrence_once_and_deleted_work_stays_removed(frozen):
    from concurrent.futures import ThreadPoolExecutor
    discovery = discovery_fixture(frozen, ['Alex Example arrived.'])
    store, matter, run, _ = frozen
    second = WorkspaceStore(store.path)
    other_service = EntityService(second.entity_repository(), source_guard=nullcontext,
        resolve_support=discovery.service.resolve_support, load_note=second.notebook_item,
        load_references=second.notebook_references, validate_references=discovery.service.validate_references)
    other = EntityDiscovery(other_service, load_unit=discovery.load_unit)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker.step, matter.matter_id, ACTOR, run.run_id) for worker in (discovery, other)]
            assert all(future.result(timeout=10)['counts']['processed'] == 1 for future in futures)
        rows, total = discovery.service.list(matter.matter_id, ACTOR)
        assert total == 1
        discovery.service.delete(matter.matter_id, ACTOR, rows[0]['entity_id'], expected_revision=1)
        discovery.extractor.version = 'synthetic-later-version'
        discovery.step(matter.matter_id, ACTOR, run.run_id)
        assert discovery.service.list(matter.matter_id, ACTOR)[1] == 0
    finally:
        second.close()


def test_unsupported_extractor_result_fails_whole_unit_without_receipts(frozen):
    from case_intelligence.entity_extractor import EntityOccurrence
    class Unsupported(DeterministicEntityExtractor):
        def extract(self, text):
            return [EntityOccurrence(0, 12, 'Alex Example', 'person'), EntityOccurrence(20, 40, 'invented support', 'person')]
    discovery = discovery_fixture(frozen, ['Alex Example arrived.'], Unsupported())
    store, matter, run, _ = frozen
    assert discovery.step(matter.matter_id, ACTOR, run.run_id)['counts']['failed'] == 1
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 0
    assert store.entity_repository().discovery_export(matter.matter_id)['occurrence_tombstones'] == []


def test_sealed_units_are_pending_before_first_discovery_request(frozen):
    discovery = discovery_fixture(frozen, ['Alex Example arrived.', 'Jordan Sample left.'])
    store, matter, run, _ = frozen
    coverage = discovery.coverage(matter.matter_id, ACTOR, run.run_id)
    assert coverage['counts'] == dict(pending=2, processed=0, failed=0, invalidated=0)
    assert store.entity_repository().discovery_export(matter.matter_id)['coverage'] == []
    assert discovery.step(matter.matter_id, ACTOR, run.run_id, limit=1)['counts']['pending'] == 1


def test_budget_rejects_dense_unit_before_any_mentions_or_receipts(frozen):
    discovery = discovery_fixture(frozen, ['ID: X1\n' * 1000])
    store, matter, run, _ = frozen
    assert len(discovery.extractor.extract('ID: X1\n' * 1000)) == 1000
    before = store.entity_repository().discovery_storage_bytes(matter.matter_id)
    with pytest.raises(WorkspaceProblem, match='byte budget'):
        discovery.step(matter.matter_id, ACTOR, run.run_id)
    assert store.entity_repository().discovery_storage_bytes(matter.matter_id) == before
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 0
    assert discovery.coverage(matter.matter_id, ACTOR, run.run_id)['counts']['pending'] == 1
    exported = store.entity_repository().discovery_export(matter.matter_id)
    assert exported['coverage'] == exported['occurrence_tombstones'] == []


def test_aggregate_budget_survives_retries_versions_and_clean_restore(frozen, tmp_path):
    texts = ['Alex Example arrived.' + ' x' * 4000, 'Jordan Sample left.' + ' x' * 4000]
    discovery = discovery_fixture(frozen, texts)
    discovery.byte_limit = 70000
    store, matter, run, _ = frozen
    assert discovery.step(matter.matter_id, ACTOR, run.run_id, limit=1)['counts']['processed'] == 1
    before = store.entity_repository().discovery_storage_bytes(matter.matter_id)
    for _ in range(3):
        with pytest.raises(WorkspaceProblem, match='byte budget'):
            discovery.step(matter.matter_id, ACTOR, run.run_id)
        assert store.entity_repository().discovery_storage_bytes(matter.matter_id) == before
    target = tmp_path / 'budget-restore.sqlite'
    with sqlite3.connect(target) as backup:
        store.connection.backup(backup)
    restored = WorkspaceStore(target)
    try:
        assert restored.entity_repository().discovery_storage_bytes(matter.matter_id) == before
        service = EntityService(restored.entity_repository(), source_guard=nullcontext, resolve_support=lambda token: None,
            load_note=restored.notebook_item, load_references=restored.notebook_references,
            validate_references=discovery.service.validate_references)
        resumed = EntityDiscovery(service, load_unit=discovery.load_unit, byte_limit=70000)
        resumed.extractor.version = 'synthetic-new-version'
        # A new version can visit the already retained span, but cannot reset
        # the matter budget and persist another full-excerpt occurrence.
        with pytest.raises(WorkspaceProblem, match='byte budget'):
            resumed.step(matter.matter_id, ACTOR, run.run_id)
        assert service.list(matter.matter_id, ACTOR)[1] == 1
        assert resumed.coverage(matter.matter_id, ACTOR, run.run_id)['counts']['pending'] == 1
    finally:
        restored.close()


def test_explicit_identifier_wins_same_span_date_without_losing_receipt(frozen):
    text = 'ID: 2026-09-12'
    rows = DeterministicEntityExtractor().extract(text)
    assert len(rows) == 1 and rows[0].kind == 'identifier' and rows[0].label == '2026-09-12'
    discovery = discovery_fixture(frozen, [text])
    store, matter, run, _ = frozen
    discovery.step(matter.matter_id, ACTOR, run.run_id)
    entity = discovery.service.list(matter.matter_id, ACTOR)[0][0]
    assert entity['entity_type'] == 'identifier'
    discovery.extractor.version = 'synthetic-reextract-v2'
    discovery.step(matter.matter_id, ACTOR, run.run_id)
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 1


def test_replaceable_extractor_must_resolve_conflicting_same_span(frozen):
    from case_intelligence.entity_extractor import EntityOccurrence
    class Conflicting(DeterministicEntityExtractor):
        def extract(self, text):
            return [EntityOccurrence(0, 12, 'Alex Example', kind) for kind in ('person', 'identifier')]
    discovery = discovery_fixture(frozen, ['Alex Example arrived.'], Conflicting())
    store, matter, run, _ = frozen
    assert discovery.step(matter.matter_id, ACTOR, run.run_id)['counts']['failed'] == 1
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 0
    assert store.entity_repository().discovery_export(matter.matter_id)['occurrence_tombstones'] == []


def test_large_match_population_is_capped_before_object_materialization(monkeypatch):
    import case_intelligence.entity_extractor as module
    original = module.EntityOccurrence
    created = 0
    def counted(*args, **kwargs):
        nonlocal created
        created += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'EntityOccurrence', counted)
    with pytest.raises(ValueError, match='proposal limit'):
        module.DeterministicEntityExtractor().extract('ID: X1\n' * 1_000_000)
    assert created == 1000


def test_replacement_iterator_is_consumed_only_to_proposal_bound(frozen):
    from case_intelligence.entity_extractor import EntityOccurrence
    class Streaming(DeterministicEntityExtractor):
        consumed = 0
        def extract(self, text):
            for _ in range(1_000_000):
                self.consumed += 1
                yield EntityOccurrence(0, 12, 'Alex Example', 'person')
    extractor = Streaming()
    discovery = discovery_fixture(frozen, ['Alex Example arrived.'], extractor)
    store, matter, run, _ = frozen
    assert discovery.step(matter.matter_id, ACTOR, run.run_id)['counts']['failed'] == 1
    assert extractor.consumed == 1001
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 0


def test_terminal_review_delete_cascades_coverage_without_deleting_mentions(frozen):
    discovery = discovery_fixture(frozen, ['Alex Example arrived.'])
    store, matter, run, _ = frozen
    discovery.step(matter.matter_id, ACTOR, run.run_id)
    before = store.entity_repository().discovery_storage_bytes(matter.matter_id)
    store.fail_review_run(run.run_id, 'Synthetic fixture completed.')
    FullTextReviewLedger(store).delete(matter.matter_id, ACTOR, run.run_id)
    exported = store.entity_repository().discovery_export(matter.matter_id)
    assert exported['coverage'] == exported['sources'] == []
    assert len(exported['occurrence_tombstones']) == 1
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 1
    assert store.entity_repository().discovery_storage_bytes(matter.matter_id) < before
    assert not store.connection.execute('PRAGMA foreign_key_check').fetchall()


def test_retry_does_not_start_pending_discovery(frozen):
    class Failed(DeterministicEntityExtractor):
        def extract(self, text):
            raise ValueError('Synthetic failure')
    discovery = discovery_fixture(frozen, ['Alex Example arrived.', 'Jordan Sample left.'], Failed())
    _, matter, run, _ = frozen
    assert discovery.step(matter.matter_id, ACTOR, run.run_id, limit=1)['counts']['failed'] == 1
    discovery.extractor = DeterministicEntityExtractor()
    coverage = discovery.step(matter.matter_id, ACTOR, run.run_id, retry=True)
    assert coverage['counts'] == dict(processed=1, pending=1, failed=0, invalidated=0)
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 1


def test_unit_coverage_pagination_keeps_complete_aggregate_counts(frozen):
    discovery = discovery_fixture(frozen, [f'Alex Example{i} arrived.' for i in range(120)])
    _, matter, run, _ = frozen
    first = discovery.coverage(matter.matter_id, ACTOR, run.run_id)
    second = discovery.coverage(matter.matter_id, ACTOR, run.run_id, page=2)
    third = discovery.coverage(matter.matter_id, ACTOR, run.run_id, page=3)
    summary = discovery.coverage(matter.matter_id, ACTOR, run.run_id, limit=0)
    assert first['counts']['pending'] == second['counts']['pending'] == third['counts']['pending'] == 120
    assert [len(page['units']) for page in (first, second, third)] == [50, 50, 20]
    assert [row['unit_ordinal'] for page in (first, second, third) for row in page['units']] == list(range(1, 121))
    assert summary['units'] == summary['sources'] == [] and summary['unit_total'] == 120


def test_partial_budget_failure_reports_every_committed_unit(frozen):
    discovery = discovery_fixture(frozen, ['Alex Example arrived.' + ' x' * 4000, 'Jordan Sample left.' + ' x' * 4000])
    discovery.byte_limit = 70000
    _, matter, run, _ = frozen
    events = []
    with pytest.raises(WorkspaceProblem, match='byte budget'):
        discovery.step(matter.matter_id, ACTOR, run.run_id, on_committed=events.append)
    assert len(events) == 1 and events[0]['state'] == 'processed' and events[0]['count'] == 1
    assert discovery.service.list(matter.matter_id, ACTOR)[1] == 1


def test_application_batches_requested_ordinals_in_one_document_pass(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from case_intelligence.workbench import create_workbench_app
    from tests.test_matter_notebook import _create_matter, WEB_ACTOR
    with TestClient(create_workbench_app(tmp_path, auth_mode='test')) as client:
        slug = _create_matter(client)
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        class Document:
            state = 'ready'
            passes = reads = 0
            def iter_parsed_units(self):
                self.passes += 1
                for number in range(1, 1001):
                    self.reads += 1
                    yield PilotUnit(number, f'Synthetic unit {number}')
        document = Document()
        monkeypatch.setattr(bench.source_store(matter), 'get', lambda identifier: document)
        monkeypatch.setattr(bench, '_candidate', lambda matter, document, unit, ordinal: SimpleNamespace(
            document_id='synthetic-document', source_version_id='synthetic-version', source_name='Synthetic source',
            citation=f'Unit {ordinal}', chunk_id=f'chunk-{ordinal}', excerpt_digest='a'*64, text=unit.text))
        monkeypatch.setattr(bench, '_support_tokens', lambda candidate: frozenset({'a'*40}))
        requested = [dict(document_id='synthetic-document', unit_ordinal=ordinal) for ordinal in range(501, 511)]
        loaded = list(bench.entity_discovery(matter).load_units(requested))
        assert len(loaded) == 10 and document.passes == 1 and document.reads == 510
        assert [unit['unit_ordinal'] for unit, _ in loaded] == list(range(501, 511))


def test_streamed_offset_index_is_reused_and_invalidated_without_caching_text(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import case_intelligence.entity_unit_reader as module
    from dataclasses import asdict
    name = 'a' * 32 + '.json'
    path = tmp_path / name
    records = [asdict(PilotUnit(i, f'José Álvarez unit {i}')) for i in range(1, 201)]
    # Preserve both UTF-8 multi-byte characters and serialized CRLF whitespace.
    path.write_bytes(json.dumps(dict(version=1, units=records), ensure_ascii=False, indent=2).replace('\n', '\r\n').encode())
    source = SimpleNamespace(derived=tmp_path, _units_file_is_safe=lambda value: value == name)
    document = SimpleNamespace(units=[], units_file=name)
    calls = reads = 0
    original = module.iter_unit_records
    def tracked(*args, **kwargs):
        nonlocal calls, reads
        calls += 1
        for record in original(*args, **kwargs):
            reads += 1
            yield record
    monkeypatch.setattr(module, 'iter_unit_records', tracked)
    reader = module.EntityUnitReader()
    first = list(reader.iter_selected(source, document, range(151, 161)))
    second = list(reader.iter_selected(source, document, range(191, 201)))
    assert calls == 1 and reads == 200
    assert first[0][1].text == 'José Álvarez unit 151' and second[-1][1].number == 200
    assert all(isinstance(position, int) for offsets in reader._indexes.values() for pair in offsets for position in pair)
    records[199] = asdict(PilotUnit(200, 'Replaced synthetic original unit.'))
    path.write_text(json.dumps(dict(version=1, units=records), ensure_ascii=False))
    assert list(reader.iter_selected(source, document, [200]))[0][1].text == 'Replaced synthetic original unit.'
    assert calls == 2 and reads == 400


def test_entity_export_remains_one_snapshot_when_reconciliation_follows_commit(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from tests.test_entity_workspace import ACTOR
    store, matter, service = setup(tmp_path)
    source = service.create(matter.matter_id, ACTOR, display_name='Alex Example')
    target = service.create(matter.matter_id, ACTOR, display_name='Alex Example')
    original = service.repository.transaction
    inject = True
    @contextmanager
    def reconcile_after_snapshot(*args):
        nonlocal inject
        with original(*args) as repo:
            yield repo
        if inject:
            inject = False
            service.reconcile(matter.matter_id, ACTOR, source['entity_id'], expected_revision=1,
                target_id=target['entity_id'], target_revision=1, action='alias')
    monkeypatch.setattr(service.repository, 'transaction', reconcile_after_snapshot)
    exported = service.export(matter.matter_id, ACTOR, source['entity_id'])
    assert exported['entity']['revision'] == 1
    assert exported['history'][0]['revision'] == 1
    assert exported['reconciliations'] == []
    current = service.export(matter.matter_id, ACTOR, source['entity_id'])
    assert current['entity']['revision'] == 2 and len(current['reconciliations']) == 1
    store.close()


def test_candidate_scoring_is_paged_and_runs_outside_writer_transaction(tmp_path, monkeypatch):
    import difflib
    from tests.test_entity_workspace import ACTOR
    store, matter, service = setup(tmp_path)
    source = service.create(matter.matter_id, ACTOR, display_name='Alex Example',
        aliases='\n'.join(f'Source label {i}' for i in range(30)))
    for index in range(120):
        service.create(matter.matter_id, ACTOR, display_name=f'Alex Example {index:03d}',
            aliases='\n'.join(f'Other label {index} {i}' for i in range(30)))
    matcher = difflib.SequenceMatcher
    calls = 0
    def counted(*args, **kwargs):
        nonlocal calls
        assert not store.connection.in_transaction
        calls += 1
        return matcher(*args, **kwargs)
    monkeypatch.setattr(difflib, 'SequenceMatcher', counted)
    seen = set()
    for page, count in [(1,50),(2,50),(3,20)]:
        before = calls
        candidates, _, more = service.reconciliation_detail(matter.matter_id, ACTOR, source['entity_id'], page=page)
        assert len(candidates) == count and calls - before <= 50
        assert more == (page < 3)
        ids = {row['entity_id'] for row in candidates}
        assert not seen & ids
        seen.update(ids)
    assert len(seen) == 120
    store.close()


@pytest.mark.parametrize('changed', ['source', 'target', 'mention'])
def test_reconciliation_conflict_retains_decision_inputs(tmp_path, changed):
    from fastapi.testclient import TestClient
    import re
    from html.parser import HTMLParser
    from case_intelligence.workbench import create_workbench_app
    from tests.test_matter_notebook import _create_matter, _reference, WEB_ACTOR
    with TestClient(create_workbench_app(tmp_path, auth_mode='test')) as client:
        slug = _create_matter(client)
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        service = bench.entity_service(matter)
        source = service.create(matter.matter_id, WEB_ACTOR, display_name='Alex Example')
        target = service.create(matter.matter_id, WEB_ACTOR, display_name='Jordan Sample')
        with service.repository.transaction(matter.matter_id, WEB_ACTOR) as repo:
            mention = repo.add_mention(matter.matter_id, WEB_ACTOR, source['entity_id'], _reference(), 'manual')
        if changed == 'mention':
            service.remove_mention(matter.matter_id, WEB_ACTOR, source['entity_id'], expected_revision=1, mention_id=mention['mention_id'])
        else:
            row = source if changed == 'source' else target
            service.update(matter.matter_id, WEB_ACTOR, row['entity_id'], expected_revision=1, display_name=row['display_name']+' corrected')
        response = client.post(f'/matters/{slug}/entities/actions', data=dict(action='split',
            entity_id=source['entity_id'], expected_revision=1, target_id=target['entity_id'],
            target_revision=1, mention_id=mention['mention_id'], return_to=f'/matters/{slug}?q=Alex'))
        assert response.status_code == 409
        form = re.search(r'<details open>.*?(<form.*?</form>)', response.text, re.S).group(1)
        class Fields(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values, self.select = {}, ''
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'input':
                    self.values[attrs.get('name')] = attrs.get('value')
                if tag == 'select':
                    self.select = attrs['name']
                if tag == 'option' and 'selected' in attrs:
                    self.values[self.select] = attrs['value']
        fields = Fields()
        fields.feed(form)
        assert fields.values['target_id'] == target['entity_id']
        assert fields.values['target_revision'] == '1'
        assert fields.values['expected_revision'] == '1'
        assert fields.values['action'] == 'split'
        assert fields.values['mention_id'] == mention['mention_id']
        assert 'Review your unsaved decision' in response.text
        assert service.reconciliation_detail(matter.matter_id, WEB_ACTOR, source['entity_id'])[1] == []
        exported = client.get(f"/matters/{slug}/entities/{source['entity_id']}/export")
        assert exported.status_code == 200 and exported.json() == service.export(matter.matter_id, WEB_ACTOR, source['entity_id'])
