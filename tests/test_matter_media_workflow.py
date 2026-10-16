from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import io
import json
import re
import subprocess
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Mapping

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.matter_archive_verifier import verify_matter_archive
from case_intelligence.media_evidence import (
    MediaProcessorError,
    MediaProcessorNotFound,
    export_transcript,
    transcript_summary_basis,
    transcript_summary_windows,
)
from case_intelligence.workbench import create_workbench_app
from tests.case_lifecycle_web_helpers import prepare_preview_exit, preview_login
from case_intelligence.workspace_store import (
    MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS,
    WorkspaceProblem,
)


ROOT = Path(__file__).parents[1]
WAV = ROOT / "src/case_intelligence/static/demo-audio.wav"
PDF = ROOT / "src/case_intelligence/demo_data/synthetic_case_report.pdf"
ACTOR = "development-taylor-morgan"


class EvidenceEchoGenerator:
    available = True

    def generate(self, **kwargs):
        evidence = kwargs["evidence"]
        if not evidence:
            return {
                "answerable": False,
                "claims": [],
                "limitation": None,
                "missing_information": "No support was found.",
            }
        return {
            "answerable": True,
            "claims": [
                {
                    "text": (
                        "The machine transcript appears to say that "
                        + evidence[0].excerpt
                        if evidence[0].evidence_kind == "transcript"
                        else evidence[0].excerpt
                    ),
                    "evidence_ids": [evidence[0].evidence_id],
                }
            ],
            "limitation": None,
            "missing_information": "",
        }


class DualModalAnswerGenerator(EvidenceEchoGenerator):
    """Return independently cited written and spoken claims when both are supplied."""

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        evidence = kwargs["evidence"]
        if "orientation overview" in kwargs["question"].casefold():
            return super().generate(**kwargs)
        written = next(
            (item for item in evidence if item.evidence_kind == "document"), None
        )
        spoken = next(
            (item for item in evidence if item.evidence_kind == "transcript"), None
        )
        if written is None or spoken is None:
            return super().generate(**kwargs)
        return {
            "answerable": True,
            "claims": [
                {"text": written.excerpt, "evidence_ids": [written.evidence_id]},
                {
                    "text": "The machine transcript appears to say that " + spoken.excerpt,
                    "evidence_ids": [spoken.evidence_id],
                },
            ],
            "limitation": None,
            "missing_information": "",
        }


class BlockingSummaryGenerator(EvidenceEchoGenerator):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, **kwargs):
        if "orientation overview" in kwargs["question"]:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("generated summary test was not released")
        return super().generate(**kwargs)


class RejectingCaptureGenerator:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if "orientation overview" in kwargs["question"]:
            evidence = kwargs["evidence"]
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": (
                            "The machine transcript appears to say that "
                            + evidence[0].excerpt
                        ),
                        "evidence_ids": [evidence[0].evidence_id],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }
        return {
            "answerable": True,
            "claims": [
                {
                    "text": "A helicopter supplied an unsupported account at 11:45 p.m.",
                    "evidence_ids": ["S1"],
                }
            ],
            "limitation": None,
            "missing_information": "",
        }


class RepairingCaptureGenerator:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        evidence = kwargs["evidence"]
        if "orientation overview" in kwargs["question"]:
            return {
                "answerable": True,
                "claims": [
                    {
                        "text": (
                            "The machine transcript appears to say that "
                            + evidence[0].excerpt
                        ),
                        "evidence_ids": [evidence[0].evidence_id],
                    }
                ],
                "limitation": None,
                "missing_information": "",
            }
        text = (
            "The machine transcript appears to say that " + evidence[1].excerpt
            if kwargs["grounding_repair"]
            else "A bystander describes an unidentified object beside the bicycle."
        )
        return {
            "answerable": True,
            "claims": [{"text": text, "evidence_ids": [evidence[1].evidence_id]}],
            "limitation": None,
            "missing_information": "",
        }


class ImmediateMediaProcessor:
    available = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.jobs: dict[tuple[str, str], dict[str, object]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.submissions = 0

    def ready(self, owner: str) -> bool:
        return True

    def find_job(
        self, owner: str, source_sha256: str, byte_size: int
    ) -> Mapping[str, object] | None:
        with self._lock:
            matches = [
                value
                for (candidate_owner, _), value in self.jobs.items()
                if candidate_owner == owner
                and value["source_sha256"] == source_sha256
                and value["size_bytes"] == byte_size
            ]
        return matches[0] if matches else None

    def submit(
        self, owner: str, source: Path, media_type: str, source_sha256: str
    ) -> Mapping[str, object]:
        assert source.is_file()
        assert media_type == "audio/wav"
        external = f"job_{self.submissions + 1:032x}"
        view = {
            "id": external,
            "job_id": external,
            "status": "review_ready",
            "stage": {
                "code": "exporting",
                "label": "Exporting",
                "status": "succeeded",
                "progress": 1.0,
            },
            "source_sha256": source_sha256,
            "size_bytes": source.stat().st_size,
            "degraded": False,
            "user_message": None,
        }
        with self._lock:
            self.submissions += 1
            self.jobs[(owner, external)] = view
        return view

    def job(self, owner: str, external_job_id: str) -> Mapping[str, object]:
        with self._lock:
            return dict(self.jobs[(owner, external_job_id)])

    def transcript(self, owner: str, external_job_id: str) -> Mapping[str, object]:
        assert (owner, external_job_id) in self.jobs
        return {
            "job_id": external_job_id,
            "revision": 3,
            "review_state": "Machine draft",
            "segments": [
                {
                    "id": "processor-segment-1",
                    "segment_id": "processor-segment-1",
                    "start": 0.0,
                    "end": 2.0,
                    "text": "The red bicycle was logged at the north entrance.",
                    "model_text": "The red bicycle was logged at the north entrance.",
                    "translated_text": None,
                    "confidence": 0.97,
                    "low_confidence": False,
                    "overlap": False,
                    "speaker": {
                        "cluster_id": "SPEAKER_00",
                        "display_name": "SPEAKER_00",
                        "identity_state": "cluster",
                    },
                },
                {
                    "id": "processor-segment-2",
                    "segment_id": "processor-segment-2",
                    "start": 2.0,
                    "end": 4.0,
                    "text": "Officer Lane collected the property receipt.",
                    "model_text": "Officer Lane collected the property receipt.",
                    "translated_text": None,
                    "confidence": 0.62,
                    "low_confidence": True,
                    "overlap": True,
                    "speaker": {
                        "cluster_id": "SPEAKER_01",
                        "display_name": "SPEAKER_01",
                        "identity_state": "cluster",
                    },
                },
                {
                    "id": "processor-segment-3",
                    "segment_id": "processor-segment-3",
                    "start": 4.0,
                    "end": 7.8,
                    "text": "The interview ended after the inventory was confirmed.",
                    "model_text": "The interview ended after the inventory was confirmed.",
                    "translated_text": None,
                    "confidence": 0.91,
                    "low_confidence": False,
                    "overlap": False,
                    "speaker": {
                        "cluster_id": "SPEAKER_00",
                        "display_name": "SPEAKER_00",
                        "identity_state": "cluster",
                    },
                },
            ],
            "speakers": [],
            "warnings": [],
            "quality": {"review_recommended": True},
            "provenance": {"pipeline": "synthetic-test-whisperx"},
            "media_url": "/temporary-link-that-must-not-be-used",
        }

    def delete(self, owner: str, external_job_id: str) -> None:
        with self._lock:
            self.deleted.append((owner, external_job_id))
            self.jobs.pop((owner, external_job_id), None)

    def cancel(self, owner: str, external_job_id: str) -> None:
        return None


class ConfigurableCleanupMediaProcessor(ImmediateMediaProcessor):
    """Expose only the three verified external-cleanup outcomes."""

    def __init__(self) -> None:
        super().__init__()
        self.cleanup_outcome = "success"
        self.cleanup_attempts: list[tuple[str, str, str]] = []

    def delete(self, owner: str, external_job_id: str) -> None:
        self.cleanup_attempts.append((owner, external_job_id, self.cleanup_outcome))
        if self.cleanup_outcome == "unavailable":
            raise MediaProcessorError("Synthetic processor cleanup is unavailable.")
        if self.cleanup_outcome == "not_found":
            raise MediaProcessorNotFound("Synthetic processor job is already absent.")
        super().delete(owner, external_job_id)


class RestartMediaProcessor(ImmediateMediaProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.release = False
        self.progress = 0.35

    def job(self, owner: str, external_job_id: str) -> Mapping[str, object]:
        view = dict(super().job(owner, external_job_id))
        if not self.release:
            view["status"] = "running"
            view["stage"] = {
                "code": "transcribing",
                "label": "Transcribing",
                "status": "running",
                "progress": self.progress,
            }
        return view


class OfflineMediaProcessor:
    available = False


def _incompatible_video(path: Path) -> Path:
    target = path / "synthetic-incompatible.mov"
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x240:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        check=True,
        timeout=15,
    )
    return target


def _browser_video(
    path: Path,
    *,
    name: str,
    audio_codec: str = "aac",
) -> Path:
    target = path / name
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=teal:s=320x240:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            audio_codec,
            "-shortest",
            str(target),
        ],
        check=True,
        timeout=15,
    )
    return target


def _stream_codecs(path: Path) -> tuple[str, str]:
    completed = subprocess.run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            "--",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    streams = json.loads(completed.stdout)["streams"]
    video = next(item["codec_name"] for item in streams if item["codec_type"] == "video")
    audio = next(item["codec_name"] for item in streams if item["codec_type"] == "audio")
    return video, audio


def _video_stream_digest(path: Path) -> str:
    completed = subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    return completed.stdout.decode("ascii").strip()


def _matter(client: TestClient, name: str = "Media evidence matter") -> str:
    response = client.post(
        "/matters",
        data={"name": name, "descriptor": "Generated media workflow test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("/")[2]


def _upload_and_wait(client: TestClient, slug: str) -> tuple[object, str]:
    uploaded = client.post(
        f"/matters/{slug}/uploads",
        files=[("files", ("Interview.wav", WAV.read_bytes(), "audio/wav"))],
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    bench = client.app.state.workbench
    matter = bench.matter(slug, ACTOR)
    document = next(iter(bench.source_store(matter).documents.values()))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = bench.workspace.media_job(
            matter.matter_id, document.document_id, document.version_id
        )
        if document.state == "ready" and job is not None and job.state in {"succeeded", "degraded"}:
            break
        time.sleep(0.01)
    assert document.state == "ready", document.message
    job = bench.workspace.media_job(
        matter.matter_id, document.document_id, document.version_id
    )
    assert job is not None and job.state in {"succeeded", "degraded"}
    return document, bench.source_store(matter).action_token(document)


def _wait_for_summary(bench, matter, document, *states: str):
    expected = set(states or ("ready",))
    deadline = time.monotonic() + 5
    summary = None
    while time.monotonic() < deadline:
        summary = bench.workspace.media_summary(
            matter.matter_id, document.document_id, document.version_id
        )
        if summary is not None and summary.state in expected:
            return summary
        time.sleep(0.01)
    raise AssertionError(
        f"transcript overview did not reach {sorted(expected)}; "
        f"last state was {summary.state if summary is not None else 'missing'}"
    )


def test_transcript_overview_runs_independently_after_transcript_is_ready(tmp_path):
    generator = BlockingSummaryGenerator()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=generator,
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    try:
        with TestClient(app) as client:
            slug = _matter(client, "Independent automatic overview matter")
            uploaded = client.post(
                f"/matters/{slug}/uploads",
                files=[("files", ("Interview.wav", WAV.read_bytes(), "audio/wav"))],
                follow_redirects=False,
            )
            assert uploaded.status_code == 303
            bench = client.app.state.workbench
            matter = bench.matter(slug, ACTOR)
            document = next(iter(bench.source_store(matter).documents.values()))
            assert generator.started.wait(timeout=5)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                media_job = bench.workspace.media_job(
                    matter.matter_id, document.document_id, document.version_id
                )
                summary = bench.workspace.media_summary(
                    matter.matter_id, document.document_id, document.version_id
                )
                if (
                    media_job is not None
                    and media_job.state in {"succeeded", "degraded"}
                    and summary is not None
                    and summary.state == "running"
                ):
                    break
                time.sleep(0.01)
            assert document.state == "ready"
            assert media_job is not None and media_job.state in {"succeeded", "degraded"}
            assert summary is not None and summary.state == "running"
            status = client.get(
                f"/matters/{slug}/sources/{bench.source_store(matter).action_token(document)}/media-status"
            )
            assert status.status_code == 200
            assert status.json()["summary_state"] == "running"

            generator.release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                summary = bench.workspace.media_summary(
                    matter.matter_id, document.document_id, document.version_id
                )
                if summary is not None and summary.state == "ready":
                    break
                time.sleep(0.01)
            assert summary is not None and summary.state == "ready"
    finally:
        generator.release.set()


def test_video_playback_survives_transcription_failure_with_browser_copy(tmp_path):
    source = _incompatible_video(tmp_path)
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=OfflineMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Independent playback matter")
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    ("Synthetic recording.mov", source.read_bytes(), "video/quicktime"),
                )
            ],
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        token = bench.source_store(matter).action_token(document)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = bench.workspace.media_job(
                matter.matter_id, document.document_id, document.version_id
            )
            if (
                job is not None
                and job.state == "failed"
                and document.playback_state == "ready"
            ):
                break
            time.sleep(0.02)
        assert job is not None and job.state == "failed"
        assert document.state == "failed"
        assert document.playback_state == "ready"
        assert document.playback_media_type == "video/mp4"
        assert document.playback_message == "Browser playback is ready after video conversion."
        compatible = bench.source_store(matter).derived / document.playback_name
        assert _stream_codecs(compatible) == ("h264", "aac")

        review = client.get(f"/matters/{slug}/sources/{token}")
        assert review.status_code == 200
        assert "The uploaded recording remains available for playback" in review.text
        assert "You can still play and review the uploaded recording" in review.text
        assert 'type="video/mp4"' in review.text

        head = client.head(f"/matters/{slug}/sources/{token}/content")
        assert head.status_code == 200
        assert head.headers["content-type"].startswith("video/mp4")
        assert head.headers[f"x-{LEGACY_STEM}-playback"] == "compatible-copy"
        assert int(head.headers["content-length"]) == document.playback_size
        prefix = client.get(
            f"/matters/{slug}/sources/{token}/content",
            headers={"Range": "bytes=0-31"},
        )
        assert prefix.status_code == 206
        assert b"ftyp" in prefix.content


def test_browser_ready_mp4_uses_the_original_without_conversion(tmp_path):
    source = _browser_video(tmp_path, name="synthetic-direct.mp4")
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=OfflineMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Direct video matter")
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[("files", ("Synthetic direct.mp4", source.read_bytes(), "video/mp4"))],
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        assert document.playback_state == "original"
        assert document.playback_name == ""
        assert document.playback_message == "The original video is ready without conversion."
        token = bench.source_store(matter).action_token(document)
        forced = client.post(
            f"/matters/{slug}/sources/{token}/playback/retry",
            follow_redirects=False,
        )
        assert forced.status_code == 303
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and document.playback_state != "ready":
            time.sleep(0.02)
        assert document.playback_state == "ready", document.playback_message
        assert document.playback_name
        assert document.playback_message == "Browser playback is ready after video conversion."


def test_h264_aac_mov_is_remuxed_without_reencoding_video(tmp_path):
    source = _browser_video(tmp_path, name="synthetic-remux.mov")
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=OfflineMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Remux video matter")
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[("files", ("Synthetic remux.mov", source.read_bytes(), "video/quicktime"))],
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and document.playback_state != "ready":
            time.sleep(0.02)
        assert document.playback_state == "ready", document.playback_message
        assert document.playback_message == "Browser playback is ready without re-encoding video."
        compatible = bench.source_store(matter).derived / document.playback_name
        assert _stream_codecs(compatible) == ("h264", "aac")
        assert _video_stream_digest(compatible) == _video_stream_digest(source)


def test_failed_remux_falls_back_to_full_video_conversion(tmp_path, monkeypatch):
    from case_intelligence import pilot_uploads

    source = _browser_video(tmp_path, name="synthetic-remux-fallback.mov")
    actual_command = pilot_uploads._browser_playback_command
    attempted_methods: list[str] = []

    def command(method, source_path, destination, maximum_output_bytes):
        attempted_methods.append(method)
        if method == "remux":
            return ["/usr/bin/false"]
        return actual_command(
            method,
            source_path,
            destination,
            maximum_output_bytes,
        )

    monkeypatch.setattr(pilot_uploads, "_browser_playback_command", command)
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=OfflineMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Remux fallback matter")
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "Synthetic remux fallback.mov",
                        source.read_bytes(),
                        "video/quicktime",
                    ),
                )
            ],
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and document.playback_state != "ready":
            time.sleep(0.02)
        assert document.playback_state == "ready", document.playback_message
        assert attempted_methods == ["remux", "full_transcode"]
        assert document.playback_message == (
            "Browser playback is ready after video conversion."
        )
        compatible = bench.source_store(matter).derived / document.playback_name
        assert _stream_codecs(compatible) == ("h264", "aac")


def test_h264_mov_with_pcm_converts_only_audio(tmp_path):
    source = _browser_video(
        tmp_path,
        name="synthetic-audio-only.mov",
        audio_codec="pcm_s16le",
    )
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=OfflineMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Audio-only conversion matter")
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    ("Synthetic audio only.mov", source.read_bytes(), "video/quicktime"),
                )
            ],
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and document.playback_state != "ready":
            time.sleep(0.02)
        assert document.playback_state == "ready", document.playback_message
        assert document.playback_message == (
            "Browser playback is ready; only the audio track was converted."
        )
        compatible = bench.source_store(matter).derived / document.playback_name
        assert _stream_codecs(compatible) == ("h264", "aac")
        assert _video_stream_digest(compatible) == _video_stream_digest(source)


def test_media_upload_transcript_review_range_citations_exports_and_clips(tmp_path):
    for migration_name in (
        "0011_matter_media.sql",
        "0012_matter_media_purge_repair.sql",
        "0013_media_transcript_summary.sql",
    ):
        assert (
            ROOT / "src/case_intelligence/migrations/sqlite" / migration_name
        ).read_bytes() == (ROOT / "migrations/sqlite" / migration_name).read_bytes()
    processor = ImmediateMediaProcessor()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="preview",
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        preview_login(client)
        slug = _matter(client)
        document, token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)

        assert document.has_video is False
        assert 7_900 <= document.duration_ms <= 8_100
        assert len(document.parsed_units()) == 3
        assert processor.submissions == 1
        assert len(processor.deleted) == 1
        assert processor.jobs == {}
        summary = _wait_for_summary(bench, matter, document, "ready")
        assert summary.attempts == 1
        assert summary.covered_segment_count == summary.total_segment_count == 3
        assert summary.payload["coverage"]["mode"] == "full"
        assert "this overview reports" in summary.payload["evidence_notice"]
        initial_overview = json.dumps(summary.payload)
        assert "Speaker 1" in initial_overview
        assert "Speaker 2" in initial_overview
        assert "SPEAKER_00" not in initial_overview
        assert "SPEAKER_01" not in initial_overview
        original_summary_basis = summary.basis_digest

        review = client.get(f"/matters/{slug}/sources/{token}")
        assert review.status_code == 200
        assert "Authenticated playback" in review.text
        assert "Synchronized transcript" in review.text
        assert "Speaker labels" in review.text
        assert "Review speakers" in review.text
        assert "Draft transcript" in review.text
        assert "Machine Draft" not in review.text
        assert "The immutable machine draft remains in the workbench" in review.text
        assert "is not included in exported files" in review.text
        assert "Machine text remains available in CSV and JSON" not in review.text
        assert 'data-open-speaker-review' in review.text
        assert 'data-speaker-review-target="SPEAKER_00"' in review.text
        assert "never identifies or confirms a person automatically" in review.text
        assert "temporary-link-that-must-not-be-used" not in review.text
        assert 'data-media-player' in review.text
        assert 'data-transcript-follow' in review.text
        assert "The red bicycle was logged" in review.text
        assert "AI transcript overview" in review.text
        assert "Transcript-based orientation" in review.text
        assert "Export overview to Word" in review.text
        assert "Speaker 1" in review.text
        assert "Speaker 2" in review.text
        summary_panel = review.text.split('id="media-summary"', 1)[1].split(
            "</section>", 1
        )[0]
        assert "SPEAKER_00" not in summary_panel
        assert "SPEAKER_01" not in summary_panel

        initial_overview_export = client.get(
            f"/matters/{slug}/sources/{token}/summary-export",
            params={"format": "markdown"},
        )
        assert initial_overview_export.status_code == 200
        assert b"Speaker 1" in initial_overview_export.content
        assert b"Speaker 2" in initial_overview_export.content
        assert b"SPEAKER_00" not in initial_overview_export.content
        assert b"SPEAKER_01" not in initial_overview_export.content

        head = client.head(f"/matters/{slug}/sources/{token}/content")
        assert head.status_code == 200
        assert head.headers["accept-ranges"] == "bytes"
        assert head.headers["cache-control"] == "no-store"
        assert int(head.headers["content-length"]) == WAV.stat().st_size
        partial = client.get(
            f"/matters/{slug}/sources/{token}/content",
            headers={"Range": "bytes=20-39"},
        )
        assert partial.status_code == 206
        assert partial.headers["content-range"] == f"bytes 20-39/{WAV.stat().st_size}"
        assert partial.content == WAV.read_bytes()[20:40]

        evidence = bench.search(matter, "red bicycle north entrance")
        assert evidence
        assert evidence[0].location == "00:00–00:02"
        assert evidence[0].href.startswith(f"/matters/{slug}?support=")
        assert "play=1" in evidence[0].href
        assert evidence[0].href.endswith("#support-pane")
        support = client.get(evidence[0].href)
        assert support.status_code == 200
        assert 'data-support-media' in support.text
        assert 'data-start-ms="0"' in support.text
        assert "Play cited moment" in support.text
        assert "Open full recording and transcript" in support.text
        assert f"/matters/{slug}/sources/{token}/content" in support.text

        conversation = bench.workspace.get_conversation(matter.matter_id)
        answer = client.post(
            f"/matters/{slug}/ask",
            data={
                "conversation": conversation.conversation_id,
                "question": "Where was the red bicycle logged?",
                "request_key": "answer-request-" + "a" * 32,
            },
            headers={"Accept": "application/json"},
        )
        assert answer.status_code == 202
        answer_job = answer.json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and answer_job["state"] not in {"succeeded", "failed"}:
            time.sleep(0.01)
            answer_job = client.get(answer_job["status_url"]).json()
        assert answer_job["state"] == "succeeded"
        answer_messages = bench.workspace.messages(
            matter.matter_id, conversation.conversation_id
        )
        generated = answer_messages[-1]
        citation = generated.payload["claims"][0]["citations"][0]
        assert citation["location"] == "00:00–00:02"
        assert citation["href"].startswith(f"/matters/{slug}?support=")
        assert citation["support_token"]
        assert "play=1" in citation["href"]
        rendered_answer = client.get(answer_job["result_url"])
        assert "Play cited moment" in rendered_answer.text
        assert f"support={citation['support_token']}" in rendered_answer.text
        assert f"conversation={conversation.conversation_id}" in rendered_answer.text

        segments = bench.workspace.transcript_segments(
            matter.matter_id, document.document_id, document.version_id
        )
        corrected = client.post(
            f"/matters/{slug}/sources/{token}/segments/{segments[0].segment_id}",
            data={
                "expected_revision": "0",
                "text": "The crimson bicycle was logged at the north entrance.",
            },
            follow_redirects=False,
        )
        assert corrected.status_code == 303
        updated = bench.workspace.transcript_segments(
            matter.matter_id, document.document_id, document.version_id
        )[0]
        assert updated.current_revision == 1
        assert updated.model_text.startswith("The red bicycle")
        assert updated.current_text.startswith("The crimson bicycle")
        assert document.parsed_units()[0].text.startswith("Speaker 1: The crimson")
        corrected_support = client.get(citation["href"])
        assert corrected_support.status_code == 200
        assert "The crimson bicycle" in corrected_support.text
        assert "current reviewed text" in corrected_support.text
        changing_summary = bench.workspace.media_summary(
            matter.matter_id, document.document_id, document.version_id
        )
        assert changing_summary is not None
        assert changing_summary.state in {"stale", "running", "ready"}

        stale = client.post(
            f"/matters/{slug}/sources/{token}/segments/{segments[0].segment_id}",
            data={"expected_revision": "0", "text": "stale edit"},
            follow_redirects=False,
        )
        assert stale.status_code == 303
        assert "error=" in stale.headers["location"]

        speakers = bench.workspace.transcript_speakers(
            matter.matter_id, document.document_id, document.version_id
        )
        speaker = next(item for item in speakers if item.speaker_cluster == "SPEAKER_00")
        labeled = client.post(
            f"/matters/{slug}/sources/{token}/speakers",
            data={
                "speaker_cluster": speaker.speaker_cluster,
                "expected_revision": str(speaker.revision),
                "display_name": "Witness Jordan",
                "identity_state": "confirmed",
            },
            follow_redirects=False,
        )
        assert labeled.status_code == 303
        assert document.parsed_units()[0].text.startswith("Witness Jordan:")
        reviewed_page = client.get(f"/matters/{slug}/sources/{token}")
        assert reviewed_page.status_code == 200
        assert "Reviewed transcript" in reviewed_page.text
        relabeled_support = client.get(citation["href"])
        assert relabeled_support.status_code == 200
        assert "Witness Jordan" in relabeled_support.text
        assert "The crimson bicycle" in relabeled_support.text

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            refreshed_summary = bench.workspace.media_summary(
                matter.matter_id, document.document_id, document.version_id
            )
            current_segments = bench.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            )
            expected_basis = transcript_summary_basis(current_segments)
            if (
                refreshed_summary is not None
                and refreshed_summary.state == "ready"
                and refreshed_summary.basis_digest == expected_basis
            ):
                break
            time.sleep(0.01)
        assert refreshed_summary is not None and refreshed_summary.state == "ready"
        assert refreshed_summary.attempts >= 2
        assert refreshed_summary.basis_digest != original_summary_basis

        for format_name, marker in (
            ("docx", b"PK"),
            ("markdown", b"# Transcript overview"),
        ):
            overview_export = client.get(
                f"/matters/{slug}/sources/{token}/summary-export",
                params={"format": format_name},
            )
            assert overview_export.status_code == 200
            assert marker in overview_export.content
            assert (
                overview_export.headers[f"x-{LEGACY_STEM}-export"]
                == "work-product"
            )
            assert document.document_id[:12] not in overview_export.headers[
                "content-disposition"
            ]

        for format_name, marker in (
            ("docx", b"PK"),
            ("markdown", b"# Transcript"),
            ("txt", b"Witness Jordan"),
            ("srt", b"00:00:00,000 --> 00:00:02,000"),
            ("vtt", b"WEBVTT"),
            ("csv", b"Speaker status"),
            ("json", b'"review_status"'),
        ):
            exported = client.get(
                f"/matters/{slug}/sources/{token}/transcript-export",
                params={"format": format_name},
            )
            assert exported.status_code == 200
            assert marker in exported.content
            assert exported.headers[f"x-{LEGACY_STEM}-export"] == "work-product"
            assert document.document_id[:12] not in exported.headers[
                "content-disposition"
            ]

        created = client.post(
            f"/matters/{slug}/sources/{token}/clips",
            data={"title": "Bicycle statement", "start_ms": "0", "end_ms": "2000"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        clip = bench.workspace.media_clips(
            matter.matter_id, document.document_id, document.version_id
        )[0]
        clip_download = client.get(
            f"/matters/{slug}/sources/{token}/clips/{clip.clip_id}/download"
        )
        assert clip_download.status_code == 200
        assert clip_download.headers["content-type"].startswith("audio/wav")
        assert clip_download.content.startswith(b"RIFF")

        bundle = client.get(f"/matters/{slug}/export")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            names = set(archive.namelist())
            assert any(name.startswith("transcripts/") and name.endswith(".md") for name in names)
            assert any(
                name.startswith("transcript-overviews/") and name.endswith(".md")
                for name in names
            )
            overview_name = next(
                name
                for name in names
                if name.startswith("transcript-overviews/") and name.endswith(".md")
            )
            bundled_overview = archive.read(overview_name)
            assert b"Witness Jordan" in bundled_overview
            assert b"Speaker 1" in bundled_overview
            assert b"SPEAKER_00" not in bundled_overview
            assert b"SPEAKER_01" not in bundled_overview
            assert "media/clip-inventory.csv" in names
            assert "media/media-work-product.json" in names
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["transcript_count"] == 1
            assert manifest["transcript_overview_count"] == 1
            assert manifest["clip_count"] == 1
            assert manifest["original_source_files_included"] is False
            media_work_product = json.loads(
                archive.read("media/media-work-product.json")
            )
            assert media_work_product["clips"][0]["start"] == "00:00:00.000"
            assert media_work_product["clips"][0]["end"] == "00:00:02.000"
            clip_inventory = archive.read("media/clip-inventory.csv").decode(
                "utf-8-sig"
            )
            assert "00:00:00.000" in clip_inventory
            assert "00:00:02.000" in clip_inventory

        events = bench.workspace.audit_events(matter.matter_id)
        assert {
                "transcript.segment_edit",
                "transcript.speaker_review",
                "transcript.export",
                "transcript.summary_automatic",
                "work_product.export",
            "media.clip_create",
            "media.clip_export",
        }.issubset({item.action for item in events})

        confirmation = prepare_preview_exit(client, matter)
        current_exit = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        preservation = client.get(f"/matters/{slug}/close/export")
        assert preservation.status_code == 200
        archive_path = tmp_path / "generated-media-preservation.zip"
        archive_path.write_bytes(preservation.content)
        full_manifest = verify_matter_archive(archive_path, expected_sha256=current_exit.archive_digest)
        assert full_manifest["table_rows"]["workbench_media_clip"] == 1
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.read("originals/" + document.document_id) == WAV.read_bytes()
            assert b"The crimson bicycle" in archive.read("records/workbench_transcript_segment_revision.jsonl")
            assert b"Witness Jordan" in archive.read("records/workbench_speaker_mapping_revision.jsonl")
            archived_transcript, = [json.loads(line) for line in archive.read(
                "records/workbench_media_transcript.jsonl").splitlines()]
            assert archived_transcript["quality_json"]["review_recommended"] is True
            assert archived_transcript["provenance_json"]["pipeline"] == "synthetic-test-whisperx"

        closed = client.post(
            f"/matters/{slug}/close",
            data=confirmation,
            follow_redirects=False,
        )
        assert closed.status_code == 303
        assert closed.headers["location"].startswith("/matters/new?")
        for table in (
            "workbench_media_job",
            "workbench_media_transcript",
            "workbench_media_summary",
            "workbench_transcript_segment",
            "workbench_transcript_segment_revision",
            "workbench_speaker_mapping_revision",
            "workbench_media_clip",
        ):
            assert bench.workspace.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE matter_id=?",
                (matter.matter_id,),
            ).fetchone()[0] == 0


def test_dual_modal_answer_resolves_page_and_timestamp_support(tmp_path):
    generator = DualModalAnswerGenerator()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=generator,
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated written and spoken evidence matter")
        media_document, _media_token = _upload_and_wait(client, slug)
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[
                (
                    "files",
                    (
                        "Generated incident report.pdf",
                        PDF.read_bytes(),
                        "application/pdf",
                    ),
                )
            ],
            follow_redirects=False,
        )
        assert uploaded.status_code == 303

        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        pdf_document = next(
            item
            for item in bench.source_store(matter).ready_documents()
            if item.media_type == "application/pdf"
        )
        assert media_document.media_type == "audio/wav"
        assert pdf_document.page_count == 3

        conversation = bench.workspace.get_conversation(matter.matter_id)
        queued = client.post(
            f"/matters/{slug}/ask",
            data={
                "conversation": conversation.conversation_id,
                "question": (
                    "Using both the written report and spoken recording, where was "
                    "the red bicycle logged?"
                ),
                "request_key": "answer-request-" + "d" * 32,
            },
            headers={"Accept": "application/json"},
        )
        assert queued.status_code == 202
        answer_job = queued.json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and answer_job["state"] not in {
            "succeeded",
            "failed",
        }:
            time.sleep(0.01)
            answer_job = client.get(answer_job["status_url"]).json()
        assert answer_job["state"] == "succeeded", answer_job

        answer = bench.workspace.messages(
            matter.matter_id, conversation.conversation_id
        )[-1]
        assert answer.payload["modality_coverage"]["mode"] == "complete"
        citations = [
            citation
            for claim in answer.payload["claims"]
            for citation in claim["citations"]
        ]
        assert len(citations) == 2
        by_kind = {citation["evidence_kind"]: citation for citation in citations}
        assert set(by_kind) == {"document", "transcript"}
        assert by_kind["document"]["location"] == "Page 2"
        assert by_kind["transcript"]["location"] == "00:00–00:02"

        document_support = bench.support(
            matter, by_kind["document"]["support_token"]
        )
        assert document_support.evidence_kind == "document"
        assert document_support.location == "Page 2"
        assert document_support.position_label == "Page 2 of 3"
        assert "red bicycle was logged at the north entrance" in " ".join(
            document_support.lines
        ).casefold()
        assert f"/matters/{slug}/sources/" in document_support.source_review_href
        document_page = client.get(by_kind["document"]["href"])
        assert document_page.status_code == 200
        assert 'id="support-pane"' in document_page.text
        assert "Page 2 of 3" in document_page.text
        assert "Open full PDF" in document_page.text

        transcript_support = bench.support(
            matter, by_kind["transcript"]["support_token"]
        )
        assert transcript_support.evidence_kind == "transcript"
        assert transcript_support.location == "00:00–00:02"
        assert transcript_support.start_ms == 0
        assert transcript_support.end_ms == 2_000
        assert "red bicycle was logged at the north entrance" in " ".join(
            transcript_support.lines
        ).casefold()
        assert f"/matters/{slug}/sources/" in transcript_support.source_review_href
        transcript_page = client.get(by_kind["transcript"]["href"])
        assert transcript_page.status_code == 200
        assert 'id="support-pane"' in transcript_page.text
        assert 'data-support-media data-start-ms="0"' in transcript_page.text
        assert "Play cited moment" in transcript_page.text

        excluded = client.post(
            f"/matters/{slug}/ask",
            data={
                "conversation": conversation.conversation_id,
                "question": (
                    "Answer from the written report, not the transcript: where was "
                    "the red bicycle logged?"
                ),
                "request_key": "answer-request-" + "e" * 32,
            },
            headers={"Accept": "application/json"},
        )
        assert excluded.status_code == 202
        excluded_job = excluded.json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and excluded_job["state"] not in {
            "succeeded",
            "failed",
        }:
            time.sleep(0.01)
            excluded_job = client.get(excluded_job["status_url"]).json()
        assert excluded_job["state"] == "succeeded", excluded_job

        document_only = bench.workspace.messages(
            matter.matter_id, conversation.conversation_id
        )[-1]
        assert document_only.payload["modality_coverage"] == {
            "mode": "complete",
            "requested_evidence_kinds": ["written"],
            "available_evidence_kinds": ["written"],
            "used_evidence_kinds": ["written"],
            "missing_evidence_kinds": [],
            "notice": "This answer cites written passages. Check each claim against the original sources.",
        }
        document_only_citations = [
            citation
            for claim in document_only.payload["claims"]
            for citation in claim["citations"]
        ]
        assert document_only_citations
        assert {item["evidence_kind"] for item in document_only_citations} == {
            "document"
        }
        # A background transcript overview may complete after the requested answer.
        document_calls = [call for call in generator.calls if call["question"].startswith(
            "Answer from the written report, not the transcript"
        )]
        assert document_calls
        assert all({item.evidence_kind for item in call["evidence"]} == {"document"}
            for call in document_calls)
        conversation_page = client.get(
            f"/matters/{slug}?conversation={conversation.conversation_id}"
        )
        assert conversation_page.status_code == 200
        assert "Requested source coverage · Complete" in conversation_page.text


def test_resumable_media_upload_reports_durable_processing_across_matter_views(tmp_path):
    processor = RestartMediaProcessor()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
        background_ingestion=True,
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    body = WAV.read_bytes()
    with TestClient(app) as client:
        slug = _matter(client, "Generated media status matter")
        created = client.post(
            f"/matters/{slug}/upload-sessions",
            json={
                "collection_name": "Generated recordings",
                "files": [
                    {
                        "name": "Generated interview.wav",
                        "relative_path": "Generated interview.wav",
                        "size": len(body),
                        "media_type": "audio/wav",
                    }
                ],
            },
        )
        assert created.status_code == 201
        item = created.json()["items"][0]
        uploaded = client.put(
            item["chunk_url"],
            content=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Upload-Offset": "0",
            },
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["review_ready"] is False
        finalized = client.post(item["finalize_url"])
        assert finalized.status_code == 200
        projection = finalized.json()
        assert projection["contains_media"] is True
        assert projection["review_ready"] is True
        assert projection["processing_count"] == 1
        assert projection["ready_count"] == 0
        assert projection["primary_review_url"].startswith(
            f"/matters/{slug}/sources/"
        )
        assert projection["items"][0]["work_state"] in {"queued", "processing"}
        assert projection["items"][0]["work_stage"]
        assert projection["items"][0]["review_url"] == projection["primary_review_url"]

        workspace = client.get(f"/matters/{slug}")
        assert workspace.status_code == 200
        assert 'data-media-activity' in workspace.text
        assert "Media processing" in workspace.text
        activity = client.get(f"/matters/{slug}/media-activity")
        assert activity.status_code == 200
        assert activity.json()["active_count"] == 1
        assert activity.json()["items"][0]["stage"]

        processor.progress = 0.68
        progress_deadline = time.monotonic() + 5
        while time.monotonic() < progress_deadline:
            progress_view = client.get(projection["status_url"]).json()
            if progress_view["items"][0]["work_progress"] >= 0.68:
                break
            time.sleep(0.01)
        assert progress_view["items"][0]["work_stage"] == "Transcribing"
        assert progress_view["items"][0]["work_progress"] == pytest.approx(0.68)

        processor.release = True
        client.app.state.workbench.media.notify()
        deadline = time.monotonic() + 5
        status = projection
        while time.monotonic() < deadline:
            status = client.get(projection["status_url"]).json()
            if status["ready_count"] == 1:
                break
            time.sleep(0.01)
        assert status["processing_count"] == 0
        assert status["ready_count"] == 1
        assert status["items"][0]["work_state"] == "ready"
        assert status["items"][0]["work_progress"] == 1


def test_media_answer_packs_neighboring_timestamps_and_keeps_matches_on_rejection(tmp_path):
    generator = RejectingCaptureGenerator()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=generator,
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated media answer matter")
        document, _token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        conversation = bench.workspace.get_conversation(matter.matter_id)
        queued = client.post(
            f"/matters/{slug}/ask",
            data={
                "conversation": conversation.conversation_id,
                "question": "What happened near the red bicycle?",
                "request_key": "answer-request-" + "e" * 32,
            },
            headers={"Accept": "application/json"},
        )
        assert queued.status_code == 202
        job = queued.json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and job["state"] not in {
            "succeeded",
            "failed",
        }:
            time.sleep(0.01)
            job = client.get(job["status_url"]).json()
        assert job["state"] == "succeeded"
        answer_calls = [
            call for call in generator.calls
            if "orientation overview" not in call["question"]
        ]
        assert len(answer_calls) == 2
        assert [call["grounding_repair"] for call in answer_calls] == [False, True]
        evidence = answer_calls[0]["evidence"]
        assert len(evidence) == 3
        assert {item.location for item in evidence} == {
            "00:00–00:02",
            "00:02–00:04",
            "00:04–00:07",
        }

        answer = bench.workspace.messages(
            matter.matter_id, conversation.conversation_id
        )[-1]
        assert answer.payload["kind"] == "not-supported"
        assert len(answer.payload["source_matches"]) == 3
        assert all(match["href"] for match in answer.payload["source_matches"])
        assert "potentially relevant source passages" in answer.content

        rendered = client.get(
            f"/matters/{slug}",
            params={"conversation": conversation.conversation_id},
        )
        assert rendered.status_code == 200
        assert "Related source passages" in rendered.text
        assert "00:00–00:02" in rendered.text

        exported = client.get(
            f"/matters/{slug}/conversations/{conversation.conversation_id}/messages/"
            f"{answer.message_id}/export",
            params={"format": "markdown"},
        )
        assert exported.status_code == 200
        assert b"Related source passage" in exported.content
        assert document.state == "ready"


def test_media_answer_repairs_to_a_verified_timestamped_transcript_claim(tmp_path):
    generator = RepairingCaptureGenerator()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=generator,
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated transcript repair matter")
        _document, _token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        conversation = bench.workspace.get_conversation(matter.matter_id)
        queued = client.post(
            f"/matters/{slug}/ask",
            data={
                "conversation": conversation.conversation_id,
                "question": "What happened near the red bicycle?",
                "request_key": "answer-request-" + "f" * 32,
            },
            headers={"Accept": "application/json"},
        )
        assert queued.status_code == 202
        job = queued.json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and job["state"] not in {
            "succeeded",
            "failed",
        }:
            time.sleep(0.01)
            job = client.get(job["status_url"]).json()

        assert job["state"] == "succeeded"
        answer_calls = [
            call for call in generator.calls
            if "orientation overview" not in call["question"]
        ]
        assert [call["grounding_repair"] for call in answer_calls] == [False, True]
        events = bench.workspace.answer_events(
            matter.matter_id, ACTOR, job["job_id"]
        )
        assert any(
            event.stage == "generating"
            and event.message == "Rewriting the draft closer to the cited text for consistency checks."
            for event in events
        )

        answer = bench.workspace.messages(
            matter.matter_id, conversation.conversation_id
        )[-1]
        assert answer.payload["kind"] == "generated"
        assert answer.payload["claims"]
        citations = answer.payload["claims"][0]["citations"]
        assert citations[0]["location"] == "00:02–00:04"
        assert citations[0]["href"].endswith("#support-pane")
        assert "play=1" in citations[0]["href"]


def test_existing_transcript_overview_is_backfilled_automatically_on_restart(tmp_path):
    runtime = tmp_path / "runtime"
    app = create_workbench_app(
        runtime,
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated legacy transcript matter")
        document, token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        transcript = bench.workspace.media_transcript(
            matter.matter_id, document.document_id, document.version_id
        )
        assert transcript is not None
        _wait_for_summary(bench, matter, document, "ready")

        # Simulate a transcript created before migration 0013. Schema migration
        # still does not call the model; coordinator startup owns the backfill.
        # Stop the coordinator before modifying its database to model an offline
        # legacy snapshot without racing a background SQLite transaction.
        bench.media.close()
        bench.media = None
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute(
                "DELETE FROM workbench_media_summary WHERE transcript_id=?",
                (transcript.transcript_id,),
            )
        assert bench.workspace.media_summary(
            matter.matter_id, document.document_id, document.version_id
        ) is None
        matter_id = matter.matter_id
        document_id = document.document_id
        source_version_id = document.version_id

    restarted = create_workbench_app(
        runtime,
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
        media_processor=OfflineMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(restarted) as client:
        bench = client.app.state.workbench
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            summary = bench.workspace.media_summary(
                matter_id, document_id, source_version_id
            )
            if summary is not None and summary.state == "ready":
                break
            time.sleep(0.01)
        assert summary is not None and summary.state == "ready"
        assert summary.attempts == 1
        media_job = bench.workspace.media_job(matter_id, document_id, source_version_id)
        assert media_job is not None and media_job.state in {"succeeded", "degraded"}
        review = client.get(f"/matters/{slug}/sources/{token}")
        assert review.status_code == 200
        assert "AI transcript overview" in review.text
        assert "Create overview" not in review.text
        assert client.get("/health").json()["media_summaries"]["ready"] == 1


def test_speaker_review_save_updates_every_passage_without_navigation(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated speaker review matter")
        document, token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        # Hold the summary worker so the response deterministically reports
        # queued refresh work instead of racing its immediate completion.
        bench.media.close()
        bench.media = None
        matter = bench.matter(slug, ACTOR)
        speaker = next(
            item
            for item in bench.workspace.transcript_speakers(
                matter.matter_id, document.document_id, document.version_id
            )
            if item.speaker_cluster == "SPEAKER_00"
        )
        assert speaker.identity_state == "cluster"
        assert speaker.revision == 0
        opened = client.get(f"/matters/{slug}/sources/{token}")
        assert opened.status_code == 200
        assert "Review speakers" in opened.text
        assert "Speaker 1" in opened.text
        assert "Unconfirmed" in opened.text
        assert ">SPEAKER_00<" not in opened.text
        assert ">Cluster<" not in opened.text
        assert "Clusters are" not in opened.text
        unchanged = next(
            item
            for item in bench.workspace.transcript_speakers(
                matter.matter_id, document.document_id, document.version_id
            )
            if item.speaker_cluster == "SPEAKER_00"
        )
        assert unchanged.identity_state == "cluster"
        assert unchanged.revision == 0

        implicit_confirmation = client.post(
            f"/matters/{slug}/sources/{token}/speakers",
            data={
                "speaker_cluster": speaker.speaker_cluster,
                "expected_revision": str(speaker.revision),
                "display_name": "Unconfirmed supplied label",
            },
            headers={"Accept": "application/json"},
        )
        assert implicit_confirmation.status_code == 422
        still_anonymous = next(
            item
            for item in bench.workspace.transcript_speakers(
                matter.matter_id, document.document_id, document.version_id
            )
            if item.speaker_cluster == "SPEAKER_00"
        )
        assert still_anonymous.identity_state == "cluster"
        assert still_anonymous.revision == 0

        saved = client.post(
            f"/matters/{slug}/sources/{token}/speakers",
            data={
                "speaker_cluster": speaker.speaker_cluster,
                "expected_revision": str(speaker.revision),
                "display_name": "Reviewer supplied label",
                "identity_state": "confirmed",
            },
            headers={"Accept": "application/json"},
        )
        assert saved.status_code == 200
        assert saved.json() == {
            "speaker_cluster": "SPEAKER_00",
            "display_name": "Reviewer supplied label",
            "identity_state": "confirmed",
            "revision": 1,
            "segment_count": 2,
            "message": "Speaker label saved across this transcript.",
            "overview_refreshing": True,
        }
        matching = [
            item
            for item in bench.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            )
            if item.speaker_cluster == "SPEAKER_00"
        ]
        assert len(matching) == 2
        assert {item.speaker_display_name for item in matching} == {
            "Reviewer supplied label"
        }
        assert {item.speaker_identity_state for item in matching} == {"confirmed"}
        reviewed = client.get(f"/matters/{slug}/sources/{token}")
        assert "Reviewer supplied label" in reviewed.text
        assert "Confirmed" in reviewed.text

        fallback = client.post(
            f"/matters/{slug}/sources/{token}/speakers",
            data={
                "speaker_cluster": "SPEAKER_00",
                "expected_revision": "1",
                "display_name": "Corrected reviewer label",
                "identity_state": "confirmed",
                "return_start_ms": "2400",
                "return_page": "1",
                "return_q": "bicycle",
                "return_flag": "edited",
                "return_segment": matching[0].segment_id,
            },
            follow_redirects=False,
        )
        assert fallback.status_code == 303
        location = fallback.headers["location"]
        assert "start_ms=2400" in location
        assert "page=1" in location
        assert "q=bicycle" in location
        assert "flag=edited" in location
        assert "speaker_review=SPEAKER_00" in location
        assert f"segment={matching[0].segment_id}" in location
        assert location.endswith("#speaker-review")


def test_failed_optional_overview_explains_cause_retries_and_independence(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated overview failure matter")
        document, token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        failed = _wait_for_summary(bench, matter, document, "failed")
        assert failed.attempts == 1

        review = client.get(f"/matters/{slug}/sources/{token}")
        assert review.status_code == 200
        assert "Local answering unavailable" in review.text
        assert "Attempt 1 of 3" in review.text
        assert "2 automatic recovery attempts remain" in review.text
        assert "Try overview again" in review.text
        assert "Authenticated playback" in review.text
        assert "Synchronized transcript" in review.text
        assert "The red bicycle was logged" in review.text

        # Reach the retry boundary through the same durable transitions as a
        # reviewer. Direct SQL here races with the live worker connection.
        for attempt in (2, 3):
            retry = client.post(
                f"/matters/{slug}/sources/{token}/summary",
                follow_redirects=False,
            )
            assert retry.status_code == 303
            failed = _wait_for_summary(bench, matter, document, "failed")
            assert failed.attempts == attempt
        bounded = client.get(f"/matters/{slug}/sources/{token}")
        assert bounded.status_code == 200
        assert "Automatic recovery limit reached" in bounded.text
        assert "3 total attempts recorded" in bounded.text
        assert "Try overview again" in bounded.text


def test_speaker_review_browser_contract_preserves_player_scroll_and_focus():
    script = (
        ROOT / "src/case_intelligence/static/case-intelligence.js"
    ).read_text(encoding="utf-8")
    start = script.index('const speakerReviewPanel = document.querySelector("[data-speaker-review]")')
    end = script.index('document.querySelectorAll("[data-seek-ms]")', start)
    speaker_review = script[start:end]

    assert 'form.addEventListener("submit", async (event)' in speaker_review
    assert "event.preventDefault()" in speaker_review
    assert "mediaPlayer.currentTime" in speaker_review
    assert "activeTranscriptSegment?.dataset.segmentId" in speaker_review
    assert "new FormData(form)" in speaker_review
    assert 'headers: { Accept: "application/json" }' in speaker_review
    assert "focus({ preventScroll: true })" in speaker_review
    assert "speakerReviewReturnPosition = { x: window.scrollX, y: window.scrollY }" in speaker_review
    assert "window.scrollTo(position.x, position.y)" in speaker_review
    assert 'querySelector("[data-close-speaker-review]")' in speaker_review
    assert "window.location.assign" not in speaker_review
    assert "window.location.replace" not in speaker_review
    assert "if (speakerReviewPanel?.open) return;" in script


def test_failed_automatic_overview_gets_a_bounded_restart_retry(tmp_path):
    runtime = tmp_path / "runtime"
    unavailable = create_workbench_app(
        runtime,
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(unavailable) as client:
        slug = _matter(client, "Generated overview recovery matter")
        document, _token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        failed = _wait_for_summary(bench, matter, document, "failed")
        assert failed.attempts == 1
        matter_id = matter.matter_id
        document_id = document.document_id
        source_version_id = document.version_id

    recovered = create_workbench_app(
        runtime,
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
        media_processor=OfflineMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(recovered) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = bench.source_store(matter).get(document_id)
        summary = _wait_for_summary(bench, matter, document, "ready")
        assert summary.matter_id == matter_id
        assert summary.source_version_id == source_version_id
        assert summary.attempts == 2


def test_interrupted_overview_at_automatic_limit_requires_manual_retry(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated overview limit matter")
        document, _token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        failed = _wait_for_summary(bench, matter, document, "failed")
        # The media coordinator still shares this connection. Hold the workspace
        # lock for the fixture write so it cannot overlap a worker statement.
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute(
                "UPDATE workbench_media_summary SET state='running',attempts=? "
                "WHERE transcript_id=?",
                (MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS, failed.transcript_id),
            )

        assert bench.workspace.recover_running_media_summaries() == 1
        summary = bench.workspace.media_summary(
            matter.matter_id, document.document_id, document.version_id
        )
        assert summary is not None
        assert summary.state == "failed"
        assert summary.attempts == MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS
        assert bench.workspace.ensure_media_summary_queue() == 0
        assert bench.workspace.claim_media_summary() is None


def test_transcript_and_clip_routes_fail_closed_across_matters(tmp_path):
    processor = ImmediateMediaProcessor()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        alpha_slug = _matter(client, "Alpha media matter")
        alpha_document, alpha_token = _upload_and_wait(client, alpha_slug)
        bench = client.app.state.workbench
        alpha = bench.matter(alpha_slug, ACTOR)
        alpha_segment = bench.workspace.transcript_segments(
            alpha.matter_id, alpha_document.document_id, alpha_document.version_id
        )[0]
        alpha_summary = _wait_for_summary(
            bench, alpha, alpha_document, "failed"
        )
        assert alpha_document.state == "ready"
        clip = bench.workspace.create_media_clip(
            alpha.matter_id,
            alpha_document.document_id,
            alpha_document.version_id,
            title="Alpha only",
            start_ms=0,
            end_ms=2_000,
            actor_id=ACTOR,
        )

        bravo_slug = _matter(client, "Bravo media matter")
        bravo_document, bravo_token = _upload_and_wait(client, bravo_slug)
        bravo = bench.matter(bravo_slug, ACTOR)

        crossed_edit = client.post(
            f"/matters/{bravo_slug}/sources/{bravo_token}/segments/{alpha_segment.segment_id}",
            data={"expected_revision": "0", "text": "crossed matter"},
        )
        assert crossed_edit.status_code == 404
        crossed_clip = client.get(
            f"/matters/{bravo_slug}/sources/{bravo_token}/clips/{clip.clip_id}/download"
        )
        assert crossed_clip.status_code == 404
        crossed_summary = client.get(
            f"/matters/{bravo_slug}/sources/{alpha_token}/summary-export"
        )
        assert crossed_summary.status_code == 404
        assert bench.workspace.transcript_segments(
            alpha.matter_id, alpha_document.document_id, alpha_document.version_id
        )[0].current_text.startswith("The red bicycle")
        assert bravo_document.document_id != alpha_document.document_id


def test_report_media_clip_export_re_resolves_exact_timestamped_source(tmp_path):
    processor = ImmediateMediaProcessor()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated timestamp report")
        document, _token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        clip = bench.workspace.create_media_clip(
            matter.matter_id,
            document.document_id,
            document.version_id,
            title="Generated selected moment",
            start_ms=0,
            end_ms=2_000,
            actor_id=ACTOR,
        )
        report = bench.workspace.create_report(
            matter.matter_id, ACTOR, "Timestamped review"
        )
        bench.add_media_clip_to_report(
            matter, ACTOR, report.report_id, clip.clip_id, expected_status=report.status
        )

        exported = client.get(
            f"/matters/{slug}/reports/{report.report_id}/export?format=markdown"
        )
        assert exported.status_code == 200
        assert "Interview.wav — 00:00–00:02" in exported.text

        foreign_marker = "Generated foreign recording label"
        # Report and media workers still share this connection.
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute(
                "UPDATE workbench_report_citation SET source_name=? "
                "WHERE matter_id=? AND report_id=? AND kind='media_clip'",
                (foreign_marker, matter.matter_id, report.report_id),
            )
        rejected = client.get(
            f"/matters/{slug}/reports/{report.report_id}/export?format=markdown"
        )
        assert rejected.status_code == 400
        assert "no longer resolves" in rejected.text
        assert foreign_marker not in rejected.text


def test_machine_segments_are_immutable_and_revision_conflicts_are_explicit(tmp_path):
    processor = ImmediateMediaProcessor()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client)
        document, _ = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        original = bench.workspace.transcript_segments(
            matter.matter_id, document.document_id, document.version_id
        )[1]
        revised = bench.workspace.revise_transcript_segment(
            matter.matter_id,
            document.document_id,
            document.version_id,
            original.segment_id,
            expected_revision=0,
            text="Officer Lane retained the corrected property receipt.",
            actor_id=ACTOR,
        )
        assert revised.current_revision == 1
        assert revised.model_text == original.model_text
        with pytest.raises(WorkspaceProblem, match="another session"):
            bench.workspace.revise_transcript_segment(
                matter.matter_id,
                document.document_id,
                document.version_id,
                original.segment_id,
                expected_revision=0,
                text="Conflicting text",
                actor_id=ACTOR,
            )
        csv_export = export_transcript(
            document.display_name,
            bench.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            ),
            "csv",
        )
        assert original.model_text.encode() not in csv_export.body
        assert b"corrected property receipt" in csv_export.body


def test_long_transcript_overview_packet_is_bounded_and_spans_the_timeline(tmp_path):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated long transcript packet matter")
        document, _token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        seed = bench.workspace.transcript_segments(
            matter.matter_id, document.document_id, document.version_id
        )[0]
        segments = tuple(
            replace(
                seed,
                segment_id=f"media-segment-{index:032x}",
                ordinal=index + 1,
                start_ms=index * 1_000,
                end_ms=(index + 1) * 1_000,
                current_text=(
                    f"Generated passage {index + 1} describes a synthetic review topic "
                    "with enough words to exercise bounded timeline selection."
                ),
            )
            for index in range(2_000)
        )
        windows = transcript_summary_windows(segments)
        assert len(windows) == 12
        assert windows[0].start_ms == 0
        assert windows[-1].end_ms == 2_000_000
        assert sum(window.segment_count for window in windows) < len(segments)
        assert all(len(window.excerpt) <= 3_600 for window in windows)
        assert [window.evidence_id for window in windows] == [
            f"S{index}" for index in range(1, 13)
        ]


def test_running_media_job_reconciles_after_workbench_restart(tmp_path):
    runtime = tmp_path / "runtime"
    processor = RestartMediaProcessor()
    app = create_workbench_app(
        runtime,
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client)
        uploaded = client.post(
            f"/matters/{slug}/uploads",
            files=[("files", ("Restart.wav", WAV.read_bytes(), "audio/wav"))],
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = bench.workspace.media_job(
                matter.matter_id, document.document_id, document.version_id
            )
            if job is not None and job.state == "running" and job.external_job_id:
                break
            time.sleep(0.01)
        assert job is not None and job.external_job_id
        assert processor.submissions == 1
        assert document.state == "processing"

    processor.release = True
    restarted = create_workbench_app(
        runtime,
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    with TestClient(restarted) as client:
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = bench.workspace.media_job(
                matter.matter_id, document.document_id, document.version_id
            )
            if job is not None and job.state == "succeeded":
                break
            time.sleep(0.01)
        assert job is not None and job.state == "succeeded"
        assert document.state == "ready"
        assert len(document.parsed_units()) == 3
        assert processor.submissions == 1
        assert len(processor.deleted) == 1


@pytest.mark.parametrize("verified_outcome", ("success", "not_found"))
def test_direct_media_source_removal_waits_for_verified_external_cleanup(
    tmp_path, verified_outcome
):
    processor = ConfigurableCleanupMediaProcessor()
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=UnavailableGenerator(),
        auth_mode="test",
        media_processor=processor,
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated external cleanup boundary")
        document, token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        _wait_for_summary(bench, matter, document, "failed")
        source_path = bench.source_store(matter).source_path(document.document_id)
        original_job = bench.workspace.media_job(
            matter.matter_id, document.document_id, document.version_id
        )
        assert original_job is not None and original_job.external_job_id

        processor.cleanup_outcome = "unavailable"
        refused = client.post(
            f"/matters/{slug}/sources/{token}/remove",
            follow_redirects=False,
        )
        assert refused.status_code == 409
        assert "could not be removed yet" in refused.text
        assert source_path.is_file()
        assert (
            bench.source_store(matter).get_by_action_token(token).document_id
            == document.document_id
        )
        retained_job = bench.workspace.media_job(
            matter.matter_id, document.document_id, document.version_id
        )
        assert retained_job is not None
        assert retained_job.media_job_id == original_job.media_job_id
        assert retained_job.external_job_id == original_job.external_job_id
        assert bench.playback is not None
        assert (matter.matter_id, document.document_id) not in (
            bench.playback._cancelled_documents
        )
        retained_support = bench.search(matter, "red bicycle")
        assert any(
            item.document_id == document.document_id for item in retained_support
        )

        processor.cleanup_outcome = verified_outcome
        removed = client.post(
            f"/matters/{slug}/sources/{token}/remove",
            follow_redirects=False,
        )
        assert removed.status_code == 303
        assert not source_path.exists()
        with pytest.raises(KeyError):
            bench.source_store(matter).get_by_action_token(token)
        assert (
            bench.workspace.media_job(
                matter.matter_id, document.document_id, document.version_id
            )
            is None
        )
        assert [item[2] for item in processor.cleanup_attempts[-2:]] == [
            "unavailable",
            verified_outcome,
        ]


@pytest.mark.parametrize("corruption", ("citation", "basis"))
def test_transcript_overview_export_requires_current_exact_timestamps(
    tmp_path, corruption
):
    app = create_workbench_app(
        tmp_path / "runtime",
        generator=EvidenceEchoGenerator(),
        auth_mode="test",
        media_processor=ImmediateMediaProcessor(),
        media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated overview export boundary")
        document, token = _upload_and_wait(client, slug)
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        summary = _wait_for_summary(bench, matter, document, "ready")

        if corruption == "citation":
            payload = json.loads(json.dumps(summary.payload))
            payload["claims"][0]["citations"][0]["start_ms"] += 1
            with bench.workspace._lock, bench.workspace.connection:
                bench.workspace.connection.execute(
                    "UPDATE workbench_media_summary SET payload_json=? "
                    "WHERE transcript_id=?",
                    (json.dumps(payload), summary.transcript_id),
                )
        else:
            with bench.workspace._lock, bench.workspace.connection:
                bench.workspace.connection.execute(
                    "UPDATE workbench_media_summary SET basis_digest=? "
                    "WHERE transcript_id=?",
                    ("f" * 64, summary.transcript_id),
                )

        direct = client.get(
            f"/matters/{slug}/sources/{token}/summary-export",
            params={"format": "markdown"},
        )
        assert direct.status_code == 409
        assert "not ready for export" in direct.text

        bundle = client.get(f"/matters/{slug}/export")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            names = archive.namelist()
            assert any(name.startswith("transcripts/") for name in names)
            assert not any(name.startswith("transcript-overviews/") for name in names)


@pytest.mark.parametrize("status, warning_only", [
    ("failed", False), ("succeeded", False), ("skipped", False), ("", True),
])
def test_transcript_processing_notices_are_visible_escaped_and_exported(
    tmp_path, status, warning_only
):
    hostile_notice = "Synthetic caution <script>window.syntheticLeak=true</script>"

    class QualityProcessor(ImmediateMediaProcessor):
        def transcript(self, owner, external_job_id):
            result = dict(super().transcript(owner, external_job_id))
            result["quality"] = {"diarization_status": status}
            result["warnings"] = (
                [{"code": "speaker_attribution_unavailable", "user_message": hostile_notice}]
                if warning_only else
                [{"code": "synthetic_quality_notice", "user_message": hostile_notice}]
                if status == "failed" else []
            )
            return result

    app = create_workbench_app(
        tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test",
        media_processor=QualityProcessor(), media_poll_seconds=0.01,
    )
    with TestClient(app) as client:
        slug = _matter(client, "Generated transcript quality matter")
        _document, token = _upload_and_wait(client, slug)
        source = f"/matters/{slug}/sources/{token}"
        review = client.get(source)
        assert review.status_code == 200
        failed = status == "failed" or warning_only
        assert ('data-transcript-notices' in review.text) is (failed or status == "skipped")
        assert ('Automatic speaker attribution failed.' in review.text) is failed
        assert ('Speaker attribution was skipped.' in review.text) is (status == "skipped")
        if failed:
            assert 'Unreviewed speaker labels are placeholders' in review.text
            assert '&lt;script&gt;window.syntheticLeak=true&lt;/script&gt;' in review.text
            assert '<script>window.syntheticLeak' not in review.text
        exported = client.get(source + '/transcript-export?format=json')
        assert exported.status_code == 200
        notices = exported.json().get('processing_notices', [])
        assert bool(notices) is (failed or status == "skipped")
        if failed:
            assert any('Automatic speaker attribution failed.' in n for n in notices)
            assert hostile_notice in notices
        for kind in ['txt', 'markdown', 'docx']:
            artifact = client.get(source + '/transcript-export?format=' + kind)
            assert artifact.status_code == 200
            if failed:
                body = artifact.content
                if kind == 'docx':
                    with zipfile.ZipFile(io.BytesIO(body)) as archive:
                        body = archive.read('word/document.xml')
                assert b'Automatic speaker attribution failed.' in body
        # Notices describe processing; they must never become timed speech cues.
        subtitles = client.get(source + '/transcript-export?format=srt')
        assert subtitles.status_code == 200
        assert b'Automatic speaker attribution failed.' not in subtitles.content
        if failed:
            bundle = client.get(f'/matters/{slug}/export')
            assert bundle.status_code == 200
            with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
                transcripts = [n for n in archive.namelist()
                               if n.startswith('transcripts/') and n.endswith('.json')]
                assert len(transcripts) == 1
                assert json.loads(archive.read(transcripts[0]))['processing_notices'] == notices
