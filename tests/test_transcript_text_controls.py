"""Synthetic legacy transcript export regressions; no media intake acceptance."""

from __future__ import annotations

from tests.fixture_write import fixture_write

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import csv
import hashlib
import io
import json
import wave
import zipfile
from dataclasses import asdict, replace

import pytest
from fastapi.testclient import TestClient

from case_intelligence.media_evidence import (
    export_transcript,
    transcript_summary_basis,
    transcript_units,
)
from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.workbench import create_workbench_app
from tests.test_work_product_exports import _records, _transcript_segments


ACTOR = "development-taylor-morgan"
RAW_TEXT = "\x1a=before\x1bafter & <literal> café a\u200db"
DISPLAY_TEXT = " =before after & <literal> café a\u200db"


@pytest.mark.parametrize("format_name", ["txt", "markdown", "srt", "vtt", "csv"])
def test_readable_transcript_exports_apply_policy_without_changing_segments(format_name):
    matter, *_ = _records()
    segments = (replace(_transcript_segments(matter)[0], current_text=RAW_TEXT),)
    basis = transcript_summary_basis(segments)
    body = export_transcript("Synthetic transcript.wav", segments, format_name).body
    text = body.decode("utf-8-sig")
    assert "\x1a" not in text and "\x1b" not in text
    assert "before after" in text and "a\u200db" in text
    if format_name == "markdown":
        assert "replaced with spaces for this export" in text
        assert "\\<literal\\>" in text and "<literal>" not in text
    if format_name == "csv":
        row = next(csv.DictReader(io.StringIO(text)))
        assert row["Text"] == "'" + DISPLAY_TEXT
    assert segments[0].current_text == RAW_TEXT
    assert transcript_summary_basis(segments) == basis
    assert json.loads(export_transcript("Synthetic transcript.wav", segments, "json").body)["segments"][0]["text"] == RAW_TEXT


def test_legacy_transcript_downloads_and_bundle_preserve_saved_basis(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic transcript exports", "descriptor": "Legacy export regression"}, follow_redirects=False)
        assert response.status_code == 303
        slug = response.headers["location"].split("/")[2]
        bench = app.state.workbench
        matter = bench.matter(slug, ACTOR)
        store = bench.source_store(matter)

        # Seed an already admitted synthetic source and normal transcript. This
        # deliberately does not exercise recording inspection or transcription.
        stream = io.BytesIO()
        with wave.open(stream, "wb") as recording:
            recording.setparams((1, 2, 8_000, 0, "NONE", "not compressed"))
            recording.writeframes(b"\x00\x00" * 8_000)
        original = stream.getvalue()
        document = PilotDocument(
            "a" * 32, "Synthetic transcript.wav", "a" * 32 + ".wav",
            "audio/wav", len(original), "ready", "", [],
            digest=hashlib.sha256(original).hexdigest(), version_id="b" * 32,
            duration_ms=1_000,
        )
        (store.files / document.stored_name).write_bytes(original)
        document._units_loader = store._load_units
        document._units_iterator = store._iter_units
        with store.mutation_guard():
            store.documents[document.document_id] = document
            store._save((document.document_id,))
        workspace = bench.workspace
        # Keep the real media worker from claiming the synthetic seed job.
        with fixture_write(workspace):
            job = workspace.queue_media_job(
                matter.matter_id, document.document_id, document.version_id, ACTOR,
                source_sha256=document.digest, byte_size=document.size,
                media_type=document.media_type, duration_ms=document.duration_ms,
            )
            claimed = workspace.claim_media_job("synthetic-export-fixture")
            assert claimed is not None and claimed.media_job_id == job.media_job_id
            workspace.import_media_transcript(
                job.media_job_id,
                segments=[{
                    "external_segment_id": "synthetic-segment-1", "start_ms": 0,
                    "end_ms": 1_000, "speaker_cluster": "SPEAKER_00",
                    "model_text": "Synthetic original transcript.",
                }], warnings=[], quality={}, provenance={},
            )
            workspace.finish_media_job(job.media_job_id, degraded=False, message="")
        keys = (matter.matter_id, document.document_id, document.version_id)
        segment = workspace.transcript_segments(*keys)[0]
        workspace.revise_transcript_segment(
            *keys, segment.segment_id, expected_revision=0,
            text="Synthetic reviewed transcript.", actor_id=ACTOR,
        )
        # Only the synthetic revision emulates legacy controls; today's import
        # and edit validators remain strict and are not bypassed in production.
        with fixture_write(workspace):
            workspace.connection.execute(
                "UPDATE workbench_transcript_segment_revision SET text=? WHERE segment_id=?",
                (RAW_TEXT, segment.segment_id),
            )
        segments = workspace.transcript_segments(*keys)
        units = transcript_units(segments)
        with store.mutation_guard():
            document.units = [asdict(unit) for unit in units]
            store._save((document.document_id,))
        before = (segments, workspace.media_transcript(*keys), transcript_summary_basis(segments), units)
        token = store.action_token(document)

        for format_name in ("markdown", "csv", "txt", "srt", "vtt", "json"):
            response = client.get(f"/matters/{slug}/sources/{token}/transcript-export", params={"format": format_name})
            assert response.status_code == 200
            assert response.headers[f"x-{LEGACY_STEM}-export"] == "work-product"
            if format_name == "json":
                assert response.json()["segments"][0]["text"] == RAW_TEXT
                continue
            text = response.content.decode("utf-8-sig")
            assert "\x1a" not in text and "\x1b" not in text
            assert "before after" in text and "a\u200db" in text
            if format_name == "csv":
                assert next(csv.DictReader(io.StringIO(text)))["Text"] == "'" + DISPLAY_TEXT
            if format_name == "markdown":
                assert "replaced with spaces for this export" in text

        response = client.get(f"/matters/{slug}/export")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            markdown = next(name for name in archive.namelist() if name.startswith("transcripts/") and name.endswith(".md"))
            text = archive.read(markdown).decode()
            assert "before after" in text and "replaced with spaces for this export" in text
            assert "\x1a" not in text and "\x1b" not in text
            structured = next(name for name in archive.namelist() if name.startswith("transcripts/") and name.endswith(".json"))
            assert json.loads(archive.read(structured))["segments"][0]["text"] == RAW_TEXT
        after = workspace.transcript_segments(*keys)
        assert (after, workspace.media_transcript(*keys), transcript_summary_basis(after), transcript_units(after)) == before
        assert document.parsed_units() == units
        assert store.source_path(document.document_id, verify_digest=True).read_bytes() == original


def test_transcript_markdown_retains_capacity_above_generic_document_limit():
    matter, *_ = _records()
    base = _transcript_segments(matter)[0]
    text = "a" * 19_999 + "\x1a"
    segments = tuple(replace(base, ordinal=index + 1, current_text=text) for index in range(501))
    artifact = export_transcript("Synthetic long transcript.wav", segments, "markdown")
    assert len(artifact.body) > 10_000_000
    assert b"\x1a" not in artifact.body
    assert artifact.body.count(("a" * 19_999 + " ").encode()) == len(segments)
    assert b"replaced with spaces for this export" in artifact.body
