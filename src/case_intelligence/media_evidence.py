"""Matter-owned media transcription, review projections, and bounded exports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import httpx

from .media_preflight import (
    MESSAGES as PREFLIGHT_MESSAGES,
    REVISION as PREFLIGHT_REVISION,
    inspect_recording,
)
from .branding import PRODUCT_NAME
from .derived_text import presentation_text
from .generation import GenerationRejected, GenerationUnavailable
from .pilot_uploads import PilotDocument, PilotStore, PilotUnit, UploadProblem
from .service_endpoints import validate_service_endpoint
from .workspace_store import (
    MediaClipRecord,
    MediaJobRecord,
    MediaSummaryRecord,
    MediaTranscriptRecord,
    MatterRecord,
    TranscriptSegmentRecord,
    WorkspaceStore,
)
from .work_product_exports import (
    DOCX_MEDIA_TYPE,
    ExportBlock,
    _TEXT_PRESENTATION_NOTICE,
    _markdown_escape,
    blocks_to_docx,
    blocks_to_markdown,
)


class MediaProcessorError(RuntimeError):
    """Staff-safe failure at the transient processor boundary."""


class MediaProcessorNotFound(MediaProcessorError):
    """The processor verified that an ephemeral job is already absent."""


MAX_SUMMARY_WINDOWS = 12
MAX_SUMMARY_WINDOW_CHARS = 3_500


@dataclass(frozen=True)
class TranscriptSummaryWindow:
    evidence_id: str
    excerpt: str
    location: str
    start_ms: int
    end_ms: int
    first_ordinal: int
    segment_count: int


@dataclass(frozen=True)
class TranscriptSummaryResult:
    payload: Mapping[str, object]
    basis_digest: str
    covered_segment_count: int
    total_segment_count: int


def transcript_summary_basis(segments: Sequence[TranscriptSegmentRecord]) -> str:
    payload = [
        {
            "segment_id": item.segment_id,
            "ordinal": item.ordinal,
            "start_ms": item.start_ms,
            "end_ms": item.end_ms,
            "speaker": item.speaker_display_name,
            "speaker_state": item.speaker_identity_state,
            "text": item.current_text,
            "text_revision": item.current_revision,
            "speaker_revision": item.speaker_revision,
        }
        for item in segments
    ]
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def transcript_summary_windows(
    segments: Sequence[TranscriptSegmentRecord],
) -> tuple[TranscriptSummaryWindow, ...]:
    if not segments:
        return ()
    speaker_labels = transcript_speaker_labels(segments)
    groups: list[list[tuple[TranscriptSegmentRecord, str]]] = []
    current: list[tuple[TranscriptSegmentRecord, str]] = []
    current_size = 0
    for segment in segments:
        line = (
            f"[{format_timestamp(segment.start_ms)}–{format_timestamp(segment.end_ms)}] "
            f"{speaker_labels[segment.speaker_cluster]}: {segment.current_text}"
        )
        if current and current_size + len(line) + 1 > MAX_SUMMARY_WINDOW_CHARS:
            groups.append(current)
            current = []
            current_size = 0
        current.append((segment, line[:MAX_SUMMARY_WINDOW_CHARS]))
        current_size += min(len(line), MAX_SUMMARY_WINDOW_CHARS) + 1
    if current:
        groups.append(current)

    if len(groups) > MAX_SUMMARY_WINDOWS:
        selected_indices = {
            round(index * (len(groups) - 1) / (MAX_SUMMARY_WINDOWS - 1))
            for index in range(MAX_SUMMARY_WINDOWS)
        }
        if len(selected_indices) < MAX_SUMMARY_WINDOWS:
            selected_indices.update(
                index
                for index in range(len(groups))
                if index not in selected_indices
                and len(selected_indices) < MAX_SUMMARY_WINDOWS
            )
        selected = [groups[index] for index in sorted(selected_indices)]
    else:
        selected = groups

    windows: list[TranscriptSummaryWindow] = []
    for index, group in enumerate(selected, 1):
        first = group[0][0]
        last = group[-1][0]
        windows.append(
            TranscriptSummaryWindow(
                evidence_id=f"S{index}",
                excerpt="\n".join(line for _segment, line in group),
                location=(
                    f"{format_timestamp(first.start_ms)}–{format_timestamp(last.end_ms)}"
                ),
                start_ms=first.start_ms,
                end_ms=last.end_ms,
                first_ordinal=first.ordinal,
                segment_count=len(group),
            )
        )
    return tuple(windows)


class MediaProcessor(Protocol):
    @property
    def available(self) -> bool: ...

    def ready(self, owner: str) -> bool: ...

    def find_job(self, owner: str, source_sha256: str, byte_size: int) -> Mapping[str, object] | None: ...

    def submit(
        self, owner: str, source: Path, media_type: str, source_sha256: str
    ) -> Mapping[str, object]: ...

    def job(self, owner: str, external_job_id: str) -> Mapping[str, object]: ...

    def transcript(self, owner: str, external_job_id: str) -> Mapping[str, object]: ...

    def delete(self, owner: str, external_job_id: str) -> None: ...

    def cancel(self, owner: str, external_job_id: str) -> None: ...


def _safe_endpoint(value: str) -> str:
    return validate_service_endpoint(
        value,
        environment_name="CASE_INTELLIGENCE_TRANSCRIPTION_ALLOWED_HOSTS",
        label="transcription endpoint",
        origin_only=True,
    )


class TranscriptionV2Client:
    """Content-minimizing client for the private, ephemeral WhisperX v2 API."""

    def __init__(self, endpoint: str, token_file: Path) -> None:
        self.endpoint = _safe_endpoint(endpoint)
        self.token_file = Path(token_file)

    @classmethod
    def from_environment(cls) -> TranscriptionV2Client | None:
        endpoint = os.getenv("CASE_INTELLIGENCE_TRANSCRIPTION_URL", "").strip()
        token_file = os.getenv("CASE_INTELLIGENCE_TRANSCRIPTION_TOKEN_FILE", "").strip()
        if not endpoint or not token_file:
            return None
        try:
            return cls(endpoint, Path(token_file))
        except ValueError:
            return None

    @property
    def available(self) -> bool:
        try:
            metadata = self.token_file.stat(follow_symlinks=False)
            return (
                not self.token_file.is_symlink()
                and stat.S_ISREG(metadata.st_mode)
                and 0 < metadata.st_size <= 4_096
                and not metadata.st_mode & 0o077
            )
        except OSError:
            return False

    def _token(self) -> str:
        if not self.available:
            raise MediaProcessorError("The local transcription service is not configured.")
        try:
            descriptor = os.open(
                self.token_file, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                token = stream.read(4_097).strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise MediaProcessorError(
                "The local transcription service is not configured."
            ) from exc
        if not token or len(token) > 4_096:
            raise MediaProcessorError("The local transcription service is not configured.")
        return token

    @staticmethod
    def _owner(value: str) -> str:
        if not re.fullmatch(r"ci-media-job-[0-9a-f]{32}", value or ""):
            raise MediaProcessorError("The transcription job identity is invalid.")
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        owner: str,
        files=None,
        data=None,
    ) -> Mapping[str, object]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-User-ID": self._owner(owner),
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(connect=5, read=3_600, write=3_600, pool=5)
        try:
            with httpx.Client(
                base_url=self.endpoint,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(method, path, files=files, data=data)
            if response.status_code >= 400:
                code = ""
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        code = str(payload.get("code") or "")
                except (ValueError, json.JSONDecodeError):
                    pass
                if response.status_code == 404:
                    raise MediaProcessorNotFound(
                        "The temporary transcription job expired."
                    )
                if response.status_code in {429, 507}:
                    raise MediaProcessorError(
                        "The local transcription service is at capacity. Try again later."
                    )
                if response.status_code == 413:
                    raise MediaProcessorError(
                        "The recording exceeds the transcription service limit."
                    )
                raise MediaProcessorError(
                    "The local transcription service could not accept this job."
                    + (f" ({code})" if code else "")
                )
            payload = response.json()
        except MediaProcessorError:
            raise
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise MediaProcessorError(
                "The local transcription service is temporarily unavailable."
            ) from exc
        if not isinstance(payload, dict):
            raise MediaProcessorError("The transcription service returned invalid data.")
        return payload

    def ready(self, owner: str) -> bool:
        try:
            payload = self._request("GET", "/ready", owner=owner)
            checks = payload.get("checks")
            return payload.get("status") == "ready" or (
                isinstance(checks, dict)
                and bool(checks.get("reserved_gpu_ready"))
                and bool(checks.get("offline_runtime"))
            )
        except MediaProcessorError:
            return False

    def find_job(
        self, owner: str, source_sha256: str, byte_size: int
    ) -> Mapping[str, object] | None:
        payload = self._request("GET", "/v1/jobs?limit=20", owner=owner)
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise MediaProcessorError("The transcription service returned invalid job data.")
        matches = [
            item
            for item in jobs
            if isinstance(item, dict)
            and item.get("source_sha256") == source_sha256
            and item.get("size_bytes") == byte_size
        ]
        if len(matches) > 1:
            raise MediaProcessorError("The temporary transcription job is ambiguous.")
        return matches[0] if matches else None

    @staticmethod
    def _safe_suffix(media_type: str) -> str:
        return {
            "audio/wav": ".wav",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/ogg": ".ogg",
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/webm": ".webm",
        }.get(media_type, ".media")

    def submit(
        self, owner: str, source: Path, media_type: str, source_sha256: str
    ) -> Mapping[str, object]:
        if not source.is_file() or source.is_symlink():
            raise MediaProcessorError("The stored recording is unavailable.")
        options = json.dumps(
            {
                "profile": "high_accuracy",
                "source_language": "auto",
                "diarize_speakers": True,
                "translate_to_english": False,
                "retention_hours": 4,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                try:
                    payload = self._request(
                        "POST",
                        "/v1/jobs",
                        owner=owner,
                        files={
                            "files": (
                                "source" + self._safe_suffix(media_type),
                                stream,
                                media_type,
                            )
                        },
                        data={"options": options},
                    )
                except MediaProcessorError as submission_error:
                    try:
                        reconciled = self.find_job(
                            owner, source_sha256, source.stat().st_size
                        )
                    except MediaProcessorError:
                        reconciled = None
                    if reconciled is not None:
                        return reconciled
                    raise submission_error
        except OSError as exc:
            raise MediaProcessorError("The stored recording is unavailable.") from exc
        jobs = payload.get("jobs")
        if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
            # A response can be lost after v2 durably accepted the source. The
            # coordinator reconciles this owner by exact digest before resubmitting.
            reconciled = self.find_job(owner, source_sha256, source.stat().st_size)
            if reconciled is None:
                raise MediaProcessorError("The transcription job was not accepted.")
            return reconciled
        return jobs[0]

    def job(self, owner: str, external_job_id: str) -> Mapping[str, object]:
        return self._request("GET", f"/v1/jobs/{external_job_id}", owner=owner)

    def transcript(self, owner: str, external_job_id: str) -> Mapping[str, object]:
        return self._request(
            "GET", f"/v1/jobs/{external_job_id}/transcript", owner=owner
        )

    def delete(self, owner: str, external_job_id: str) -> None:
        self._request("DELETE", f"/v1/jobs/{external_job_id}", owner=owner)

    def cancel(self, owner: str, external_job_id: str) -> None:
        try:
            self._request("POST", f"/v1/jobs/{external_job_id}/cancel", owner=owner)
        except MediaProcessorError:
            return


def _matched_external_id(view: Mapping[str, object], job: MediaJobRecord) -> str:
    if view.get("source_sha256") != job.source_sha256 or view.get("size_bytes") != job.byte_size:
        raise MediaProcessorError("The transcription service did not preserve the submitted source identity.")
    external_id = str(view.get("job_id") or view.get("id") or "")
    if not external_id:
        raise MediaProcessorError("The transcription service returned an invalid job identity.")
    return external_id


def processor_owner(media_job_id: str) -> str:
    if not re.fullmatch(r"media-job-[0-9a-f]{32}", media_job_id or ""):
        raise ValueError("invalid media job")
    return "ci-" + media_job_id


def normalize_transcript_payload(
    payload: Mapping[str, object], *, duration_ms: int
) -> tuple[Mapping[str, object], ...]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise MediaProcessorError("The transcription service returned no transcript.")
    normalized: list[Mapping[str, object]] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            raise MediaProcessorError("The transcription service returned invalid segments.")
        speaker = item.get("speaker")
        if not isinstance(speaker, dict):
            speaker = {}
        try:
            start_ms = round(float(item.get("start")) * 1000)
            end_ms = round(float(item.get("end")) * 1000)
        except (TypeError, ValueError) as exc:
            raise MediaProcessorError(
                "The transcription service returned invalid timestamps."
            ) from exc
        if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms + 2_000:
            raise MediaProcessorError(
                "The transcription service returned timestamps outside the recording."
            )
        text = str(item.get("model_text") or item.get("text") or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "external_segment_id": str(
                    item.get("segment_id") or item.get("id") or ""
                ),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_cluster": str(speaker.get("cluster_id") or "UNKNOWN"),
                "speaker_display_name": str(
                    speaker.get("display_name") or speaker.get("cluster_id") or "UNKNOWN"
                ),
                "model_text": text,
                "translated_text": item.get("translated_text"),
                "confidence": item.get("confidence"),
                "low_confidence": bool(item.get("low_confidence")),
                "overlap": bool(item.get("overlap")),
            }
        )
    if not normalized:
        raise MediaProcessorError("The recording produced no searchable transcript.")
    return tuple(normalized)


def transcript_speaker_labels(
    segments: Sequence[TranscriptSegmentRecord],
) -> dict[str, str]:
    """Assign one stable staff-facing label to every transcript speaker cluster."""

    confirmed_labels: dict[str, str] = {}
    anonymous_clusters: set[str] = set()
    for segment in segments:
        display_name = " ".join(segment.speaker_display_name.split()).strip()
        if segment.speaker_identity_state == "confirmed" and display_name:
            confirmed_labels[segment.speaker_cluster] = display_name
        else:
            anonymous_clusters.add(segment.speaker_cluster)
    anonymous_clusters.difference_update(confirmed_labels)
    anonymous_labels = {
        cluster: f"Speaker {ordinal}"
        for ordinal, cluster in enumerate(
            sorted(anonymous_clusters),
            1,
        )
    }
    return {**anonymous_labels, **confirmed_labels}


def transcript_units(
    segments: Sequence[TranscriptSegmentRecord],
) -> tuple[PilotUnit, ...]:
    units: list[PilotUnit] = []
    speaker_labels = transcript_speaker_labels(segments)
    for ordinal, segment in enumerate(segments, 1):
        speaker = speaker_labels[segment.speaker_cluster]
        text = f"{speaker}: {segment.current_text}"
        units.append(
            PilotUnit(
                ordinal,
                text,
                line_start=segment.start_ms,
                line_end=segment.end_ms,
                excerpt_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                speaker_cluster=segment.speaker_cluster,
            )
        )
    return tuple(units)


ProjectTranscript = Callable[
    [MatterRecord, PilotDocument, Sequence[TranscriptSegmentRecord], bool], None
]


class MediaCoordinator:
    """Admit one transcription at a time and recover safely across restarts."""

    TERMINAL_READY = {"review_ready", "degraded"}
    TERMINAL_FAILED = {"failed", "canceled"}

    def __init__(
        self,
        workspace: WorkspaceStore,
        processor: MediaProcessor | None,
        *,
        resolve_matter: Callable[[str], MatterRecord],
        resolve_store: Callable[[MatterRecord], PilotStore],
        project_transcript: ProjectTranscript,
        summarize_transcript: Callable[
            [PilotDocument, Sequence[TranscriptSegmentRecord]], TranscriptSummaryResult
        ]
        | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.workspace = workspace
        self.processor = processor
        self.resolve_matter = resolve_matter
        self.resolve_store = resolve_store
        self.project_transcript = project_transcript
        self.summarize_transcript = summarize_transcript
        self.poll_seconds = max(float(poll_seconds), 0.05)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._summary_wake = threading.Event()
        self._state_condition = threading.Condition()
        self._active_job: MediaJobRecord | None = None
        self._active_summary: MediaSummaryRecord | None = None
        self._cancelled_matters: set[str] = set()
        self.workspace.recover_running_media_jobs()
        self.workspace.recover_running_media_summaries()
        self.workspace.ensure_media_summary_queue()
        self._thread = threading.Thread(
            target=self._run,
            name="case-media-1",
            daemon=True,
        )
        self._summary_thread = threading.Thread(
            target=self._run_summaries,
            name="case-media-summary-1",
            daemon=True,
        )
        self._thread.start()
        self._summary_thread.start()

    @property
    def available(self) -> bool:
        return bool(self.processor is not None and self.processor.available)

    def notify(self) -> None:
        self._wake.set()
        self._summary_wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._summary_wake.set()
        self._thread.join(timeout=5)
        self._summary_thread.join(timeout=35)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self.workspace.claim_media_job("media-worker-1")
            if job is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            with self._state_condition:
                self._active_job = job
            try:
                self._process(job)
            finally:
                with self._state_condition:
                    self._active_job = None
                    self._state_condition.notify_all()

    def _run_summaries(self) -> None:
        while not self._stop.is_set():
            summary = self.workspace.claim_media_summary()
            if summary is None:
                self._summary_wake.wait(timeout=0.5)
                self._summary_wake.clear()
                continue
            with self._state_condition:
                self._active_summary = summary
            try:
                self._process_summary(summary)
            finally:
                with self._state_condition:
                    self._active_summary = None
                    self._state_condition.notify_all()

    def _matter_cancelled(self, matter_id: str) -> bool:
        with self._state_condition:
            return matter_id in self._cancelled_matters

    def _stage(self, job: MediaJobRecord, view: Mapping[str, object]) -> None:
        stage = view.get("stage")
        label = "Transcribing"
        progress = 0.1
        if isinstance(stage, dict):
            label = str(stage.get("label") or label)[:100]
            try:
                progress = float(stage.get("progress") or progress)
            except (TypeError, ValueError):
                progress = 0.1
        self.workspace.update_media_job(
            job.media_job_id,
            stage=label,
            progress=progress,
            external_job_id=str(view.get("job_id") or view.get("id") or "") or None,
            message=str(view.get("user_message") or "")[:240],
        )

    def _project_existing(
        self, job: MediaJobRecord, matter: MatterRecord, document: PilotDocument
    ) -> bool:
        store = self.resolve_store(matter)
        with store.mutation_guard():
            transcript = self.workspace.media_transcript(
                job.matter_id, job.document_id, job.source_version_id
            )
            if transcript is None:
                return False
            segments = self.workspace.transcript_segments(
                job.matter_id, job.document_id, job.source_version_id
            )
            self.project_transcript(matter, document, segments, bool(job.degraded))
            return True

    def _process_summary(self, summary: MediaSummaryRecord) -> None:
        if self._matter_cancelled(summary.matter_id):
            return
        if self.summarize_transcript is None:
            try:
                self.workspace.fail_media_summary(
                    summary.transcript_id,
                    "Transcript overview is unavailable while local answering is offline.",
                )
            except KeyError:
                pass
            return
        try:
            matter = self.resolve_matter(summary.matter_id)
            document = self.resolve_store(matter).get(summary.document_id)
            if document.version_id != summary.source_version_id:
                raise MediaProcessorError(
                    "The source version changed before its overview was created."
                )
            segments = self.workspace.transcript_segments(
                summary.matter_id, summary.document_id, summary.source_version_id
            )
            if not segments:
                raise MediaProcessorError(
                    "The transcript does not contain passages for an overview."
                )
            result = self.summarize_transcript(document, segments)
            finished = self.workspace.finish_media_summary(
                summary.transcript_id,
                payload=result.payload,
                basis_digest=result.basis_digest,
                covered_segment_count=result.covered_segment_count,
                total_segment_count=result.total_segment_count,
            )
            try:
                self.workspace.append_audit_event(
                    actor_principal_id=None,
                    session_id=None,
                    matter_id=finished.matter_id,
                    request_id=f"media-summary-{finished.transcript_id[-32:]}",
                    action="transcript.summary_automatic",
                    outcome="success",
                    object_type="transcript_summary",
                    object_id=finished.transcript_id,
                    details={"state": finished.state},
                )
            except Exception:
                pass
        except GenerationUnavailable:
            self._fail_summary(
                summary,
                "Local answering is unavailable for this optional overview.",
            )
        except GenerationRejected:
            self._fail_summary(
                summary,
                "The overview did not have enough cited transcript support.",
            )
        except MediaProcessorError as exc:
            message = str(exc)
            if "version changed" in message.casefold():
                message = "The transcript changed before the overview finished."
            elif "does not contain passages" in message.casefold():
                message = "The transcript has no passages available for an overview."
            else:
                message = "The current transcript could not be prepared for an overview."
            self._fail_summary(summary, message)
        except Exception:
            self._fail_summary(
                summary,
                "The optional overview encountered an unexpected local error.",
            )

    def _fail_summary(
        self, summary: MediaSummaryRecord, message: str
    ) -> MediaSummaryRecord | None:
        """Persist a content-free, staff-readable optional-overview failure."""

        try:
            failed = self.workspace.fail_media_summary(summary.transcript_id, message)
        except KeyError:
            return None
        try:
            self.workspace.append_audit_event(
                actor_principal_id=None,
                session_id=None,
                matter_id=failed.matter_id,
                request_id=f"media-summary-{failed.transcript_id[-32:]}",
                action="transcript.summary_automatic",
                outcome="failure",
                object_type="transcript_summary",
                object_id=failed.transcript_id,
                details={"state": failed.state},
            )
        except Exception:
            pass
        return failed

    def _require_job_access(self, job: MediaJobRecord) -> None:
        try:
            self.workspace.membership(job.matter_id, job.requested_by)
        except KeyError as exc:
            raise MediaProcessorError("Matter access was removed; transcription stopped.") from exc

    def _process(self, claimed: MediaJobRecord) -> None:
        job = claimed
        owner = processor_owner(job.media_job_id)
        external_id = job.external_job_id
        try:
            self._require_job_access(job)
            known_hold = (
                job.preflight.get("outcome") in {"no_audio", "no_speech", "uncertain", "failed"}
                and job.preflight.get("continued") is False
            )
            if not external_id and job.attempts > 1 and not known_hold:
                if self.processor is None or not self.processor.available:
                    raise MediaProcessorError(
                        "Earlier transcription work could not be checked. Try again when the service is available."
                    )
                recovered = self.processor.find_job(owner, job.source_sha256, job.byte_size)
                if recovered is not None:
                    external_id = _matched_external_id(recovered, job)
                    job = self.workspace.update_media_job(
                        job.media_job_id, stage="Resuming transcription", progress=0.03,
                        external_job_id=external_id,
                    )
            if self._matter_cancelled(job.matter_id):
                raise MediaProcessorError("Transcription cancelled because the matter is closing.")
            matter = self.resolve_matter(job.matter_id)
            store = self.resolve_store(matter)
            document = store.get(job.document_id)
            if (
                document.version_id != job.source_version_id
                or document.digest != job.source_sha256
                or document.size != job.byte_size
            ):
                raise MediaProcessorError("The source version changed before transcription.")
            if self._project_existing(job, matter, document):
                if self.processor is not None and external_id:
                    try:
                        self.processor.delete(owner, external_id)
                    except MediaProcessorError:
                        pass
                transcript = self.workspace.media_transcript(
                    job.matter_id, job.document_id, job.source_version_id
                )
                assert transcript is not None
                self.workspace.finish_media_job(
                    job.media_job_id,
                    degraded=bool(job.degraded),
                    message=document.message,
                    warnings=transcript.warnings,
                    quality=transcript.quality,
                    provenance=transcript.provenance,
                )
                return
            self._require_job_access(job)
            source = store.source_path(job.document_id, verify_digest=True)
            if not external_id:
                preflight = job.preflight
                if not (
                    preflight.get("revision") == PREFLIGHT_REVISION
                    and (preflight.get("outcome") == "ready" or preflight.get("continued") is True)
                ):
                    store.mark_media_processing(job.document_id, stage="Checking recording")
                    self.workspace.update_media_job(
                        job.media_job_id, stage="Checking recording", progress=0
                    )
                    result = inspect_recording(
                        source, job.media_type,
                        cancelled=lambda: self._stop.is_set() or self._matter_cancelled(job.matter_id),
                    )
                    if self._stop.is_set():
                        return  # durable running job is recovered on restart
                    if self._matter_cancelled(job.matter_id):
                        raise MediaProcessorError("Recording check cancelled because the matter is closing.")
                    with store.mutation_guard():
                        if document.version_id != job.source_version_id or document.digest != job.source_sha256:
                            raise MediaProcessorError("The source version changed during the recording check.")
                        store.source_path(job.document_id, verify_digest=True)
                        hold = result["outcome"] != "ready"
                        message = PREFLIGHT_MESSAGES[str(result["outcome"])]
                        if hold:
                            document.state = "playback_only" if result["outcome"] == "no_audio" else "needs_review"
                            document.message = message
                            document.processing_stage = "Recording checked"
                            document.completed_units = document.total_units = 0
                            store._save((document.document_id,))
                        # Save the source projection first. An interruption before
                        # this queue transition remains recoverable as running work.
                        job = self.workspace.save_media_preflight(
                            job.media_job_id, result, hold=hold, message=message
                        )
                        if hold:
                            return
            if self.processor is None or not self.processor.available:
                raise MediaProcessorError(
                    "The local transcription service is not configured. Choose Try again after it is restored."
                )
            if not self.processor.ready(owner):
                raise MediaProcessorError(
                    "The local transcription service is not ready. Choose Try again shortly."
                )
            self._require_job_access(job)
            source = store.source_path(job.document_id)
            store.mark_media_processing(job.document_id, stage="Preparing transcription")
            if not external_id:
                reconciled = self.processor.find_job(
                    owner, job.source_sha256, job.byte_size
                )
                if reconciled is None:
                    self.workspace.update_media_job(
                        job.media_job_id, stage="Submitting recording", progress=0.02
                    )
                    reconciled = self.processor.submit(
                        owner, source, job.media_type, job.source_sha256
                    )
                external_id = _matched_external_id(reconciled, job)
                job = self.workspace.update_media_job(
                    job.media_job_id,
                    stage="Queued for transcription",
                    progress=0.03,
                    external_job_id=external_id,
                )
            if self._matter_cancelled(job.matter_id):
                self.cancel_external(job)
                raise MediaProcessorError("Transcription cancelled because the matter is closing.")
            last_projection: tuple[str, str, float, str] | None = None
            while not self._stop.is_set():
                if self._matter_cancelled(job.matter_id):
                    self.cancel_external(job)
                    raise MediaProcessorError(
                        "Transcription cancelled because the matter is closing."
                    )
                self._require_job_access(job)
                view = self.processor.job(owner, external_id)
                status = str(view.get("status") or "")
                stage_view = view.get("stage")
                stage_label = (
                    str(stage_view.get("label") or "")
                    if isinstance(stage_view, dict)
                    else ""
                )
                try:
                    stage_progress = (
                        float(stage_view.get("progress") or 0.1)
                        if isinstance(stage_view, dict)
                        else 0.1
                    )
                except (TypeError, ValueError):
                    stage_progress = 0.1
                stage_progress = max(0.0, min(stage_progress, 1.0))
                projection = (
                    stage_label,
                    status,
                    round(stage_progress, 4),
                    str(view.get("user_message") or "")[:240],
                )
                if projection != last_projection:
                    self._stage(job, view)
                    store.mark_media_processing(
                        job.document_id,
                        stage=stage_label or "Transcribing",
                        progress=stage_progress,
                    )
                    last_projection = projection
                if status in self.TERMINAL_FAILED:
                    message = str(view.get("user_message") or "")[:240]
                    raise MediaProcessorError(
                        message or "Transcription did not finish. Choose Try again."
                    )
                if status in self.TERMINAL_READY:
                    degraded = status == "degraded" or bool(view.get("degraded"))
                    payload = self.processor.transcript(owner, external_id)
                    normalized = normalize_transcript_payload(
                        payload, duration_ms=job.duration_ms
                    )
                    self.workspace.update_media_job(
                        job.media_job_id, stage="Importing transcript", progress=0.98
                    )
                    with store.mutation_guard():
                        self._require_job_access(job)
                        transcript = self.workspace.import_media_transcript(
                            job.media_job_id,
                            segments=normalized,
                            warnings=(
                                payload.get("warnings")
                                if isinstance(payload.get("warnings"), list)
                                else []
                            ),
                            quality=(
                                payload.get("quality")
                                if isinstance(payload.get("quality"), dict)
                                else {}
                            ),
                            provenance=(
                                payload.get("provenance")
                                if isinstance(payload.get("provenance"), dict)
                                else {}
                            ),
                        )
                        segments = self.workspace.transcript_segments(
                            job.matter_id, job.document_id, job.source_version_id
                        )
                        self.project_transcript(matter, document, segments, degraded)
                    cleanup_message = ""
                    try:
                        self.processor.delete(owner, external_id)
                    except MediaProcessorError:
                        cleanup_message = (
                            " Transcript ready; temporary processor scratch will expire automatically."
                        )
                    self.workspace.finish_media_job(
                        job.media_job_id,
                        degraded=degraded,
                        message=(document.message + cleanup_message)[:240],
                        warnings=transcript.warnings,
                        quality=transcript.quality,
                        provenance=transcript.provenance,
                    )
                    return
                if self._stop.wait(self.poll_seconds):
                    return
            return
        except (MediaProcessorError, UploadProblem) as exc:
            message = str(exc)[:240]
        except Exception:
            message = "Transcription did not finish. Choose Try again."
        try:
            matter = self.resolve_matter(job.matter_id)
            self.resolve_store(matter).mark_failed(job.document_id, message)
        except Exception:
            pass
        try:
            self.workspace.fail_media_job(job.media_job_id, message)
        except Exception:
            pass
        if self.processor is not None and external_id:
            try:
                self.processor.delete(owner, external_id)
            except MediaProcessorError:
                pass

    def cancel_external(self, media_job: MediaJobRecord) -> bool:
        """Strictly remove processor-owned bytes for a matter purge."""

        if not media_job.external_job_id:
            return True
        if self.processor is None:
            return False
        owner = processor_owner(media_job.media_job_id)
        try:
            self.processor.cancel(owner, media_job.external_job_id)
        except MediaProcessorError:
            # A completed processor job may no longer accept cancellation; an
            # independently successful delete is still sufficient.
            pass
        try:
            self.processor.delete(owner, media_job.external_job_id)
        except MediaProcessorNotFound:
            return True
        except MediaProcessorError:
            return False
        return True

    def cancel_matter(self, matter_id: str, *, timeout: float = 5.0) -> bool:
        """Stop transient work before owned source bytes can be quarantined."""

        with self._state_condition:
            self._cancelled_matters.add(matter_id)
        external_cleanup_complete = True
        for media_job in self.workspace.media_jobs_for_matter(matter_id):
            if not self.cancel_external(media_job):
                external_cleanup_complete = False
        self._wake.set()
        self._summary_wake.set()
        deadline = time.monotonic() + max(float(timeout), 0)
        with self._state_condition:
            while (
                self._active_job is not None
                and self._active_job.matter_id == matter_id
            ) or (
                self._active_summary is not None
                and self._active_summary.matter_id == matter_id
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_condition.wait(timeout=remaining)
        return external_cleanup_complete


def format_timestamp(milliseconds: int, *, include_millis: bool = False) -> str:
    value = max(int(milliseconds), 0)
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    if include_millis:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass(frozen=True)
class MediaExport:
    body: bytes
    media_type: str
    suffix: str


@dataclass(frozen=True)
class _PortableTranscriptSegment:
    ordinal: int
    start_ms: int
    end_ms: int
    speaker: str
    speaker_status: str
    text: str

    @property
    def start(self) -> str:
        return format_timestamp(self.start_ms, include_millis=True).replace(",", ".")

    @property
    def end(self) -> str:
        return format_timestamp(self.end_ms, include_millis=True).replace(",", ".")


_RAW_PROCESSOR_SPEAKER = re.compile(
    r"(?:(?:speaker|spk)(?:[_-][a-z0-9]+|\s+\d+)|"
    r"unknown(?:[_-][a-z0-9]+)?)",
    re.IGNORECASE,
)


def _portable_transcript_segments(
    segments: Sequence[TranscriptSegmentRecord],
) -> tuple[_PortableTranscriptSegment, ...]:
    """Present speakers and transcript text without processor metadata."""

    first = segments[0]
    if any(
        item.matter_id != first.matter_id or item.transcript_id != first.transcript_id
        for item in segments
    ):
        raise ValueError("transcript contains mixed sources")
    speaker_labels = transcript_speaker_labels(segments)
    result: list[_PortableTranscriptSegment] = []
    for item in segments:
        display_name = " ".join(item.speaker_display_name.split()).strip()
        # A staff-confirmed label is authoritative even when its wording happens
        # to resemble a processor cluster. Only unconfirmed machine labels are
        # anonymized for portable exports.
        confirmed_name = item.speaker_identity_state == "confirmed" and bool(
            display_name
        )
        if confirmed_name:
            speaker = speaker_labels[item.speaker_cluster]
            speaker_status = "Confirmed"
        else:
            speaker = speaker_labels[item.speaker_cluster]
            speaker_status = "Unconfirmed"
        result.append(
            _PortableTranscriptSegment(
                ordinal=item.ordinal,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                speaker=speaker,
                speaker_status=speaker_status,
                text=item.current_text,
            )
        )
    return tuple(result)


def _portable_csv_value(value: object) -> str:
    text = presentation_text(str(value))
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def export_media_summary(
    source_name: str,
    summary: MediaSummaryRecord,
    segments: Sequence[TranscriptSegmentRecord],
    format_name: str,
) -> MediaExport:
    if summary.state != "ready":
        raise ValueError("transcript overview is not ready")
    if (
        not segments
        or any(
            segment.matter_id != summary.matter_id
            or segment.transcript_id != summary.transcript_id
            for segment in segments
        )
        or summary.basis_digest != transcript_summary_basis(segments)
    ):
        raise ValueError("transcript overview no longer matches this transcript")
    exact_locations = {
        (window.location, window.start_ms, window.end_ms, window.first_ordinal)
        for window in transcript_summary_windows(segments)
    }
    claims = summary.payload.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("transcript overview is empty")
    created = summary.finished_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    blocks: list[ExportBlock] = [
        ExportBlock(f"Transcript overview — {source_name}", "title"),
        ExportBlock(f"Generated by {PRODUCT_NAME} at {created}.", "metadata"),
    ]
    notice = summary.payload.get("evidence_notice")
    if isinstance(notice, str) and notice.strip():
        blocks.append(ExportBlock(notice.strip(), "note"))
    coverage = summary.payload.get("coverage")
    if isinstance(coverage, Mapping):
        mode = str(coverage.get("mode") or "representative")
        covered = int(coverage.get("covered_segment_count") or 0)
        total = int(coverage.get("total_segment_count") or 0)
        blocks.append(
            ExportBlock(
                (
                    f"Coverage: {covered} of {total} transcript passages"
                    + (" (representative sampling)" if mode != "full" else "")
                ),
                "metadata",
            )
        )
    for index, claim in enumerate(claims, 1):
        if not isinstance(claim, Mapping):
            raise ValueError("transcript overview contains an invalid point")
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("transcript overview contains an invalid point")
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations:
            raise ValueError("transcript overview point has no exact timestamp")
        prepared_locations: list[str] = []
        for citation in citations:
            if not isinstance(citation, Mapping):
                raise ValueError("transcript overview citation is invalid")
            location = citation.get("location")
            start_ms = citation.get("start_ms")
            end_ms = citation.get("end_ms")
            ordinal = citation.get("ordinal")
            if (
                not isinstance(location, str)
                or isinstance(start_ms, bool)
                or not isinstance(start_ms, int)
                or isinstance(end_ms, bool)
                or not isinstance(end_ms, int)
                or isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or (location, start_ms, end_ms, ordinal) not in exact_locations
            ):
                raise ValueError("transcript overview citation is invalid")
            prepared_locations.append(location)
        blocks.append(ExportBlock(f"{index}. Overview point", "heading1"))
        blocks.append(ExportBlock(text.strip()))
        for location in prepared_locations:
            blocks.append(ExportBlock(f"Transcript: {location}", "citation"))
    blocks.append(
        ExportBlock(
            "This overview is orientation from a machine-generated transcript. Review the recording and cited timestamps before relying on it.",
            "footer",
        )
    )
    kind = format_name.strip().casefold()
    if kind == "markdown":
        return MediaExport(
            blocks_to_markdown(blocks), "text/markdown; charset=utf-8", ".md"
        )
    if kind == "docx":
        return MediaExport(
            blocks_to_docx(
                blocks,
                title=f"Transcript overview — {source_name}",
                created_at=created,
            ),
            DOCX_MEDIA_TYPE,
            ".docx",
        )
    raise ValueError("unsupported transcript overview export")


def transcript_processing_notices(transcript: MediaTranscriptRecord | None) -> tuple[str, ...]:
    """Present bounded staff-facing outcomes, without exposing backend diagnostics."""
    if transcript is None:
        return ()
    warnings = [item for item in transcript.warnings[:20] if isinstance(item, Mapping)]
    status = transcript.quality.get("diarization_status")
    notices: list[str] = []
    if status == "failed" or any(item.get("code") == "speaker_attribution_unavailable" for item in warnings):
        notices.append(
            "Automatic speaker attribution failed. The transcript is available. "
            "Unreviewed speaker labels are placeholders, not detected speaker groupings. "
            "Check the recording before assigning names."
        )
    elif status == "skipped":
        notices.append(
            "Speaker attribution was skipped. Unreviewed speaker labels are placeholders; "
            "check the recording before assigning names."
        )
    for item in warnings:
        message = item.get("user_message")
        if isinstance(message, str) and message.strip():
            message = message.strip()[:1000]
            if message not in notices:
                notices.append(message)
    return tuple(notices)


def export_transcript(
    source_name: str,
    segments: Sequence[TranscriptSegmentRecord],
    format_name: str,
    *,
    notices: Sequence[str] = (),
) -> MediaExport:
    if not segments:
        raise ValueError("transcript is empty")
    portable_segments = _portable_transcript_segments(segments)
    kind = format_name.strip().casefold()
    if kind == "txt":
        lines = [f"Processing notice: {notice}" for notice in notices] + [
            f"[{item.start}–{item.end}] {item.speaker}: {item.text}"
            for item in portable_segments
        ]
        return MediaExport(presentation_text("\n".join(lines) + "\n").encode("utf-8"), "text/plain; charset=utf-8", ".txt")
    if kind == "markdown":
        # Keep the transcript's existing capacity and timestamp layout. The
        # generic block serializer has a lower whole-document size limit.
        lines = [f"# Transcript — {_markdown_escape(source_name)}", ""]
        lines.extend(f"**Processing notice:** {_markdown_escape(notice)}\n" for notice in notices)
        for item in portable_segments:
            lines.extend(
                [
                    f"**{item.start}–{item.end} · {_markdown_escape(item.speaker)} "
                    f"({item.speaker_status})**",
                    "",
                    _markdown_escape(item.text),
                    "",
                ]
            )
        markdown = "\n".join(lines)
        presented = presentation_text(markdown)
        if presented != markdown:
            presented += f"\n**Note:** {_TEXT_PRESENTATION_NOTICE}\n"
        return MediaExport(presented.encode("utf-8"), "text/markdown; charset=utf-8", ".md")
    if kind == "docx":
        created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        blocks: list[ExportBlock] = [
            ExportBlock(f"Transcript — {source_name}", "title"),
            ExportBlock(
                f"Exported from {PRODUCT_NAME} at {created}.", "metadata"
            ),
        ]
        blocks.extend(ExportBlock(f"Processing notice: {notice}") for notice in notices)
        for item in portable_segments:
            blocks.extend(
                (
                    ExportBlock(
                        f"{item.start}–{item.end} · {item.speaker} "
                        f"({item.speaker_status})",
                        "heading1",
                    ),
                    ExportBlock(item.text),
                )
            )
        blocks.append(
            ExportBlock(
                "Review this transcript against the recording before relying on it.",
                "footer",
            )
        )
        return MediaExport(
            blocks_to_docx(blocks, title=f"Transcript — {source_name}", created_at=created),
            DOCX_MEDIA_TYPE,
            ".docx",
        )
    if kind in {"srt", "vtt"}:
        blocks: list[str] = []
        for index, item in enumerate(portable_segments, 1):
            start = format_timestamp(item.start_ms, include_millis=True)
            end = format_timestamp(item.end_ms, include_millis=True)
            if kind == "vtt":
                start = start.replace(",", ".")
                end = end.replace(",", ".")
                blocks.append(f"{start} --> {end}\n{item.speaker}: {item.text}")
            else:
                blocks.append(f"{index}\n{start} --> {end}\n{item.speaker}: {item.text}")
        prefix = "WEBVTT\n\n" if kind == "vtt" else ""
        return MediaExport(
            presentation_text(prefix + "\n\n".join(blocks) + "\n").encode("utf-8"),
            "text/vtt; charset=utf-8" if kind == "vtt" else "application/x-subrip",
            ".vtt" if kind == "vtt" else ".srt",
        )
    if kind == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "Source",
                "Type",
                "Ordinal",
                "Start",
                "End",
                "Speaker",
                "Speaker status",
                "Text",
            ]
        )
        for item in portable_segments:
            writer.writerow(
                [
                    _portable_csv_value(source_name),
                    "Transcript",
                    item.ordinal,
                    item.start,
                    item.end,
                    _portable_csv_value(item.speaker),
                    item.speaker_status,
                    _portable_csv_value(item.text),
                ]
            )
        return MediaExport(output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", ".csv")
    if kind == "json":
        payload = {
            "source_name": source_name,
            "source_kind": "Transcript",
            "review_status": (
                "Staff edits saved"
                if any(item.current_revision or item.speaker_identity_state == "confirmed" for item in segments)
                else "Unconfirmed"
            ),
            "segments": [
                {
                    "ordinal": item.ordinal,
                    "start": item.start,
                    "end": item.end,
                    "speaker": item.speaker,
                    "speaker_status": item.speaker_status,
                    "text": item.text,
                }
                for item in portable_segments
            ],
        }
        if notices:
            payload["processing_notices"] = list(notices)
        return MediaExport(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "application/json",
            ".json",
        )
    raise ValueError("unsupported transcript export")


def render_clip(source: Path, clip: MediaClipRecord, *, has_video: bool, output: Path) -> None:
    """Materialize one exact bounded interval without retaining a second source."""

    duration = (clip.end_ms - clip.start_ms) / 1000
    start = clip.start_ms / 1000
    if duration < 1 or duration > 600:
        raise ValueError("clip duration is invalid")
    if source.is_symlink() or not source.is_file() or output.exists() or output.is_symlink():
        raise ValueError("clip path is unsafe")
    if has_video:
        command = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
    else:
        command = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=900,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.unlink(missing_ok=True)
        raise RuntimeError("The clip could not be rendered.") from exc
    if completed.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        raise RuntimeError("The clip could not be rendered.")
