"""Synthetic preservation coverage, privacy, capacity, and recovery regressions."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.matter_archive import (ArchiveProblem, build_matter_archive,
    cleanup_archive, matter_snapshot_digest)
from case_intelligence.matter_archive_schema import SCHEMA, validate_schema
from case_intelligence.matter_archive_verifier import verify_matter_archive
from case_intelligence.pilot_uploads import PilotDocument, PilotStore
from case_intelligence.workspace_store import WorkspaceStore

OWNER = 'principal-owner'
OTHER = 'principal-other'


@pytest.fixture
def archive_case(tmp_path):
    workspace = WorkspaceStore(tmp_path / 'workspace.sqlite')
    for ident in (OWNER, OTHER):
        workspace.upsert_principal('test', ident, 'Generated identity', 'generated', preferred_principal_id=ident)
    matter = workspace.create_matter('Generated preservation matter', 'Generated orientation', OWNER)
    def changed(change):
        if change.upserted:
            workspace.upsert_source_catalog(matter.matter_id, [dict(
                document_id=d.document_id, version_id=d.version_id, action_token=PilotStore.action_token(d),
                display_name=d.display_name, relative_path=d.relative_path or d.display_name,
                media_type=d.media_type, kind='TXT', source_state=d.state, tone='ready',
                state_label='Ready', count_label='Generated count', byte_size=d.size, source_sha256=d.digest,
                origin=d.origin) for d in change.upserted])
        if change.removed_document_ids:
            workspace.delete_source_catalog(matter.matter_id, change.removed_document_ids)
    store = PilotStore(tmp_path / 'sources', on_change=changed)
    policy = StoragePolicy(reserve_bytes=0)
    bench = SimpleNamespace(workspace=workspace, storage=SimpleNamespace(root=tmp_path), storage_policy=policy,
                            source_registry=None, archive_source_store=lambda matter: store)
    bench._storage_reservation_lock = threading.RLock()
    bench._preservation_reservations = {}
    bench._playback_reserved_bytes = lambda matter_id=None: sum(bench._preservation_reservations.values()) if matter_id is None else 0
    def ensure_capacity(incoming):
        if shutil.disk_usage(tmp_path).free - incoming < bench.storage_policy.reserve_bytes:
            raise RuntimeError('Generated capacity refusal')
    bench.storage.ensure_capacity = ensure_capacity
    yield bench, matter, store
    store.close()
    workspace.close()


def close(bench, matter):
    return bench.workspace.case_lifecycle.begin_exit(matter.slug, OWNER)


def records(artifact, table):
    with zipfile.ZipFile(artifact.path) as archive:
        return [json.loads(line) for line in archive.read('records/' + table + '.jsonl').splitlines()]


def test_archive_preserves_original_units_authored_text_and_all_tables(archive_case):
    bench, matter, store = archive_case
    raw = b'Generated original passage.\nPreserve every retained extracted unit.\n'
    document, _ = store.store_stream('generated.txt', 'text/plain', io.BytesIO(raw))
    report = bench.workspace.create_report(matter.matter_id, OWNER, 'Generated memo')
    # Authored text must not be pattern-redacted even when it resembles a path or credential.
    authored = 'Preserve these exact words: /generated/example and token=synthetic-authored-text.'
    bench.workspace.add_report_section(matter.matter_id, report.report_id, OWNER,
        expected_status='draft', heading='Human analysis', body=authored)
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        manifest = verify_matter_archive(artifact.path, expected_sha256=artifact.sha256)
        assert set(manifest['coverage']) == set(SCHEMA)
        assert artifact.snapshot_digest == matter_snapshot_digest(bench, matter, store)
        assert stat_mode(artifact.path) == 0o600
        assert stat_mode(artifact.cleanup_directory) == 0o700
        with zipfile.ZipFile(artifact.path) as archive:
            assert archive.read('originals/' + document.document_id) == raw
            units = [json.loads(line) for line in archive.read('extracted/' + document.document_id + '.jsonl').splitlines()]
            assert 'Generated original passage.' in units[0]['text']
            assert not any(name.endswith(('.sqlite', '.sqlite3')) for name in archive.namelist())
        assert records(artifact, 'workbench_report_section')[0]['body'] == authored
        assert records(artifact, 'workbench_report')[0]['created_by'].startswith('actor-')
        assert bench.workspace.case_lifecycle.current_exit(matter.matter_id).export_id is None
    finally:
        cleanup_archive(artifact)
    assert not artifact.cleanup_directory.exists()


def stat_mode(path):
    return path.stat().st_mode & 0o777


def test_cross_matter_and_operational_canaries_are_absent_but_failed_work_remains(archive_case):
    bench, matter, store = archive_case
    workspace = bench.workspace
    other = workspace.create_matter('FOREIGN-MATTER-CANARY', '', OTHER)
    workspace.create_report(other.matter_id, OTHER, 'FOREIGN-REPORT-CANARY')
    convo = workspace.create_conversation(matter.matter_id, 'Generated conversation')
    now = workspace._now()
    question = workspace.append_message(matter.matter_id, convo.conversation_id, 'user', 'Generated unfinished question')
    with workspace.connection:
        workspace.connection.execute('INSERT INTO workbench_answer_job '
            '(job_id,matter_id,conversation_id,actor_id,idempotency_key,question,question_message_id,state,stage,'
            'attempts,worker_id,cancellation_requested,message,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,?,\'failed\',\'failed\',1,?,0,?,?,?)',
            ('answer-job-'+'a'*32, matter.matter_id, convo.conversation_id, OWNER, 'REQUEST-CANARY',
             'Generated unfinished question', question.message_id, 'WORKER-CANARY', 'HOST-PATH-CANARY', now, now))
        workspace.connection.execute('INSERT INTO workbench_answer_event VALUES (?,?,?,?,?,?)',
            ('answer-job-'+'a'*32, 1, 'failed', 'failed', 'EVENT-HOST-CANARY', now))
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        jobs = records(artifact, 'workbench_answer_job')
        assert jobs[0]['question'] == 'Generated unfinished question'
        assert jobs[0]['state'] == 'failed'
        assert records(artifact, 'workbench_answer_event')[0]['state'] == 'failed'
        with zipfile.ZipFile(artifact.path) as archive:
            material = b'\n'.join(archive.read(name) for name in archive.namelist())
        for forbidden in (b'FOREIGN-MATTER-CANARY', b'FOREIGN-REPORT-CANARY', b'REQUEST-CANARY',
                          b'WORKER-CANARY', b'HOST-PATH-CANARY', b'EVENT-HOST-CANARY', OWNER.encode()):
            assert forbidden not in material
        assert jobs[0]['job_id'].startswith('reference-')
    finally:
        cleanup_archive(artifact)


@pytest.mark.parametrize('change', ['table', 'column'])
def test_unknown_schema_blocks_instead_of_omitting_or_exporting(archive_case, change):
    bench, matter, store = archive_case
    close(bench, matter)
    with bench.workspace.connection:
        bench.workspace.connection.execute('CREATE TABLE future_records(matter_id,text)' if change == 'table' else
            'ALTER TABLE workbench_report ADD COLUMN future_secret TEXT')
    with pytest.raises(ArchiveProblem):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    assert not list((bench.storage.root / 'preservation-exports').iterdir())


@pytest.mark.parametrize('damage', ['missing', 'changed', 'symlink', 'hardlink', 'quarantine', 'incoming', 'orphan_original', 'orphan_derived'])
def test_unsafe_missing_or_changed_material_blocks_archive(archive_case, damage):
    bench, matter, store = archive_case
    document, _ = store.store_stream('generated.txt', 'text/plain', io.BytesIO(b'Generated original'))
    path = store.files / document.stored_name
    close(bench, matter)
    if damage == 'missing':
        path.unlink()
    elif damage == 'changed':
        path.write_bytes(b'Changed bytes here')
    elif damage == 'symlink':
        path.unlink()
        path.symlink_to(bench.storage.root / 'workspace.sqlite')
    elif damage == 'hardlink':
        os.link(path, bench.storage.root / 'second-link')
    elif damage == 'quarantine':
        (store.quarantine_root / 'generated.blocked').write_bytes(b'Generated unsafe bytes')
    elif damage == 'incoming':
        (store.incoming / 'generated.part').write_bytes(b'Generated incomplete upload')
    else:
        ((store.files if damage == 'orphan_original' else store.derived) / 'generated-orphan').write_bytes(b'Generated orphan bytes')
    with pytest.raises(ArchiveProblem):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    assert not list((bench.storage.root / 'preservation-exports').iterdir())


def test_capacity_failure_cleans_partial_archive(archive_case, monkeypatch):
    bench, matter, store = archive_case
    store.store_stream('generated.txt', 'text/plain', io.BytesIO(b'Generated original'))
    close(bench, matter)
    monkeypatch.setattr('case_intelligence.matter_archive.shutil.disk_usage',
                        lambda path: SimpleNamespace(free=0))
    bench.storage_policy = replace(bench.storage_policy, reserve_bytes=1)
    with pytest.raises(ArchiveProblem, match='storage'):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    assert not list((bench.storage.root / 'preservation-exports').iterdir())


def test_interruption_cleans_partial_archive_and_can_retry(archive_case, monkeypatch):
    from case_intelligence import matter_archive
    bench, matter, store = archive_case
    store.store_stream('generated.txt', 'text/plain', io.BytesIO(b'Generated original'))
    close(bench, matter)
    real = matter_archive._consume
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(matter_archive, '_consume', interrupted)
    with pytest.raises(KeyboardInterrupt):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    assert not list((bench.storage.root / 'preservation-exports').iterdir())
    monkeypatch.setattr(matter_archive, '_consume', real)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    cleanup_archive(artifact)


def test_snapshot_ignores_new_audit_events_but_detects_authored_edit(archive_case):
    bench, matter, store = archive_case
    report = bench.workspace.create_report(matter.matter_id, OWNER, 'Generated title')
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace._append_audit_event_locked(actor_principal_id=OWNER, session_id=None,
                matter_id=matter.matter_id, request_id='generated', action='work_product.export', outcome='success')
        assert artifact.snapshot_digest == matter_snapshot_digest(bench, matter, store)
        # The frozen database rejects ordinary edits; reopening invalidates the
        # old exit and permits a fresh changed-content snapshot.
        with pytest.raises(sqlite3.IntegrityError, match='frozen'):
            with bench.workspace.connection:
                bench.workspace.connection.execute('UPDATE workbench_report SET title=? WHERE report_id=?',
                                                    ('Disallowed edit', report.report_id))
        bench.workspace.case_lifecycle.cancel_exit(matter.slug, OWNER, artifact.exit_id)
        with bench.workspace.connection:
            bench.workspace.connection.execute('UPDATE workbench_report SET title=? WHERE report_id=?',
                                                ('Changed generated title', report.report_id))
        assert artifact.snapshot_digest != matter_snapshot_digest(bench, matter, store)
    finally:
        cleanup_archive(artifact)


def test_wrong_actor_and_active_matter_cannot_export(archive_case):
    bench, matter, store = archive_case
    with pytest.raises(ArchiveProblem, match='Close'):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    close(bench, matter)
    with pytest.raises(KeyError):
        build_matter_archive(bench, matter, OTHER)


def test_nested_operational_fields_are_removed_and_unknown_fields_block(archive_case):
    bench, matter, store = archive_case
    conversation = bench.workspace.create_conversation(matter.matter_id, 'Generated context')
    message = bench.workspace.append_message(matter.matter_id, conversation.conversation_id, 'assistant',
        'Generated answer', dict(claims=[dict(text='Exact authored claim', citations=[dict(
            document_id='a'*32, source_version_id='b'*32, excerpt='Generated historical excerpt',
            job_id='NESTED-JOB-CANARY', session_id='NESTED-SESSION-CANARY',
            path='/generated/NESTED-PATH-CANARY', request_id='NESTED-REQUEST-CANARY',
            lease_token='NESTED-LEASE-CANARY')])]))
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        row = records(artifact, 'workbench_message')[0]
        assert row['payload_json']['claims'][0]['text'] == 'Exact authored claim'
        material = json.dumps(row)
        assert 'CANARY' not in material
        assert 'Generated historical excerpt' in material
        with zipfile.ZipFile(artifact.path) as archive:
            unavailable = [json.loads(line) for line in archive.read('unavailable-references.jsonl').splitlines()]
        assert unavailable[0]['availability'] == 'previously_removed'
    finally:
        cleanup_archive(artifact)
    bench.workspace.case_lifecycle.cancel_exit(matter.slug, OWNER, artifact.exit_id)
    with bench.workspace.connection:
        bench.workspace.connection.execute('UPDATE workbench_message SET payload_json=? WHERE message_id=?',
            (json.dumps(dict(future_unreviewed_field='UNKNOWN-CANARY')), message.message_id))
    close(bench, matter)
    with pytest.raises(ArchiveProblem):
        build_matter_archive(bench, matter, OWNER, source_store=store)


def test_source_registry_catalog_disagreement_blocks_certification(archive_case):
    bench, matter, store = archive_case
    store.store_stream('generated.txt', 'text/plain', io.BytesIO(b'Generated original'))
    with bench.workspace.connection:
        bench.workspace.connection.execute('DELETE FROM workbench_source_catalog WHERE matter_id=?', (matter.matter_id,))
    close(bench, matter)
    with pytest.raises(ArchiveProblem, match='inventory'):
        build_matter_archive(bench, matter, OWNER, source_store=store)


def test_media_original_machine_text_every_revision_speaker_history_and_clip_recipe(archive_case, tmp_path, monkeypatch):
    bench, matter, store = archive_case
    # Use repository synthetic WAV; original bytes are never generated by a model.
    wav = Path(__file__).parents[1] / 'src/case_intelligence/static/demo-audio.wav'
    # Construct an already retained media source: this unit test exercises
    # preservation, while codec probing has its own ingestion regressions.
    document = PilotDocument(document_id='d'*32, display_name='generated.wav', stored_name='e'*32,
        media_type='audio/wav', size=wav.stat().st_size, state='ready', message='', units=[],
        digest=hashlib.sha256(wav.read_bytes()).hexdigest(), version_id='f'*32, duration_ms=4000)
    (store.files / document.stored_name).write_bytes(wav.read_bytes())
    store.documents[document.document_id] = document
    store._save((document.document_id,))
    workspace = bench.workspace
    job = workspace.queue_media_job(matter.matter_id, document.document_id, document.version_id, OWNER,
        source_sha256=document.digest, byte_size=document.size, media_type='audio/wav', duration_ms=4000)
    workspace.claim_media_job('generated-worker')
    summary = natural_transcription_summary(tmp_path, monkeypatch, document.digest)
    transcript = workspace.import_media_transcript(job.media_job_id, segments=[dict(
        external_segment_id='generated-segment', start_ms=0, end_ms=4000, speaker_cluster='SPEAKER_00',
        speaker_display_name='Speaker 1', model_text='Generated machine text', confidence=.9)],
        warnings=[], quality=summary['quality'], provenance=summary['provenance'])
    segment = workspace.transcript_segments(matter.matter_id, document.document_id, document.version_id)[0]
    for revision, text in [(0, 'First human correction'), (1, 'Second human correction')]:
        workspace.revise_transcript_segment(matter.matter_id, document.document_id, document.version_id,
            segment.segment_id, expected_revision=revision, text=text, actor_id=OWNER)
    workspace.revise_speaker_mapping(matter.matter_id, document.document_id, document.version_id, 'SPEAKER_00',
        expected_revision=0, display_name='Generated Witness', identity_state='confirmed', actor_id=OWNER)
    workspace.create_media_clip(matter.matter_id, document.document_id, document.version_id,
        title='Generated clip', start_ms=1000, end_ms=3000, actor_id=OWNER)
    workspace.finish_media_job(job.media_job_id, degraded=False, message='Complete', warnings=[], quality={}, provenance={})
    workspace.queue_media_summary(matter.matter_id, document.document_id, document.version_id, OWNER)
    workspace.start_media_summary(transcript.transcript_id)
    workspace.fail_media_summary(transcript.transcript_id, 'Generated failed overview')
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        archived_transcript = records(artifact, 'workbench_media_transcript')[0]
        assert archived_transcript['quality_json'] == summary['quality']
        assert archived_transcript['provenance_json'] == summary['provenance']
        assert records(artifact, 'workbench_transcript_segment')[0]['model_text'] == 'Generated machine text'
        assert {r['text'] for r in records(artifact, 'workbench_transcript_segment_revision')} == {
            'First human correction', 'Second human correction'}
        assert len(records(artifact, 'workbench_speaker_mapping_revision')) == 2
        assert records(artifact, 'workbench_media_clip')[0]['start_ms'] == 1000
        with zipfile.ZipFile(artifact.path) as archive:
            assert archive.read('originals/' + document.document_id) == wav.read_bytes()
    finally:
        cleanup_archive(artifact)


def test_verifier_rejects_changed_bytes_duplicate_members_and_wrong_digest(archive_case, tmp_path):
    bench, matter, store = archive_case
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        with pytest.raises(ValueError, match='recorded digest'):
            verify_matter_archive(artifact.path, expected_sha256='0'*64)
        corrupt = tmp_path / 'corrupted.zip'
        with zipfile.ZipFile(artifact.path) as source, zipfile.ZipFile(corrupt, 'w') as out:
            for name in source.namelist():
                payload = source.read(name)
                if name == 'records/workbench_matter.jsonl':
                    payload = payload.replace(b'Generated', b'Altered__')
                out.writestr(name, payload)
        with pytest.raises(ValueError, match='integrity'):
            verify_matter_archive(corrupt)
        with zipfile.ZipFile(corrupt, 'a') as out:
            with pytest.warns(UserWarning):
                out.writestr('manifest.json', b'{}')
        with pytest.raises(ValueError, match='Duplicate'):
            verify_matter_archive(corrupt)
    finally:
        cleanup_archive(artifact)


def test_archive_contains_standalone_verifier_and_instructions(archive_case, tmp_path):
    import subprocess
    import sys
    bench, matter, store = archive_case
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            verifier = tmp_path / 'verify_archive.py'
            verifier.write_bytes(archive.read('verify_archive.py'))
            readme = archive.read('README.txt').decode()
            assert 'Python 3 standard library only' in readme
            assert 'unavailable-references.jsonl' in readme
        result = subprocess.run([sys.executable, '-I', str(verifier), str(artifact.path), '--sha256', artifact.sha256],
            cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0
        assert 'Verified' in result.stdout
    finally:
        cleanup_archive(artifact)


def test_registered_source_uses_safe_stager_and_verifies_digest(archive_case, tmp_path):
    from case_intelligence.source_locations import StagedSource
    bench, matter, store = archive_case
    raw = b'Generated registered original\n'
    document, _ = store.store_stream('generated.txt', 'text/plain', io.BytesIO(raw))
    document.origin = 'registered'
    document.source_location_id = 'generated-registered-source'
    document.relative_path = 'Generated collection/generated.txt'
    document.stable_device, document.stable_inode, document.stable_mtime_ns = 1, 2, 3
    (store.files / document.stored_name).unlink()
    document.stored_name = ''
    store._save((document.document_id,))
    called = []
    def stage(location, item, destination):
        called.append((location, item))
        assert sum(bench._preservation_reservations.values()) == len(raw)
        path = destination / 'generated-registered.part'
        path.write_bytes(raw)
        return StagedSource(path, hashlib.sha256(raw).hexdigest(), len(raw))
    bench.source_registry = SimpleNamespace(stage=stage)
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        assert bench._preservation_reservations == {}
        assert len(called) == 1
        assert called[0][0] == 'generated-registered-source'
        assert called[0][1].stable_inode == 2
        with zipfile.ZipFile(artifact.path) as archive:
            assert archive.read('originals/' + document.document_id) == raw
            assert b'generated-registered-source' not in archive.read('sources.jsonl')
    finally:
        cleanup_archive(artifact)
    def corrupt(location, item, destination):
        result = stage(location, item, destination)
        return StagedSource(result.path, '0'*64, result.byte_size)
    bench.source_registry = SimpleNamespace(stage=corrupt)
    with pytest.raises(ArchiveProblem, match='registered original changed'):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    assert not list((bench.storage.root / 'preservation-exports').iterdir())


def test_diskfull_during_zip_write_cleans_all_temporary_content(archive_case, monkeypatch):
    import errno
    from case_intelligence import matter_archive
    bench, matter, store = archive_case
    store.store_stream('generated.txt', 'text/plain', io.BytesIO(b'Generated original'))
    close(bench, matter)
    original = matter_archive._consume
    calls = []
    def full(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise OSError(errno.ENOSPC, 'Generated full volume')
        return original(*args, **kwargs)
    monkeypatch.setattr(matter_archive, '_consume', full)
    with pytest.raises(ArchiveProblem):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    assert not list((bench.storage.root / 'preservation-exports').iterdir())


def test_archive_streams_original_larger_than_legacy_bundle_limit(archive_case):
    import tracemalloc
    bench, matter, store = archive_case
    size = 101 * 1024 * 1024
    document = PilotDocument(document_id='1'*32, display_name='generated-large.txt', stored_name='2'*32,
        media_type='text/plain', size=size, state='failed', message='Generated extraction limit', units=[],
        version_id='3'*32)
    path = store.files / document.stored_name
    with path.open('wb') as stream:
        stream.truncate(size)  # synthetic sparse original; extraction was unsuccessful
    with path.open('rb') as stream:
        document.digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    store.documents[document.document_id] = document
    store._save((document.document_id,))
    close(bench, matter)
    tracemalloc.start()
    try:
        artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
        try:
            _, peak = tracemalloc.get_traced_memory()
            assert peak < 32 * 1024 * 1024
            assert artifact.path.stat().st_size > 100 * 1024 * 1024
            assert artifact.manifest['entries']['originals/' + document.document_id]['bytes'] == size
        finally:
            cleanup_archive(artifact)
    finally:
        tracemalloc.stop()


def natural_transcription_summary(tmp_path, monkeypatch, source_digest):
    # Exercise the real connector serializer with synthetic pipeline results;
    # this does not run ASR or claim a model-quality evaluation.
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'services/transcription/src'))
    from transcription_v2.worker import JobWorker
    from transcription_v2.pipeline import PipelineResult, PipelineStatus
    from transcription_v2.media import MediaProbe
    from transcription_v2.model_manifest import ModelReadiness, ArtifactReadiness, ASRBinding, MODEL_MANIFEST_SCHEMA_V2
    worker = JobWorker.__new__(JobWorker)
    output = tmp_path / 'generated-connector'
    output.mkdir()
    worker.storage = SimpleNamespace(job_paths=lambda identifier: SimpleNamespace(output=output))
    worker.model_readiness = ModelReadiness(MODEL_MANIFEST_SCHEMA_V2, 'a'*64,
        (ArtifactReadiness('asr', 'generated/model', 'b'*40, 'Generated license', 1, 256),), 1, 256,
        (ASRBinding('primary', 'generated/model', 'b'*40, 'faster-whisper', True),))
    job = SimpleNamespace(id='generated-media-job', profile='generated-profile')
    result = PipelineResult(job.id, PipelineStatus.SUCCEEDED, {},
        {'asr_model': 'generated/model', 'alignment_backend': 'generated-aligner'},
        source_language='en', source_language_confidence=.95,
        segments=[{'confidence': .9, 'overlap': False}],
        diarization={'requested': True, 'status': 'completed', 'speaker_count': 1, 'model': 'generated/diarizer'},
        provenance={'pipeline_version': 'generated-v1', 'engine': {'mock': True}})
    bundle = SimpleNamespace(manifest_path=Path('generated-manifest.json'), zip_path=None, archive_sha256=None)
    worker._write_ui_summary(job, source_digest, MediaProbe(4., 1, 1, 16000, 'wav', False, 256000).to_dict(), result, bundle)
    return json.loads((output / 'ui-summary.json').read_text())


def test_actual_recording_check_payload_and_private_inspection_id(archive_case, tmp_path):
    from case_intelligence.media_preflight import inspect_recording
    bench, matter, store = archive_case
    document, _ = store.store_stream('generated.txt', 'text/plain', io.BytesIO(b'Generated unavailable recording'))
    workspace = bench.workspace
    job = workspace.queue_media_job(matter.matter_id, document.document_id, document.version_id, OWNER,
        source_sha256=document.digest, byte_size=document.size, media_type='audio/wav', duration_ms=4000)
    workspace.claim_media_job('generated-worker')
    # The real bounded inspector produces the complete failed-check schema for
    # invalid synthetic media on every host, including hosts without ffmpeg.
    result = inspect_recording(store.files / document.stored_name, 'audio/wav')
    saved = workspace.save_media_preflight(job.media_job_id, result, hold=True, message='Generated failed check')
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        archived = records(artifact, 'workbench_media_job')[0]
        assert archived['state'] == 'cancelled'
        assert archived['quality_json']['preflight'] == {**result, 'continued': False}
        assert saved.preflight['inspection_id'] not in json.dumps(archived)
    finally:
        cleanup_archive(artifact)


def test_nested_private_metadata_excluded_and_model_paths_blocked():
    from case_intelligence.matter_archive_projection import portable
    private = ('provider', 'provider_subject', 'login_name', 'auth_method', 'application_roles', 'next_path',
               'created_by_name', 'updated_by_name', 'request_id', 'session_id', 'lease_token', 'worker_id')
    assert portable({key: 'PRIVATE-GENERATED-CANARY' for key in private}, 'generated-matter') == {}
    for key in ('model', 'model_name', 'model_id', 'asr_model', 'alignment_model', 'aligner_model', 'diarization_model'):
        with pytest.raises(ValueError, match='runtime location'):
            portable({key: '/generated/private/model'}, 'generated-matter')
    with pytest.raises(ValueError, match='unreviewed'):
        portable({'preflight': {'unexpected': 'PRIVATE-GENERATED-CANARY'}}, 'generated-matter')


def test_notebook_and_report_origin_links_are_navigable(archive_case):
    bench, matter, store = archive_case
    workspace = bench.workspace
    convo = workspace.create_conversation(matter.matter_id, 'Generated discussion')
    message = workspace.append_message(matter.matter_id, convo.conversation_id, 'assistant', 'Generated response')
    note, _ = workspace.create_notebook_item(matter.matter_id, OWNER, item_type='note', status='confirmed',
        title='Generated note', body='Generated note body', source_conversation_id=convo.conversation_id,
        source_message_id=message.message_id)
    report = workspace.create_report_from_sections(matter.matter_id, OWNER, 'Generated report', '',
        origin_id=message.message_id, sections=[{'heading': 'Generated section', 'body': 'Generated body'}])
    close(bench, matter)
    artifact = build_matter_archive(bench, matter, OWNER, source_store=store)
    try:
        archived_note = records(artifact, 'workbench_notebook_item')[0]
        assert archived_note['source_conversation_id'] in {row['conversation_id'] for row in records(artifact, 'workbench_conversation')}
        assert archived_note['source_message_id'] == records(artifact, 'workbench_message')[0]['message_id']
        assert records(artifact, 'workbench_report_section')[0]['origin_id'] == archived_note['source_message_id']
    finally:
        cleanup_archive(artifact)


def test_process_death_leaves_only_matter_owned_build_for_cleanup(archive_case):
    import subprocess
    import sys
    from case_intelligence.case_lifecycle_support import cleanup_archives
    bench, matter, _ = archive_case
    script = """import os,sys
from pathlib import Path
from types import SimpleNamespace
from case_intelligence.matter_archive import _temporary
bench = SimpleNamespace(storage=SimpleNamespace(root=Path(sys.argv[1])))
with _temporary(bench, sys.argv[2]) as directory:
    (directory / 'partial.jsonl').write_text('Generated retained text')
    os._exit(72)
"""
    result = subprocess.run([sys.executable, '-c', script, str(bench.storage.root), matter.matter_id])
    assert result.returncode == 72
    parent = bench.storage.root / 'preservation-exports' / matter.matter_id
    assert len(list(parent.glob('building-*'))) == 1
    bench._validate_clip_export_matter_id = lambda value: value
    cleanup_archives(bench, matter.matter_id)
    assert not parent.exists()


def test_archive_respects_other_workers_reserved_capacity(archive_case, monkeypatch):
    bench, matter, store = archive_case
    close(bench, matter)
    bench._preservation_reservations['generated-other-build'] = 2048
    monkeypatch.setattr('case_intelligence.matter_archive.shutil.disk_usage', lambda path: SimpleNamespace(free=1024))
    with pytest.raises(ArchiveProblem, match='reserve'):
        build_matter_archive(bench, matter, OWNER, source_store=store)
    assert not (bench.storage.root / 'preservation-exports' / matter.matter_id).exists()
    assert bench._preservation_reservations == {'generated-other-build': 2048}


def test_archive_format_snapshot_rules_match_standalone_verifier():
    from case_intelligence.matter_archive_schema import VOLATILE_TABLES
    from case_intelligence.matter_archive_verifier import VOLATILE_TABLES as FROZEN_RULES
    assert FROZEN_RULES == VOLATILE_TABLES
