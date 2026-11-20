from __future__ import annotations

import io
import hashlib
import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from transcription_v2.domain import JobStatus, TranscriptionOptions
from transcription_v2.pipeline import (
    MockPipelineEngine,
    TranscriptionPipeline,
    TranscriptionRequest,
)
from transcription_v2.runtime import build_runtime
from transcription_v2.settings import Settings
from transcription_v2.model_manifest import MODEL_MANIFEST_SCHEMA
from transcription_v2.media import MediaDurationExceeded
from transcription_v2.worker import (
    JobWorker,
    WorkerConfigurationError,
    _translation_for_segment,
)


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16_000)
        recording.writeframes(b"\x00\x00" * 16_000)
    return output.getvalue()


def _manifest_file(path: Path, cache: Path) -> dict[str, object]:
    return {
        "path": path.resolve().relative_to(cache.resolve()).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _approved_manifest(
    cache: Path,
    *,
    diarization_config: Path | None = None,
) -> Path:
    artifact = cache / "model.bin"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"approved model fixture")
    artifacts: list[dict[str, object]] = [
        {
            "role": "asr",
            "model_id": "example/model",
            "revision": "0123456789abcdef",
            "license": "MIT",
            "files": [_manifest_file(artifact, cache)],
        }
    ]
    if diarization_config is not None:
        artifacts.append(
            {
                "role": "diarization",
                "model_id": "pyannote/speaker-diarization-community-1",
                "revision": "0123456789abcdef0123456789abcdef01234567",
                "license": "CC-BY-4.0",
                # A Hugging Face snapshot config is a symlink. Approve and hash
                # its regular cache blob while the runtime receives the
                # snapshot-facing config.yaml path.
                "files": [_manifest_file(diarization_config, cache)],
            }
        )
    manifest = cache / "approved-model-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": MODEL_MANIFEST_SCHEMA,
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _staged_diarization_config(root: Path) -> Path:
    repository = root / "models--pyannote--speaker-diarization-community-1"
    blob = repository / "blobs" / "approved-community-1-config"
    blob.parent.mkdir(parents=True)
    blob.write_text("pipeline: {}\n", encoding="utf-8")
    snapshot = repository / "snapshots" / (
        "0123456789abcdef0123456789abcdef01234567"
    )
    snapshot.mkdir(parents=True)
    config = snapshot / "config.yaml"
    config.symlink_to("../../blobs/approved-community-1-config")
    return config


class _TrackingDiarizationEngine(MockPipelineEngine):
    def __init__(self) -> None:
        self.diarization_called = False

    def diarize(self, request, profile):  # type: ignore[no-untyped-def]
        self.diarization_called = True
        return super().diarize(request, profile)


class WorkerTests(unittest.TestCase):
    def test_duration_limit_is_permanent_and_staff_safe(self) -> None:
        self.assertEqual(
            ("media_duration_exceeded", False),
            JobWorker._classify_error(MediaDurationExceeded("private detail")),
        )

    def test_transcription_options_preserve_legacy_default_and_opt_out(self) -> None:
        legacy = TranscriptionOptions.from_mapping(
            {"schema_version": 1, "source_language": "en"}
        )
        self.assertTrue(legacy.diarize_speakers)

        opted_out = TranscriptionOptions(diarize_speakers=False)
        restored = TranscriptionOptions.from_json(opted_out.to_json())
        self.assertFalse(restored.diarize_speakers)
        with self.assertRaisesRegex(ValueError, "diarize_speakers"):
            TranscriptionOptions(diarize_speakers="false")  # type: ignore[arg-type]

    def test_mock_worker_does_not_require_or_read_model_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = build_runtime(
                Settings(
                    data_root=root,
                    database_path=root / "jobs.sqlite3",
                    pipeline_backend="mock",
                    model_cache_dir=root / "missing-models",
                )
            )
            worker = JobWorker(runtime)
            self.assertIsNone(worker.model_readiness)

    def test_real_worker_fails_before_engine_construction_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "models"
            cache.mkdir()
            runtime = build_runtime(
                Settings(
                    data_root=root / "data",
                    database_path=root / "data" / "jobs.sqlite3",
                    pipeline_backend="whisperx",
                    model_cache_dir=cache,
                )
            )
            with patch("transcription_v2.worker.create_pipeline_engine") as create_engine:
                with self.assertRaisesRegex(WorkerConfigurationError, "verify-models"):
                    JobWorker(runtime)
            create_engine.assert_not_called()

    def test_real_worker_constructs_only_after_manifest_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "models"
            diarization_config = _staged_diarization_config(cache)
            manifest = _approved_manifest(
                cache, diarization_config=diarization_config
            )
            runtime = build_runtime(
                Settings(
                    data_root=root / "data",
                    database_path=root / "data" / "jobs.sqlite3",
                    pipeline_backend="whisperx",
                    model_cache_dir=cache,
                    model_manifest_path=manifest,
                )
            )
            with patch.dict(
                "os.environ",
                {
                    "TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH": str(
                        diarization_config
                    )
                },
            ):
                with patch(
                    "transcription_v2.worker.create_pipeline_engine",
                    return_value=MockPipelineEngine(),
                ) as create_engine:
                    worker = JobWorker(runtime)
            create_engine.assert_called_once()
            self.assertIsNotNone(worker.model_readiness)
            self.assertTrue(worker.model_readiness.public_dict()["ready"])

    def test_real_worker_strict_mode_rejects_missing_diarization_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "models"
            diarization_config = _staged_diarization_config(cache)
            manifest = _approved_manifest(
                cache, diarization_config=diarization_config
            )
            diarization_config.unlink()
            runtime = build_runtime(
                Settings(
                    data_root=root / "data",
                    database_path=root / "data" / "jobs.sqlite3",
                    pipeline_backend="whisperx",
                    model_cache_dir=cache,
                    model_manifest_path=manifest,
                    allow_degraded_diarization=False,
                )
            )
            with patch.dict(
                "os.environ",
                {
                    "TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH": str(
                        diarization_config
                    )
                },
            ):
                with patch(
                    "transcription_v2.worker.create_pipeline_engine"
                ) as create_engine:
                    with self.assertRaisesRegex(
                        WorkerConfigurationError,
                        "diarization model is not ready",
                    ):
                        JobWorker(runtime)
            create_engine.assert_not_called()

    def test_strict_mode_rejects_present_config_omitted_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "models"
            diarization_config = _staged_diarization_config(cache)
            manifest = _approved_manifest(cache)
            runtime = build_runtime(
                Settings(
                    data_root=root / "data",
                    database_path=root / "data" / "jobs.sqlite3",
                    pipeline_backend="whisperx",
                    model_cache_dir=cache,
                    model_manifest_path=manifest,
                    allow_degraded_diarization=False,
                )
            )
            with patch.dict(
                "os.environ",
                {
                    "TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH": str(
                        diarization_config
                    )
                },
            ):
                with patch(
                    "transcription_v2.worker.create_pipeline_engine"
                ) as create_engine:
                    with self.assertRaisesRegex(
                        WorkerConfigurationError,
                        "diarization model is not ready",
                    ):
                        JobWorker(runtime)
            create_engine.assert_not_called()

    def test_degraded_mode_skips_present_config_omitted_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "models"
            diarization_config = _staged_diarization_config(cache)
            manifest = _approved_manifest(cache)
            runtime = build_runtime(
                Settings(
                    data_root=root / "data",
                    database_path=root / "data" / "jobs.sqlite3",
                    pipeline_backend="whisperx",
                    model_cache_dir=cache,
                    model_manifest_path=manifest,
                    allow_degraded_diarization=True,
                )
            )
            engine = _TrackingDiarizationEngine()
            with patch.dict(
                "os.environ",
                {
                    "TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH": str(
                        diarization_config
                    )
                },
            ):
                with patch(
                    "transcription_v2.worker.create_pipeline_engine",
                    return_value=engine,
                ):
                    worker = JobWorker(runtime)

            media = root / "fixture.wav"
            media.write_bytes(b"opaque fixture bytes")
            result = worker._default_pipeline(lambda _stage, _status: None).run(
                TranscriptionRequest(media)
            )

            self.assertFalse(engine.diarization_called)
            self.assertEqual(result.diarization["status"], "skipped")
            self.assertEqual(result.errors, [])
            self.assertTrue(
                any(
                    warning["code"] == "diarization_model_unavailable"
                    for warning in result.warnings
                )
            )

    def test_strict_mode_rejects_unlisted_config_for_approved_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "models"
            approved_config = _staged_diarization_config(cache)
            manifest = _approved_manifest(
                cache, diarization_config=approved_config
            )
            unlisted_config = cache / "unlisted" / "config.yaml"
            unlisted_config.parent.mkdir()
            unlisted_config.write_text("pipeline: {}\n", encoding="utf-8")
            runtime = build_runtime(
                Settings(
                    data_root=root / "data",
                    database_path=root / "data" / "jobs.sqlite3",
                    pipeline_backend="whisperx",
                    model_cache_dir=cache,
                    model_manifest_path=manifest,
                    allow_degraded_diarization=False,
                )
            )
            with patch.dict(
                "os.environ",
                {
                    "TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH": str(
                        unlisted_config
                    )
                },
            ):
                with patch(
                    "transcription_v2.worker.create_pipeline_engine"
                ) as create_engine:
                    with self.assertRaisesRegex(
                        WorkerConfigurationError,
                        "diarization model is not ready",
                    ):
                        JobWorker(runtime)
            create_engine.assert_not_called()

    def test_real_worker_degraded_mode_wires_missing_model_to_pipeline_skip(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "models"
            manifest = _approved_manifest(cache)
            runtime = build_runtime(
                Settings(
                    data_root=root / "data",
                    database_path=root / "data" / "jobs.sqlite3",
                    pipeline_backend="whisperx",
                    model_cache_dir=cache,
                    model_manifest_path=manifest,
                    allow_degraded_diarization=True,
                )
            )
            engine = _TrackingDiarizationEngine()
            with patch.dict(
                "os.environ",
                {
                    "TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH": str(
                        cache / "missing-community-1-snapshot"
                    )
                },
            ):
                with patch(
                    "transcription_v2.worker.create_pipeline_engine",
                    return_value=engine,
                ):
                    worker = JobWorker(runtime)

            media = root / "fixture.wav"
            media.write_bytes(b"opaque fixture bytes")
            result = worker._default_pipeline(lambda _stage, _status: None).run(
                TranscriptionRequest(media)
            )

            self.assertFalse(engine.diarization_called)
            self.assertEqual(result.diarization["status"], "skipped")
            self.assertEqual(result.stage("diarization").status.value, "skipped")
            self.assertEqual(result.errors, [])
            self.assertTrue(
                any(
                    warning["code"] == "diarization_model_unavailable"
                    for warning in result.warnings
                )
            )

    def test_default_worker_reuses_one_engine_between_job_pipelines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = build_runtime(
                Settings(
                    data_root=root,
                    database_path=root / "jobs.sqlite3",
                    pipeline_backend="mock",
                )
            )
            worker = JobWorker(runtime)
            first = worker._default_pipeline(lambda _stage, _status: None)
            second = worker._default_pipeline(lambda _stage, _status: None)
            self.assertIs(first.engine, second.engine)

    def test_review_translation_hint_combines_all_overlapping_cues(self) -> None:
        source = {"start": 0.0, "end": 3.0}
        translated = [
            {"start": 1.0, "end": 2.0, "text": "second"},
            {"start": 0.0, "end": 1.5, "text": "first"},
            {"start": 4.0, "end": 5.0, "text": "outside"},
        ]
        self.assertEqual(
            _translation_for_segment(source, translated),
            "first second",
        )

    def test_mock_job_with_diarization_opt_out_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = build_runtime(
                Settings(
                    data_root=root,
                    database_path=root / "jobs.sqlite3",
                    pipeline_backend="mock",
                )
            )
            job = runtime.store.create_job(
                owner_key="owner",
                options=TranscriptionOptions(
                    retention_hours=4,
                    diarize_speakers=False,
                ),
                profile="balanced",
            )
            ingested = runtime.storage.ingest_stream(
                job.id,
                io.BytesIO(_wav_bytes()),
                "Interview One.wav",
                media_type="audio/wav",
                max_bytes=10 * 1024 * 1024,
            )
            job_file = runtime.store.register_file(
                job.id,
                owner_key="owner",
                original_name=ingested.original_name,
                safe_name=ingested.safe_name,
                relative_path=ingested.relative_path,
                media_type=ingested.media_type,
                size_bytes=ingested.size_bytes,
                sha256=ingested.sha256,
            )
            runtime.store.enqueue_job(job.id, "owner")

            worker = JobWorker(runtime)
            self.assertTrue(worker.run_once())

            completed = runtime.store.get_owned_job(job.id, "owner")
            self.assertIs(completed.status, JobStatus.SUCCEEDED)
            self.assertIsNotNone(completed.expires_at)
            segments = runtime.store.list_segments(job.id, "owner", file_id=job_file.id)
            self.assertEqual(len(segments), 1)
            self.assertIn("Synthetic test transcript", segments[0].model_text)
            self.assertIsNone(segments[0].speaker_key)
            summary_path = runtime.storage.job_paths(job.id).output / "ui-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertTrue(summary["degraded"])
            self.assertTrue(summary["provenance"]["mock"])
            self.assertEqual(summary["quality"]["source_alignment_status"], "succeeded")
            self.assertFalse(summary["quality"]["diarization_requested"])
            self.assertEqual(summary["quality"]["diarization_status"], "skipped")
            self.assertIsNone(summary["provenance"]["diarization_model"])
            archive = runtime.storage.job_paths(job.id).output / "Interview_One.delivery.zip"
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as package:
                self.assertNotIn(ingested.safe_name, package.namelist())
                self.assertIn("Interview_One.docx", package.namelist())
                self.assertIn("Interview_One.txt", package.namelist())
                self.assertIn("Interview_One.srt", package.namelist())
                self.assertIn("Interview_One.manifest.json", package.namelist())
                manifest = json.loads(
                    package.read("Interview_One.manifest.json").decode("utf-8")
                )
                self.assertFalse(manifest["diarization"]["requested"])
                self.assertEqual(manifest["diarization"]["status"], "skipped")
                self.assertIsNone(manifest["diarization"]["model"])
            machine = runtime.storage.job_paths(job.id).work / "machine-result.json"
            self.assertTrue(machine.is_file())

    def test_cancel_after_source_stops_before_later_model_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = build_runtime(
                Settings(
                    data_root=root,
                    database_path=root / "jobs.sqlite3",
                    pipeline_backend="mock",
                )
            )
            job = runtime.store.create_job(
                owner_key="owner",
                options=TranscriptionOptions(),
                profile="balanced",
            )
            ingested = runtime.storage.ingest_stream(
                job.id,
                io.BytesIO(_wav_bytes()),
                "cancel.wav",
                media_type="audio/wav",
            )
            runtime.store.register_file(
                job.id,
                owner_key="owner",
                original_name=ingested.original_name,
                safe_name=ingested.safe_name,
                relative_path=ingested.relative_path,
                media_type=ingested.media_type,
                size_bytes=ingested.size_bytes,
                sha256=ingested.sha256,
            )
            runtime.store.enqueue_job(job.id, "owner")

            class CancelAfterSource(MockPipelineEngine):
                alignment_called = False

                def transcribe_source(self, request, profile):  # type: ignore[no-untyped-def]
                    value = super().transcribe_source(request, profile)
                    runtime.store.request_cancel(job.id, "owner")
                    return value

                def align_source(self, request, profile, source_result):  # type: ignore[no-untyped-def]
                    self.alignment_called = True
                    return super().align_source(request, profile, source_result)

            engine = CancelAfterSource()
            worker = JobWorker(
                runtime,
                pipeline_factory=lambda callback: TranscriptionPipeline(
                    engine,
                    stage_callback=callback,
                ),
            )
            self.assertTrue(worker.run_once())
            self.assertFalse(engine.alignment_called)
            self.assertIs(runtime.store.get_job(job.id).status, JobStatus.CANCELED)


if __name__ == "__main__":
    unittest.main()
