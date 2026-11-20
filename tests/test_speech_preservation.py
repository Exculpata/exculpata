"""Portable evidence from a generated recording and synthetic speech services."""
import io
import json
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from case_intelligence import media_enrichment
from case_intelligence.external_model_grants import configured_model_destination
from case_intelligence.matter_archive import build_matter_archive, cleanup_archive
from case_intelligence.matter_archive_projection import portable
from case_intelligence.matter_archive_verifier import verify_matter_archive
from tests.test_matter_media_workflow import ACTOR, _matter
from tests.test_media_preflight import ObservedProcessor, app_for, silence, upload, wait_job
from tests.test_speech_endpoint import provider as provider


def test_verified_copy_preserves_reviewed_and_enriched_recording(tmp_path, monkeypatch, provider):
    source = "Prueba."
    translation = "A test."
    correction = "Una prueba."
    provider["response"] = json.dumps({"text": source, "language": "es", "segments": [
        {"start": 0.0, "end": 2.0, "text": source}]}).encode()
    replies = {
        "ALIGNMENT": {"words": [{"word": source, "start": 0.1, "end": 1.5}]},
        "TRANSLATION": {"text": translation},
        "DIARIZATION": {"speaker": "SPEAKER_01"},
    }
    for role in replies:
        monkeypatch.setenv(f"CASE_INTELLIGENCE_{role}_URL", "https://private-enrichment.example.test")
        monkeypatch.setenv(f"CASE_INTELLIGENCE_{role}_MODEL", f"synthetic/{role.lower()}")

    def respond(request, *, environment_prefix, timeout):
        row = replies[environment_prefix.removeprefix("CASE_INTELLIGENCE_")]
        return io.BytesIO(json.dumps({"segments": [{"id": "segment-1", **row}]}).encode())

    monkeypatch.setattr(media_enrichment, "open_generator_request", respond)
    wav = tmp_path / "synthetic.wav"
    silence(wav, 4)
    with TestClient(app_for(tmp_path / "runtime", ObservedProcessor())) as client:
        slug = _matter(client)
        bench, matter, document, token = upload(client, slug, wav)
        held = wait_job(bench, matter, document)
        expected_models = {}
        for capability in ("transcription", "alignment", "translation", "speaker_labeling"):
            destination = configured_model_destination(capability)
            bench.workspace.set_model_grant(matter.matter_id, destination, ACTOR, approved=True)
            expected_models[capability] = {"model": destination.model}
        route = f"/matters/{slug}/sources/{token}"
        action = client.post(route + "/recording-check", data={
            "action": "continue", "inspection_id": held.preflight["inspection_id"]}, follow_redirects=False)
        assert action.status_code == 303
        job = wait_job(bench, matter, document, ("succeeded", "degraded", "failed"))
        assert job.state == "succeeded", job.message
        [segment] = bench.workspace.transcript_segments(matter.matter_id, document.document_id, document.version_id)
        edited = client.post(route + "/segments/" + segment.segment_id,
                            data={"expected_revision": "0", "text": correction}, follow_redirects=False)
        assert edited.status_code == 303
        session = bench.workspace.create_session(ACTOR, "d"*64, "test",
                                                 "2099-01-01T00:00:00Z", "2099-01-02T00:00:00Z")
        bench.workspace.case_lifecycle.begin_exit(slug, ACTOR, session_id=session.session_id)
        artifact = build_matter_archive(bench, matter, ACTOR)
        saved = tmp_path / "downloaded.zip"
        try:
            shutil.copyfile(artifact.path, saved)
        finally:
            cleanup_archive(artifact)

    verify_matter_archive(saved, expected_sha256=artifact.sha256)
    with zipfile.ZipFile(saved) as archive:
        def record(table):
            return json.loads(archive.read("records/workbench_" + table + ".jsonl"))

        for table in ("media_job", "media_transcript"):
            row = record(table)
            assert row["provenance_json"] == expected_models
            for capability in ("alignment", "translation", "speaker_labeling"):
                assert row["quality_json"][capability + "_status"] == "completed"
        retained = record("transcript_segment")
        assert (retained["model_text"], retained["translated_text"]) == (source, translation)
        assert (retained["start_ms"], retained["end_ms"], retained["speaker_cluster"]) == (100, 1500, "SPEAKER_01")
        assert record("transcript_segment_revision")["text"] == correction
        assert archive.read("originals/" + document.document_id) == wav.read_bytes()
        for name in archive.namelist():
            data = archive.read(name)
            for canary in (b"speech.example.test", b"private-enrichment.example.test", b"synthetic-test-token"):
                assert canary not in data


def test_only_reviewed_speech_metadata_is_portable():
    private = {"endpoint": "https://private.example.test", "headers": {"Authorization": "secret"},
               "api_key": "secret", "credentials": "secret", "model": "vendor/model:v2"}
    assert portable({"translation": private}, "matter") == {"translation": {"model": "vendor/model:v2"}}
    assert portable({"source_alignment_status": "degraded", "alignment_status": "completed_locally",
                     "speaker_labeling_status": "denied", "translation_status": "skipped"}, "matter")
    for payload in ({"translation": {"model": ["model"]}}, {"alignment": {"model": "/private/path"}},
                    {"transcription": {"model": "https://private.example.test/model"}},
                    {"alignment": {"model": "../private"}}, {"translation": {"model": "model?token=secret"}},
                    {"speaker_labeling": {"model": "model", "text": "unknown field"}},
                    {"alignment_status": "https://private.example.test"}, {"source_alignment_status": ["failed"]}):
        with pytest.raises(ValueError):
            portable(payload, "matter")
