"""Single-host durable worker for the ephemeral v2 delivery service."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import tempfile
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .domain import (
    IdentityMethod,
    IdentityStatus,
    Job,
    JobStatus,
    PipelineStage,
    SegmentDraft,
    StageStatus,
)
from .exporters import ExportBundle, source_export_base_name, write_export_bundle
from .media import MediaDurationExceeded, probe_media
from .model_manifest import ModelManifestError, ModelReadiness, verify_model_manifest
from .pipeline import (
    PipelineResult,
    PipelineStatus,
    TranscriptionPipeline,
    TranscriptionRequest,
    create_pipeline_engine,
)
from .resources import gpu_reservation, gpu_states
from .runtime import Runtime, build_runtime
from .settings import Settings
from .store import InvalidTransitionError, NotFoundError


LOGGER = logging.getLogger("transcription_v2.worker")

_DIARIZATION_ARTIFACT_ROLE = "diarization"
_COMMUNITY_1_MODEL_ID = "pyannote/speaker-diarization-community-1"
_PIPELINE_STAGE_MAP = {
    "input_validation": PipelineStage.PROBE,
    "source_transcription": PipelineStage.TRANSCRIBE,
    "source_alignment": PipelineStage.ALIGN,
    "diarization": PipelineStage.DIARIZE,
    "speaker_identity": PipelineStage.IDENTIFY,
    "translation": PipelineStage.TRANSLATE,
}


class WorkerConfigurationError(RuntimeError):
    pass


class WorkerResourceUnavailable(RuntimeError):
    pass


def _approved_diarization_config_present(
    configured_path: str | None,
    *,
    cache_root: Path,
    model_readiness: ModelReadiness,
    backend: str = "community-1",
) -> bool:
    if backend == "nemotron":
        from .nemotron import approved_snapshot
        return approved_snapshot(configured_path, cache_root=cache_root,
                                 model_readiness=model_readiness)
    if not configured_path:
        return False
    path = Path(configured_path).expanduser()
    config = path if path.is_file() else path / "config.yaml"
    try:
        resolved_config = config.resolve(strict=True)
        resolved_cache = cache_root.resolve(strict=True)
        resolved_config.relative_to(resolved_cache)
    except (OSError, ValueError):
        return False
    return model_readiness.authorizes_file(
        config,
        role=_DIARIZATION_ARTIFACT_ROLE,
        model_id=_COMMUNITY_1_MODEL_ID,
    )


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= number <= 1.0:
        return number
    if number < 0.0:
        return max(0.0, min(1.0, math.exp(max(-50.0, number))))
    return None


def _translation_for_segment(
    segment: Mapping[str, Any], translated: Sequence[Mapping[str, Any]]
) -> str | None:
    """Return a review-only approximation from every overlapping translation cue.

    The canonical translation remains unchanged in ``machine-result.json`` and
    delivery exports.  This value is only a convenience alongside a source
    segment because speech-translation timestamps are not forced-aligned.
    """

    try:
        source_start = float(segment.get("start") or 0.0)
        source_end = float(segment.get("end") or source_start)
    except (TypeError, ValueError):
        return None
    matches: list[tuple[float, int, str]] = []
    for ordinal, candidate in enumerate(translated):
        try:
            start = float(candidate.get("start") or 0.0)
            end = float(candidate.get("end") or start)
        except (TypeError, ValueError):
            continue
        overlap = max(0.0, min(source_end, end) - max(source_start, start))
        text = str(candidate.get("text") or "").strip()
        if overlap > 0.0 and text:
            matches.append((start, ordinal, text))
    matches.sort(key=lambda item: (item[0], item[1]))
    return " ".join(item[2] for item in matches) or None


def _segment_drafts(result: PipelineResult) -> list[SegmentDraft]:
    translation_segments: list[Mapping[str, Any]] = []
    if isinstance(result.translation, Mapping):
        candidates = result.translation.get("segments")
        if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
            translation_segments = [item for item in candidates if isinstance(item, Mapping)]

    drafts: list[SegmentDraft] = []
    previous_end = 0
    for ordinal, segment in enumerate(result.segments):
        try:
            start_ms = max(0, round(float(segment.get("start") or 0.0) * 1000))
        except (TypeError, ValueError):
            start_ms = previous_end
        try:
            end_ms = max(start_ms, round(float(segment.get("end") or 0.0) * 1000))
        except (TypeError, ValueError):
            end_ms = start_ms
        previous_end = end_ms
        speaker = str(segment.get("speaker") or "").strip() or None
        drafts.append(
            SegmentDraft(
                ordinal=ordinal,
                start_ms=start_ms,
                end_ms=end_ms,
                model_text=str(segment.get("text") or "").strip(),
                speaker_key=speaker,
                translated_text=_translation_for_segment(segment, translation_segments),
                confidence=_safe_confidence(segment.get("confidence")),
                overlap=bool(segment.get("overlap")),
            )
        )
    return drafts


class JobWorker:
    def __init__(
        self,
        runtime: Runtime | None = None,
        *,
        pipeline_factory: Callable[[Callable[[str, str], None]], TranscriptionPipeline] | None = None,
    ) -> None:
        self.runtime = runtime or build_runtime()
        self.settings = self.runtime.settings
        self.store = self.runtime.store
        self.storage = self.runtime.storage
        self._stop = threading.Event()
        self._engine = None
        self._diarization_model_available = True
        self.model_readiness: ModelReadiness | None = None
        diarization_model_path = (
            os.environ.get("TRANSCRIPTION_V2_DIARIZATION_MODEL_PATH") or None
        )
        if self.settings.pipeline_backend == "whisperx":
            try:
                self.model_readiness = verify_model_manifest(
                    self.settings.model_cache_dir,
                    self.settings.approved_model_manifest_path,
                )
            except ModelManifestError:
                raise WorkerConfigurationError(
                    "approved local model artifacts are not ready; "
                    "run transcription-v2 verify-models"
                ) from None
            self._diarization_model_available = (
                _approved_diarization_config_present(
                    diarization_model_path,
                    cache_root=self.settings.model_cache_dir,
                    model_readiness=self.model_readiness,
                    backend=self.settings.diarization_backend,
                )
            )
            if (
                not self._diarization_model_available
                and not self.settings.allow_degraded_diarization
            ):
                raise WorkerConfigurationError(
                    "approved local diarization model is not ready; "
                    "stage the selected backend or explicitly allow degraded diarization"
                )
        if pipeline_factory is None:
            # One single-threaded worker owns one bounded model cache. Reusing
            # it avoids reloading large local weights for every queued job.
            self._engine = create_pipeline_engine(
                self.settings.pipeline_backend,
                model_cache_dir=self.settings.model_cache_dir,
                model_readiness=self.model_readiness,
                diarization_model_path=diarization_model_path,
                diarization_backend=self.settings.diarization_backend,
            )
            self._pipeline_factory = self._default_pipeline
        else:
            self._pipeline_factory = pipeline_factory

    def _default_pipeline(
        self, callback: Callable[[str, str], None]
    ) -> TranscriptionPipeline:
        return TranscriptionPipeline(
            self._engine,
            diarization_model_available=self._diarization_model_available,
            stage_callback=callback,
        )

    def stop(self) -> None:
        self._stop.set()

    def recover(self) -> None:
        stale_before = datetime.now(timezone.utc) - timedelta(
            minutes=self.settings.stale_job_minutes
        )
        summary = self.store.recover_running_jobs(stale_before=stale_before)
        if summary.requeued or summary.canceled or summary.failed:
            LOGGER.info(
                "worker_recovery requeued=%d canceled=%d failed=%d",
                summary.requeued,
                summary.canceled,
                summary.failed,
            )

    def purge_expired(self) -> list[str]:
        purged = self.store.purge_expired(delete_files=self.storage.delete_job_tree)
        stale_before = datetime.now(timezone.utc) - timedelta(
            hours=self.settings.max_active_job_hours
        )
        purged.extend(
            self.store.purge_stale_unfinished(
                stale_before=stale_before,
                delete_files=self.storage.delete_job_tree,
            )
        )
        if purged:
            LOGGER.info("expired_jobs_purged count=%d", len(purged))
        return purged

    def run_forever(self) -> None:
        self.recover()
        self.purge_expired()
        next_maintenance = time.monotonic() + 60.0
        while not self._stop.is_set():
            worked = self.run_once()
            if time.monotonic() >= next_maintenance:
                # Reconsider rows that were too fresh to recover immediately
                # after a crash, and enforce expiry after a long job returns.
                self.recover()
                self.purge_expired()
                next_maintenance = time.monotonic() + 60.0
            if not worked:
                self._stop.wait(self.settings.worker_poll_seconds)

    def run_once(self) -> bool:
        if self.settings.pipeline_backend == "whisperx":
            try:
                self._assert_reserved_gpu()
            except WorkerResourceUnavailable:
                # Leave jobs queued; lack of VRAM must not consume attempts.
                return False
        job = self.store.claim_next(self.settings.worker_id)
        if job is None:
            return False
        LOGGER.info("job_claimed job_id=%s attempt=%d", job.id, job.attempt)
        try:
            self._process(job)
        except Exception as exc:  # error details are intentionally not logged
            try:
                current = self.store.get_job(job.id)
                if current.status is JobStatus.CANCEL_REQUESTED:
                    self._finish_cancellation(current)
                elif current.status is JobStatus.RUNNING:
                    error_code, retryable = self._classify_error(exc)
                    LOGGER.error(
                        "job_failed job_id=%s error_code=%s",
                        job.id,
                        error_code,
                    )
                    self.store.fail_job(
                        job.id,
                        self.settings.worker_id,
                        error_code,
                        retryable=retryable,
                        retry_delay_seconds=30 if retryable else 0,
                    )
            except NotFoundError:
                pass
        return True

    def _process(self, job: Job) -> None:
        files = self.store.list_files(job.id, job.owner_key)
        if len(files) != 1:
            raise WorkerConfigurationError("a v2 worker job must contain exactly one file")
        job_file = files[0]
        media_path = self.storage.resolve_relative(job_file.relative_path)
        self.store.set_stage(
            job.id, self.settings.worker_id, PipelineStage.PROBE, StageStatus.RUNNING
        )
        probe = probe_media(
            media_path,
            max_duration_seconds=self.settings.max_media_duration_seconds,
        )
        _atomic_json(
            self.storage.job_paths(job.id).work / "media_probe.json", probe.to_dict()
        )
        self.store.set_stage(
            job.id, self.settings.worker_id, PipelineStage.PROBE, StageStatus.SUCCEEDED
        )
        current_after_probe = self.store.get_job(job.id)
        if current_after_probe.status is JobStatus.CANCEL_REQUESTED:
            self._finish_cancellation(current_after_probe)
            return

        cancellation_seen = threading.Event()

        def progress(stage_name: str, status_name: str) -> None:
            stage = _PIPELINE_STAGE_MAP.get(stage_name)
            if stage is None:
                return
            current = self.store.get_job(job.id)
            if current.status is JobStatus.CANCEL_REQUESTED:
                cancellation_seen.set()
                return
            status = {
                "running": StageStatus.RUNNING,
                "succeeded": StageStatus.SUCCEEDED,
                "skipped": StageStatus.SKIPPED,
                "degraded": StageStatus.SUCCEEDED,
                "failed": StageStatus.SUCCEEDED,
            }.get(status_name)
            if status is None:
                return
            reason = status_name if status_name in {"degraded", "failed"} else None
            try:
                self.store.set_stage(
                    job.id,
                    self.settings.worker_id,
                    stage,
                    status,
                    reason_code=reason,
                )
            except InvalidTransitionError:
                # A later callback may have already advanced the durable stage.
                pass

        request = TranscriptionRequest(
            audio_path=media_path,
            profile=job.profile,
            language=None if job.options.source_language == "auto" else job.options.source_language,
            translation_target="en" if job.options.translate_to_english else None,
            diarize_speakers=job.options.diarize_speakers,
            min_speakers=job.options.min_speakers,
            max_speakers=job.options.max_speakers,
            hotwords=job.options.hotwords,
            device="cuda" if self.settings.pipeline_backend == "whisperx" else "cpu",
            device_index=0,
            local_files_only=True,
            job_id=job.id,
        )

        reservation = nullcontext()
        if self.settings.pipeline_backend == "whisperx":
            self._assert_reserved_gpu()
            reservation = gpu_reservation(self.settings.gpu_lock_path)

        pipeline = self._pipeline_factory(progress)
        def cancel_requested() -> bool:
            if cancellation_seen.is_set():
                return True
            try:
                return self.store.get_job(job.id).status is JobStatus.CANCEL_REQUESTED
            except NotFoundError:
                return True

        with self._heartbeat(job.id, cancellation_seen), reservation:
            result = pipeline.run(request, cancel_requested=cancel_requested)

        current = self.store.get_job(job.id)
        if current.status is JobStatus.CANCEL_REQUESTED or cancellation_seen.is_set():
            self._finish_cancellation(current)
            return
        if result.status is PipelineStatus.FAILED:
            raise RuntimeError("pipeline_failed")

        self.store.set_stage(
            job.id,
            self.settings.worker_id,
            PipelineStage.QUALITY_CONTROL,
            StageStatus.RUNNING,
        )
        drafts = _segment_drafts(result)
        replace = getattr(self.store, "replace_segments", None)
        if callable(replace):
            replace(job.id, job_file.id, self.settings.worker_id, drafts)
        else:
            existing = self.store.list_segments(job.id, job.owner_key, file_id=job_file.id)
            if not existing:
                self.store.add_segments(job.id, job_file.id, self.settings.worker_id, drafts)
        for speaker_key in sorted({draft.speaker_key for draft in drafts if draft.speaker_key}):
            existing = {
                item.speaker_key: item
                for item in self.store.list_speaker_mappings(job.id, job.owner_key)
            }
            if speaker_key not in existing:
                self.store.set_speaker_mapping(
                    job.id,
                    job.owner_key,
                    speaker_key,
                    speaker_key,
                    identity_status=IdentityStatus.UNKNOWN,
                    identity_method=IdentityMethod.NONE,
                    expected_revision=0,
                )
        self.store.set_stage(
            job.id,
            self.settings.worker_id,
            PipelineStage.QUALITY_CONTROL,
            StageStatus.SUCCEEDED,
        )

        self.store.set_stage(
            job.id, self.settings.worker_id, PipelineStage.EXPORT, StageStatus.RUNNING
        )
        _atomic_json(
            self.storage.job_paths(job.id).work / "machine-result.json",
            result.to_dict(),
        )
        bundle = write_export_bundle(
            result,
            self.storage.job_paths(job.id).output,
            base_name=source_export_base_name(job_file.safe_name),
            create_zip=True,
        )
        self._write_ui_summary(job, job_file.sha256, probe.to_dict(), result, bundle)
        self.store.set_stage(
            job.id, self.settings.worker_id, PipelineStage.EXPORT, StageStatus.SUCCEEDED
        )
        completed = self.store.complete_job(job.id, self.settings.worker_id)
        LOGGER.info(
            "job_ready_for_delivery job_id=%s expires_at=%s degraded=%s",
            job.id,
            completed.expires_at,
            result.status is PipelineStatus.DEGRADED,
        )

    def _write_ui_summary(
        self,
        job: Job,
        source_sha256: str,
        probe: Mapping[str, Any],
        result: PipelineResult,
        bundle: ExportBundle,
    ) -> None:
        warnings = [
            {"code": item.get("code"), "user_message": item.get("message")}
            for item in result.warnings
            if isinstance(item, Mapping)
        ]
        summary = {
            "schema_version": 1,
            "job_id": job.id,
            "degraded": result.status is PipelineStatus.DEGRADED,
            "warnings": warnings,
            "quality": {
                "detected_language": result.source_language,
                "source_alignment_status": result.stage("source_alignment").status.value,
                "language_confidence": result.source_language_confidence,
                "diarization_requested": result.diarization.get("requested", True),
                "diarization_status": result.diarization.get("status"),
                "speaker_count": result.diarization.get("speaker_count"),
                "low_confidence_segments": sum(
                    1
                    for segment in result.segments
                    if _safe_confidence(segment.get("confidence")) is not None
                    and _safe_confidence(segment.get("confidence")) < 0.75
                ),
                "overlap_segments": sum(1 for segment in result.segments if segment.get("overlap")),
            },
            "provenance": {
                "source_sha256": source_sha256,
                "profile": job.profile,
                "pipeline_version": result.provenance.get("pipeline_version"),
                "asr_model": result.profile.get("asr_model"),
                "alignment_model": result.profile.get("alignment_backend"),
                "diarization_model": result.diarization.get("model"),
                "mock": result.provenance.get("engine", {}).get("mock") is True,
                "model_artifacts": (
                    self.model_readiness.public_dict()
                    if self.model_readiness is not None
                    else {
                        "status": "not_required",
                        "ready": None,
                        "required": False,
                    }
                ),
                "media": dict(probe),
            },
            "bundle": {
                "manifest": bundle.manifest_path.name,
                "zip": bundle.zip_path.name if bundle.zip_path else None,
                "sha256": bundle.archive_sha256,
            },
        }
        _atomic_json(self.storage.job_paths(job.id).output / "ui-summary.json", summary)

    def _finish_cancellation(self, job: Job) -> None:
        events = self.store.list_events(job.id, job.owner_key)
        delete_requested = any(
            event.reason_code == "owner_delete" for event in events[-5:]
        )
        if job.status is JobStatus.CANCEL_REQUESTED:
            self.store.acknowledge_cancel(job.id, self.settings.worker_id)
        if delete_requested:
            self.store.delete_job(
                job.id,
                job.owner_key,
                delete_files=self.storage.delete_job_tree,
            )
        LOGGER.info("job_canceled job_id=%s purge_requested=%s", job.id, delete_requested)

    def _assert_reserved_gpu(self) -> None:
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
        if not visible or "," in visible or not visible.isdigit():
            raise WorkerConfigurationError(
                "real inference requires exactly one numeric CUDA_VISIBLE_DEVICES reservation"
            )
        physical_index = int(visible)
        state = next((item for item in gpu_states() if item.index == physical_index), None)
        if state is None:
            raise WorkerResourceUnavailable("the reserved GPU is not available")
        if state.free_vram_mb < self.settings.minimum_free_vram_mb:
            raise WorkerResourceUnavailable("the reserved GPU lacks required free VRAM")

    def _heartbeat(self, job_id: str, cancellation_seen: threading.Event):
        worker = self

        class HeartbeatContext:
            def __enter__(self):
                self.stop = threading.Event()

                def beat() -> None:
                    while not self.stop.wait(15):
                        try:
                            current = worker.store.heartbeat(job_id, worker.settings.worker_id)
                            if current.status is JobStatus.CANCEL_REQUESTED:
                                cancellation_seen.set()
                        except (InvalidTransitionError, NotFoundError):
                            return

                self.thread = threading.Thread(target=beat, name="v2-heartbeat", daemon=True)
                self.thread.start()
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.stop.set()
                self.thread.join(timeout=2)
                return False

        return HeartbeatContext()

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[str, bool]:
        if isinstance(exc, MediaDurationExceeded):
            return "media_duration_exceeded", False
        if isinstance(exc, WorkerConfigurationError):
            return "worker_configuration", False
        if isinstance(exc, WorkerResourceUnavailable):
            return "gpu_resources_unavailable", True
        if isinstance(exc, OSError):
            return "local_io_error", True
        return "pipeline_or_export_error", False


def run_worker(settings: Settings | None = None) -> None:
    worker = JobWorker(build_runtime(settings))

    def stop(_signum: int, _frame: Any) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run_forever()


__all__ = [
    "JobWorker",
    "WorkerConfigurationError",
    "WorkerResourceUnavailable",
    "run_worker",
]
