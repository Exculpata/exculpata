"""Synthetic witness decisions survive certified export and scoped deletion."""

import hashlib
import io
import sqlite3
import zipfile
from contextlib import nullcontext

import pytest

from case_intelligence.assertion_service import AssertionService
from case_intelligence.entity_service import EntityService, REFERENCE_FIELDS
from case_intelligence.matter_archive import build_matter_archive, cleanup_archive, matter_snapshot_digest
from case_intelligence.matter_archive_projection import reference
from case_intelligence.matter_archive_verifier import verify_matter_archive
from case_intelligence.witness_inconsistency import WitnessInconsistencyService
from tests.case_lifecycle_helpers import claim_synthetic_exit
from tests.fixture_write import fixture_write
from tests.test_matter_archive import OWNER, OTHER, archive_case, close, records


def seed_candidates(workspace, matter, store, actor):
    refs = {}
    for index, time in enumerate(('09:15', '10:45', '11:30')):
        text = f'Avery Example stated the handoff occurred at {time} beside the north gate.'
        document, _ = store.store_stream(f'generated-{index}.txt', 'text/plain', io.BytesIO(text.encode()))
        token = hashlib.sha1(text.encode()).hexdigest()
        refs[token] = dict(document_id=document.document_id, source_version_id=document.version_id,
            source_name=document.display_name, location='Lines 1-1', unit_number=1, chunk_id='chunk-1',
            excerpt_digest=hashlib.sha256(text.encode()).hexdigest(), excerpt=text, support_token=token)
    entities = EntityService(workspace.entity_repository(), source_guard=nullcontext,
        resolve_support=refs.__getitem__, load_note=workspace.notebook_item,
        load_references=workspace.notebook_references,
        validate_references=lambda values: frozenset(i for i, value in enumerate(values)
            if value['support_token'] in refs and all(refs[value['support_token']][key] == value[key]
                for key in REFERENCE_FIELDS)))
    assertions = AssertionService(workspace.assertion_repository(), entities)
    witness = entities.create(matter.matter_id, actor, display_name='Avery Example')
    tokens = list(refs)
    event = assertions.create(matter.matter_id, actor, title='Generated handoff',
        statement='Generated accounts of a handoff.', record_type='event', support=tokens[0],
        attributed_to='Avery Example',
        roles=[dict(entity_id=witness['entity_id'], expected_revision=1, role='witness')])
    for revision, token in enumerate(tokens[1:], start=1):
        assertions.attach(matter.matter_id, actor, event['assertion_id'], expected_revision=revision,
            support=token, attributed_to='Avery Example')
    finder = WitnessInconsistencyService(assertions.repository, entities)
    candidates = finder.find(matter.matter_id, actor)
    assert len(candidates) == 3
    finder.mark_reviewed(matter.matter_id, actor, candidates[0].candidate_id)
    finder.dismiss(matter.matter_id, actor, candidates[1].candidate_id)
    return finder, candidates


def test_close_archive_preserves_witness_results_and_purge_is_matter_scoped(archive_case):
    bench, matter, store = archive_case
    workspace = bench.workspace
    finder, candidates = seed_candidates(workspace, matter, store, OWNER)
    other = workspace.create_matter('Generated other matter', '', OTHER)
    foreign_id = 'witness-inconsistency-' + 'f' * 64
    finder.mark_reviewed(other.matter_id, OTHER, foreign_id)
    table = 'workbench_witness_inconsistency_review'
    retained = [dict(row) for row in workspace.connection.execute(
        f'SELECT * FROM {table} WHERE matter_id=? ORDER BY candidate_id', (matter.matter_id,))]

    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        manifest = verify_matter_archive(artifact.path, expected_sha256=artifact.sha256)
        assert manifest['coverage'][table]['disposition'] == 'included'
        assert manifest['table_rows'][table] == 2
        assert records(artifact, table) == [
            {**row, 'actor_id': reference(matter.matter_id, OWNER, actor=True)} for row in retained]
        accounts = {row['account_id']: row for row in records(artifact, 'workbench_assertion_account')}
        for candidate in candidates:
            for passage in (candidate.first, candidate.second):
                account = accounts[passage.account_id]
                assert account['excerpt'] == passage.excerpt
                assert account['document_id'] == passage.document_id
                assert account['source_version_id'] == passage.source_version_id
                with zipfile.ZipFile(artifact.path) as archive:
                    assert archive.read('originals/' + passage.document_id).decode() == passage.excerpt
        with zipfile.ZipFile(artifact.path) as archive:
            assert foreign_id.encode() not in archive.read(f'records/{table}.jsonl')
        assert artifact.snapshot_digest == matter_snapshot_digest(bench, matter, store)
        with pytest.raises(sqlite3.IntegrityError, match='frozen'):
            with fixture_write(workspace):
                workspace.connection.execute(f"UPDATE {table} SET state='dismissed' WHERE matter_id=?",
                    (matter.matter_id,))
        workspace.case_lifecycle.register_export(matter.matter_id, OWNER, artifact.exit_id,
            artifact.export_id, artifact.sha256, artifact.snapshot_digest,
            session_id=bench.archive_test_session_id)
        _, lifecycle = claim_synthetic_exit(workspace, matter, OWNER)
        workspace.complete_matter_purge(matter.matter_id, lifecycle.purge_id)
        remaining = [dict(row) for row in workspace.connection.execute(f'SELECT * FROM {table}')]
        assert [(row['matter_id'], row['candidate_id']) for row in remaining] == [(other.matter_id, foreign_id)]
        assert records(artifact, table)[0]['candidate_id'] == retained[0]['candidate_id']
    finally:
        cleanup_archive(artifact)


def test_witness_review_decisions_are_snapshot_inputs(archive_case):
    bench, matter, store = archive_case
    finder, candidates = seed_candidates(bench.workspace, matter, store, OWNER)
    before = matter_snapshot_digest(bench, matter, store)
    finder.dismiss(matter.matter_id, OWNER, candidates[0].candidate_id)
    assert matter_snapshot_digest(bench, matter, store) != before
