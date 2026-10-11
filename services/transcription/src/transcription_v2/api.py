"""Private job API for the isolated, ephemeral transcription service."""

import hashlib
import json
import mimetypes
import secrets
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Mapping
from urllib.parse import quote

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .auth import AuthenticationError, authenticate, owner_key
from .batch_exports import (
    BATCH_ARCHIVE_ARTIFACT,
    BATCH_ARCHIVE_DOWNLOAD_NAME,
    BatchExportError,
    build_or_reuse_batch_archive,
)
from .delivery_tokens import DeliveryTokenSigner, InvalidDeliveryToken
from .domain import (
    ConcurrencyConflictError,
    IdentityMethod,
    IdentityStatus,
    InvalidTransitionError,
    Job,
    JobStatus,
    NotFoundError,
    PipelineStage,
    RetryLimitError,
    SpeakerMapping,
    TranscriptSegment,
    TranscriptionOptions,
    TERMINAL_JOB_STATUSES,
    validate_batch_id,
)
from .media import MediaProbeError, validate_media_name
from .profiles import DEFAULT_PROFILE_NAME, get_profile
from .profile_availability import ProfileAvailability
from .resources import queue_readiness
from .review_exports import ReviewExportError, refresh_review_exports
from .runtime import Runtime, build_runtime
from .settings import Settings
from .storage import FileTooLargeError, StorageError


MAX_OPTIONS_BYTES = 128 * 1024
MAX_SUMMARY_BYTES = 2 * 1024 * 1024
# Allow bounded multipart headers for the technical file-count ceiling and the
# options field without accepting another recording's worth of unparsed data.
MAX_MULTIPART_OVERHEAD_BYTES = MAX_OPTIONS_BYTES + 1024 * 1024


class SegmentPatch(BaseModel):
    text: str = Field(max_length=100_000)
    expected_revision: int = Field(ge=1)


class SpeakerPatch(BaseModel):
    display_name: str = Field(min_length=1, max_length=500)
    identity_state: str
    expected_revision: int = Field(ge=0)


class DeliveryPackageRequest(BaseModel):
    purge_after_download: bool = True


def _read_json(path: Path, *, maximum_bytes: int = MAX_SUMMARY_BYTES) -> dict[str, Any]:
    try:
        if path.stat().st_size > maximum_bytes:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _status_code(job: Job, summary: Mapping[str, Any]) -> str:
    if job.status is JobStatus.SUCCEEDED:
        return "degraded" if summary.get("degraded") else "review_ready"
    return {
        JobStatus.CREATED: "created",
        JobStatus.QUEUED: "queued",
        JobStatus.RUNNING: "running",
        JobStatus.CANCEL_REQUESTED: "canceling",
        JobStatus.CANCELED: "canceled",
        JobStatus.FAILED: "failed",
    }.get(job.status, job.status.value)


def _stage_view(job: Job) -> dict[str, Any]:
    order = list(PipelineStage)
    index = order.index(job.stage)
    code = {
        PipelineStage.INGEST: "preparing",
        PipelineStage.PROBE: "preparing",
        PipelineStage.TRANSCRIBE: "transcribing",
        PipelineStage.ALIGN: "aligning",
        PipelineStage.DIARIZE: "diarizing",
        PipelineStage.IDENTIFY: "identifying",
        PipelineStage.TRANSLATE: "translating",
        PipelineStage.QUALITY_CONTROL: "quality_control",
        PipelineStage.EXPORT: "exporting",
    }[job.stage]
    progress = (index + (1.0 if job.stage_status.value in {"succeeded", "skipped"} else 0.25)) / len(order)
    if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED}:
        progress = 1.0
    return {
        "code": code,
        "label": code.replace("_", " ").title(),
        "status": job.stage_status.value,
        "progress": min(1.0, progress),
    }


def _safe_user_message(job: Job, summary: Mapping[str, Any]) -> str | None:
    if job.status is JobStatus.FAILED:
        return {
            "worker_configuration": "The local worker is not ready for this job. An administrator can retry it after checking resources.",
            "gpu_resources_unavailable": "The reserved local GPU is temporarily unavailable. The service will retry without using another workload's GPU.",
            "local_io_error": "Local media or disk processing failed. The job may be retried.",
            "media_duration_exceeded": "This recording exceeds the 12-hour processing limit. Submit a shorter recording.",
            "pipeline_or_export_error": "The local transcription pipeline failed before a delivery package was ready.",
        }.get(job.error_code or "", "The job failed before a delivery package was ready.")
    warnings = summary.get("warnings")
    if summary.get("degraded") and isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, Mapping) and isinstance(warning.get("user_message"), str):
                return str(warning["user_message"])[:600]
    return None


def _summary(runtime: Runtime, job_id: str) -> dict[str, Any]:
    try:
        path = runtime.storage.job_paths(job_id, create=False).output / "ui-summary.json"
    except FileNotFoundError:
        return {}
    return _read_json(path)


def _job_view(runtime: Runtime, job: Job) -> dict[str, Any]:
    summary = _summary(runtime, job.id)
    files = runtime.store.list_files(job.id, job.owner_key)
    primary = files[0] if files else None
    status = _status_code(job, summary)
    human_reviewed = False
    if job.status is JobStatus.SUCCEEDED:
        segments = runtime.store.list_segments(job.id, job.owner_key)
        mappings = runtime.store.list_speaker_mappings(job.id, job.owner_key)
        human_reviewed = any(item.revision > 1 for item in segments) or any(
            item.revision > 1
            or item.identity_status is IdentityStatus.CONFIRMED
            for item in mappings
        )
    return {
        "id": job.id,
        "job_id": job.id,
        "batch_id": job.options.batch_id,
        "status": status,
        "stage": _stage_view(job),
        "profile": job.profile,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "primary_filename": primary.safe_name if primary else None,
        "size_bytes": primary.size_bytes if primary else None,
        "source_sha256": primary.sha256 if primary else None,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "expires_at": job.expires_at,
        "delete_at": job.expires_at,
        "degraded": bool(summary.get("degraded")),
        "warnings": summary.get("warnings") or [],
        "quality": summary.get("quality") or {},
        "provenance": summary.get("provenance") or {},
        "review_state": "Human reviewed" if human_reviewed else "Machine draft",
        "user_message": _safe_user_message(job, summary),
    }


def _speaker_view(mapping: SpeakerMapping | None, speaker_key: str) -> dict[str, Any]:
    if mapping is None:
        return {
            "cluster_id": speaker_key,
            "display_name": speaker_key,
            "identity_state": "cluster",
            "identity_method": "none",
            "confidence": None,
            "revision": 0,
        }
    state = {
        IdentityStatus.UNKNOWN: "cluster",
        IdentityStatus.SUGGESTED: "suggested",
        IdentityStatus.CONFIRMED: "confirmed",
    }[mapping.identity_status]
    return {
        "cluster_id": mapping.speaker_key,
        "display_name": mapping.display_name,
        "identity_state": state,
        "identity_method": mapping.identity_method.value,
        "confidence": mapping.confidence,
        "revision": mapping.revision,
    }


def _segment_view(segment: TranscriptSegment, speakers: Mapping[str, SpeakerMapping]) -> dict[str, Any]:
    speaker_key = segment.speaker_key or "UNKNOWN"
    return {
        "id": segment.id,
        "segment_id": segment.id,
        "start": segment.start_ms / 1000.0,
        "end": segment.end_ms / 1000.0,
        "text": segment.text,
        "model_text": segment.model_text,
        "translated_text": segment.translated_text,
        "confidence": segment.confidence,
        "low_confidence": segment.confidence is not None and segment.confidence < 0.75,
        "overlap": segment.overlap,
        "revision": segment.revision,
        "speaker": _speaker_view(speakers.get(speaker_key), speaker_key),
    }


def _normalized_options(raw: str, settings: Settings) -> tuple[str, TranscriptionOptions]:
    if len(raw.encode("utf-8")) > MAX_OPTIONS_BYTES:
        raise ValueError("job options are too large")
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("job options must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("job options must be an object")
    if "batch_id" in value:
        raise ValueError("batch_id is generated by the service")
    profile = str(value.pop("profile", DEFAULT_PROFILE_NAME))
    profile = get_profile(profile).name
    retention_value = value.get(
        "retention_hours", settings.default_retention_hours
    )
    if not isinstance(retention_value, int) or isinstance(retention_value, bool):
        raise ValueError("retention_hours must be an integer")
    retention = retention_value
    if retention > settings.max_retention_hours:
        raise ValueError("requested delivery window exceeds the service maximum")
    value["retention_hours"] = retention
    return profile, TranscriptionOptions.from_mapping(value)


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime = build_runtime(settings)
    profile_availability = ProfileAvailability(runtime.settings)
    signing_secret = hashlib.sha256(
        runtime.settings.api_token.encode("utf-8")
        if runtime.settings.api_token
        else secrets.token_bytes(32)
    ).digest()
    signer = DeliveryTokenSigner(signing_secret)
    consumed_delivery_tokens: dict[str, int] = {}
    consumed_delivery_lock = threading.Lock()
    upload_admission_lock = threading.Lock()
    batch_export_lock = threading.Lock()

    def delivery_url(token: str) -> str:
        return f"{runtime.settings.public_delivery_prefix}/{quote(token, safe='')}"

    def live_owned_job(job_id: str, owner: str) -> Job:
        """Fetch an owned job and enforce its delivery deadline on every route."""

        job = runtime.store.reconcile_job_retention(job_id, owner)
        if _expired(job.expires_at):
            runtime.store.delete_job(
                job.id,
                owner,
                delete_files=runtime.storage.delete_job_tree,
            )
            raise NotFoundError("job expired")
        return job

    def owned_batch_jobs(batch_id: str, owner: str) -> list[Job]:
        """Resolve a batch exclusively through the authenticated owner's jobs."""

        try:
            validated = validate_batch_id(batch_id)
        except ValueError as exc:
            raise NotFoundError("batch not found") from exc
        matches: list[Job] = []
        for candidate in runtime.store.list_jobs(owner, limit=1000):
            if candidate.options.batch_id != validated:
                continue
            try:
                matches.append(live_owned_job(candidate.id, owner))
            except NotFoundError:
                # Logical expiry removes the row and its transient files.
                continue
        if not matches:
            raise NotFoundError("batch not found")
        if len(matches) > runtime.settings.max_files_per_request:
            raise InvalidTransitionError("batch cardinality is invalid")
        return sorted(matches, key=lambda item: (item.created_at, item.id))

    def batch_view(batch_id: str, owner: str) -> dict[str, Any]:
        batch_jobs = owned_batch_jobs(batch_id, owner)
        succeeded = sum(job.status is JobStatus.SUCCEEDED for job in batch_jobs)
        failed = sum(job.status is JobStatus.FAILED for job in batch_jobs)
        canceled = sum(job.status is JobStatus.CANCELED for job in batch_jobs)
        active = sum(job.status not in TERMINAL_JOB_STATUSES for job in batch_jobs)
        all_terminal = active == 0
        if all_terminal and succeeded:
            batch_status = "ready"
        elif all_terminal:
            batch_status = "failed"
        else:
            batch_status = "processing"
        projected_jobs: list[dict[str, Any]] = []
        for job in batch_jobs:
            summary = _summary(runtime, job.id)
            files = runtime.store.list_files(job.id, owner)
            primary = files[0] if files else None
            projected_jobs.append(
                {
                    "job_id": job.id,
                    "primary_filename": primary.safe_name if primary else None,
                    "status": _status_code(job, summary),
                    "stage": _stage_view(job),
                }
            )
        return {
            "batch_id": batch_id,
            "status": batch_status,
            "all_terminal": all_terminal,
            "download_ready": all_terminal and succeeded > 0,
            "counts": {
                "total": len(batch_jobs),
                "active": active,
                "succeeded": succeeded,
                "failed": failed,
                "canceled": canceled,
            },
            "jobs": projected_jobs,
        }

    app = FastAPI(
        title="Exculpata Transcription v2",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.state.delivery_signer = signer

    async def owner_dependency(
        authorization: Annotated[str | None, Header()] = None,
        x_user_id: Annotated[str | None, Header()] = None,
    ) -> str:
        try:
            claimed = authenticate(
                configured_token=runtime.settings.api_token,
                authorization=authorization,
                claimed_owner=x_user_id,
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail="Authentication required") from exc
        return owner_key(claimed)

    Owner = Annotated[str, Depends(owner_dependency)]

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        declared_length: int | None = None
        if request.method == "POST" and request.url.path == "/v1/jobs":
            try:
                parsed_length = int(request.headers.get("content-length", ""))
                if parsed_length >= 0:
                    declared_length = parsed_length
            except ValueError:
                # Missing, malformed, or streaming lengths cannot be rejected
                # safely here; endpoint validation remains authoritative.
                pass
        if (
            declared_length is not None
            and declared_length
            > runtime.settings.max_batch_upload_bytes
            + MAX_MULTIPART_OVERHEAD_BYTES
        ):
            response = JSONResponse(
                status_code=413,
                content={"code": "batch_too_large"},
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        return response

    @app.exception_handler(NotFoundError)
    async def not_found(_request: Request, _exc: NotFoundError):
        return JSONResponse(status_code=404, content={"code": "not_found"})

    @app.exception_handler(ConcurrencyConflictError)
    async def conflict(_request: Request, _exc: ConcurrencyConflictError):
        return JSONResponse(status_code=409, content={"code": "revision_conflict"})

    @app.exception_handler(InvalidTransitionError)
    @app.exception_handler(RetryLimitError)
    async def invalid_transition(_request: Request, _exc: Exception):
        return JSONResponse(status_code=409, content={"code": "invalid_job_state"})

    @app.exception_handler(ValueError)
    async def invalid_value(_request: Request, _exc: ValueError):
        return JSONResponse(status_code=422, content={"code": "invalid_request"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request_shape(_request: Request, _exc: RequestValidationError):
        # FastAPI's default detail can echo submitted transcript corrections or
        # speaker labels. Return only a content-free validation code.
        return JSONResponse(status_code=422, content={"code": "invalid_request"})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "transcription-v2",
            "pipeline_backend": runtime.settings.pipeline_backend,
            "ephemeral_delivery": True,
        }

    @app.get("/ready")
    def ready(_owner: Owner) -> dict[str, object]:
        return queue_readiness(runtime.settings)

    @app.get("/v1/profiles")
    def profiles(_owner: Owner) -> dict[str, Any]:
        return {"profiles": profile_availability.public_profiles(),
                "languages": profile_availability.languages()}

    @app.post("/v1/jobs", status_code=201)
    def submit_jobs(
        owner: Owner,
        files: Annotated[list[UploadFile], File()],
        options: Annotated[str, Form()] = "{}",
    ) -> dict[str, Any]:
        if not files:
            raise HTTPException(status_code=400, detail="No media files supplied")
        if len(files) > runtime.settings.max_files_per_request:
            raise HTTPException(status_code=400, detail="Too many files")
        try:
            profile, job_options = _normalized_options(options, runtime.settings)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid job options") from exc

        if not profile_availability.available(
            get_profile(profile), translate=job_options.translate_to_english,
        ):
            return JSONResponse(status_code=409, content={"code": "profile_unavailable"})
        languages = profile_availability.languages()
        if not languages or (job_options.source_language != "auto" and job_options.source_language not in languages):
            return JSONResponse(status_code=409, content={"code": "language_unavailable"})

        declared_sizes: list[int] = []
        for upload in files:
            candidate = getattr(upload, "size", None)
            if (
                not isinstance(candidate, int)
                or isinstance(candidate, bool)
                or candidate < 0
            ):
                candidate = runtime.settings.max_upload_bytes
            if candidate > runtime.settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="A media file is too large")
            declared_sizes.append(candidate)
        declared_batch_size = sum(declared_sizes)
        if declared_batch_size > runtime.settings.max_batch_upload_bytes:
            raise HTTPException(status_code=413, detail="Upload batch is too large")

        # Keep the capacity snapshot and every child ingest/enqueue in one
        # process-local critical section. Otherwise two batches can both pass
        # preflight and then consume the last slots between one another's
        # children, turning a capacity rejection into a partial batch. This
        # intentionally serializes local disk ingestion (the request body has
        # already been parsed/spooled by Starlette); a competing request may
        # therefore wait for one complete, at-most-5-GiB batch. The UI client's
        # one-hour upload timeout accommodates that bounded head-of-line wait.
        # Do not add API worker processes or raise the byte ceiling without a
        # shared reservation mechanism and a fresh timeout/capacity review.
        with upload_admission_lock:
            owner_jobs, owner_bytes = runtime.store.retained_usage(owner)
            global_jobs, global_bytes = runtime.store.retained_usage()
            if (
                owner_jobs + len(files)
                > runtime.settings.max_retained_jobs_per_owner
            ):
                raise HTTPException(
                    status_code=429,
                    detail="Owner job capacity is unavailable",
                )
            if (
                owner_bytes + declared_batch_size
                > runtime.settings.max_retained_bytes_per_owner
            ):
                raise HTTPException(
                    status_code=429,
                    detail="Owner byte capacity is unavailable",
                )
            if global_jobs + len(files) > runtime.settings.max_retained_jobs_global:
                raise HTTPException(
                    status_code=507,
                    detail="Service job capacity is unavailable",
                )
            if (
                global_bytes + declared_batch_size
                > runtime.settings.max_retained_bytes_global
            ):
                raise HTTPException(
                    status_code=507,
                    detail="Service byte capacity is unavailable",
                )
            try:
                free_bytes = shutil.disk_usage(runtime.settings.data_root).free
            except OSError as exc:
                raise HTTPException(
                    status_code=507,
                    detail="Storage capacity is unavailable",
                ) from exc
            if (
                free_bytes - declared_batch_size
                < runtime.settings.minimum_free_disk_bytes
            ):
                raise HTTPException(
                    status_code=507,
                    detail="Storage capacity is unavailable",
                )

            batch_id = f"batch_{secrets.token_hex(16)}" if len(files) > 1 else None
            persisted_options = (
                replace(job_options, batch_id=batch_id)
                if batch_id is not None
                else job_options
            )

            accepted: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            for index, upload in enumerate(files):
                try:
                    validate_media_name(upload.filename or "upload.bin")
                except MediaProbeError:
                    rejected.append({"index": index, "code": "unsupported_media"})
                    continue

                # Retain the definitive child guards for state changes outside
                # this single API process, ingest-size mismatches, and future
                # storage implementations even though ordinary submissions are
                # serialized by upload_admission_lock.
                owner_jobs, owner_bytes = runtime.store.retained_usage(owner)
                global_jobs, global_bytes = runtime.store.retained_usage()
                declared_size = getattr(upload, "size", None)
                declared_size = (
                    int(declared_size)
                    if isinstance(declared_size, int) and not isinstance(declared_size, bool)
                    else None
                )
                if declared_size is not None and declared_size > runtime.settings.max_upload_bytes:
                    rejected.append({"index": index, "code": "file_too_large"})
                    continue
                projected_size = (
                    declared_size
                    if declared_size is not None and declared_size > 0
                    else runtime.settings.max_upload_bytes
                )
                if (
                    owner_jobs >= runtime.settings.max_retained_jobs_per_owner
                    or owner_bytes + projected_size
                    > runtime.settings.max_retained_bytes_per_owner
                ):
                    rejected.append({"index": index, "code": "owner_quota"})
                    continue
                if (
                    global_jobs >= runtime.settings.max_retained_jobs_global
                    or global_bytes + projected_size
                    > runtime.settings.max_retained_bytes_global
                ):
                    rejected.append({"index": index, "code": "service_capacity"})
                    continue
                try:
                    free_bytes = shutil.disk_usage(runtime.settings.data_root).free
                except OSError:
                    rejected.append({"index": index, "code": "service_capacity"})
                    continue
                if (
                    free_bytes - projected_size
                    < runtime.settings.minimum_free_disk_bytes
                ):
                    rejected.append({"index": index, "code": "insufficient_storage"})
                    continue

                job = runtime.store.create_job(
                    owner_key=owner,
                    options=persisted_options,
                    profile=profile,
                )
                try:
                    ingested = runtime.storage.ingest_stream(
                        job.id,
                        upload.file,
                        upload.filename or "upload.bin",
                        media_type=upload.content_type,
                        max_bytes=runtime.settings.max_upload_bytes,
                    )
                    runtime.store.register_file(
                        job.id,
                        owner_key=owner,
                        original_name=ingested.original_name,
                        safe_name=ingested.safe_name,
                        relative_path=ingested.relative_path,
                        media_type=ingested.media_type,
                        size_bytes=ingested.size_bytes,
                        sha256=ingested.sha256,
                    )
                    queued = runtime.store.enqueue_job(job.id, owner)
                    accepted.append(_job_view(runtime, queued))
                except FileTooLargeError:
                    runtime.store.delete_job(
                        job.id,
                        owner,
                        delete_files=runtime.storage.delete_job_tree,
                        include_batch=False,
                    )
                    rejected.append({"index": index, "code": "file_too_large"})
                except (OSError, StorageError, ValueError):
                    runtime.store.delete_job(
                        job.id,
                        owner,
                        delete_files=runtime.storage.delete_job_tree,
                        include_batch=False,
                    )
                    rejected.append({"index": index, "code": "ingest_failed"})
        if not accepted:
            rejection_codes = {item["code"] for item in rejected}
            if rejection_codes == {"file_too_large"}:
                code = 413
            elif rejection_codes == {"unsupported_media"}:
                code = 415
            elif "owner_quota" in rejection_codes:
                code = 429
            elif rejection_codes & {"service_capacity", "insufficient_storage"}:
                code = 507
            else:
                code = 400
            raise HTTPException(status_code=code, detail="No files were accepted")
        return {"jobs": accepted, "rejected": rejected, "batch_id": batch_id}

    @app.get("/v1/jobs")
    def jobs(owner: Owner, limit: int = Query(default=100, ge=1, le=200)) -> dict[str, Any]:
        # Purge on reads as a backstop if the worker is temporarily stopped.
        runtime.store.purge_expired(delete_files=runtime.storage.delete_job_tree, limit=100)
        runtime.store.purge_stale_unfinished(
            stale_before=datetime.now(timezone.utc)
            - timedelta(hours=runtime.settings.max_active_job_hours),
            delete_files=runtime.storage.delete_job_tree,
            limit=100,
        )
        return {"jobs": [_job_view(runtime, job) for job in runtime.store.list_jobs(owner, limit=limit)]}

    @app.get("/v1/batches/{batch_id}")
    def batch_status(batch_id: str, owner: Owner) -> dict[str, Any]:
        return batch_view(batch_id, owner)

    @app.get("/v1/jobs/{job_id}")
    def job_detail(job_id: str, owner: Owner) -> dict[str, Any]:
        job = live_owned_job(job_id, owner)
        return _job_view(runtime, job)

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, owner: Owner) -> dict[str, Any]:
        live_owned_job(job_id, owner)
        return _job_view(runtime, runtime.store.request_cancel(job_id, owner))

    @app.post("/v1/jobs/{job_id}/retry")
    def retry_job(job_id: str, owner: Owner) -> dict[str, Any]:
        live_owned_job(job_id, owner)
        return _job_view(runtime, runtime.store.retry_job(job_id, owner))

    @app.delete("/v1/jobs/{job_id}")
    def delete_job(job_id: str, owner: Owner) -> dict[str, Any]:
        job = live_owned_job(job_id, owner)
        if job.status in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}:
            runtime.store.request_cancel(job_id, owner, reason_code="owner_delete")
            return {"status": "deletion_pending", "job_id": job_id}
        runtime.store.delete_job(job_id, owner, delete_files=runtime.storage.delete_job_tree)
        return {"status": "deleted", "job_id": job_id}

    @app.get("/v1/jobs/{job_id}/transcript")
    def transcript(job_id: str, owner: Owner) -> dict[str, Any]:
        job = live_owned_job(job_id, owner)
        if job.status is not JobStatus.SUCCEEDED:
            raise InvalidTransitionError("transcript is not ready")
        mappings = {
            item.speaker_key: item
            for item in runtime.store.list_speaker_mappings(job_id, owner)
        }
        segments = runtime.store.list_segments(job_id, owner)
        summary = _summary(runtime, job_id)
        files = runtime.store.list_files(job_id, owner)
        media_url = None
        if files:
            token, _ = signer.issue(
                job_id=job_id,
                owner_key=owner,
                artifact=f"@source:{files[0].id}",
                lifetime_seconds=runtime.settings.delivery_token_seconds,
            )
            media_url = delivery_url(token)
        human_reviewed = any(item.revision > 1 for item in segments) or any(
            item.revision > 1
            or item.identity_status is IdentityStatus.CONFIRMED
            for item in mappings.values()
        )
        return {
            "job_id": job_id,
            "revision": sum(item.revision for item in segments)
            + sum(item.revision for item in mappings.values()),
            "review_state": "Human reviewed" if human_reviewed else "Machine draft",
            "segments": [_segment_view(item, mappings) for item in segments],
            "speakers": [_speaker_view(item, item.speaker_key) for item in mappings.values()],
            "roster": list(job.options.roster),
            "media_url": media_url,
            "warnings": summary.get("warnings") or [],
            "quality": summary.get("quality") or {},
            "provenance": summary.get("provenance") or {},
        }

    @app.patch("/v1/jobs/{job_id}/segments/{segment_id}")
    def update_segment(job_id: str, segment_id: str, patch: SegmentPatch, owner: Owner) -> dict[str, Any]:
        live_owned_job(job_id, owner)
        available = {item.id for item in runtime.store.list_segments(job_id, owner)}
        if segment_id not in available:
            raise NotFoundError("segment not found")
        segment = runtime.store.update_segment(
            segment_id,
            owner,
            patch.expected_revision,
            edited_text=patch.text,
        )
        export_refresh_pending = False
        try:
            refresh_review_exports(runtime, job_id, owner)
        except ReviewExportError:
            # The optimistic edit is already durable. Do not tell the user it
            # failed merely because packaging needs a later retry.
            export_refresh_pending = True
        mappings = {
            item.speaker_key: item
            for item in runtime.store.list_speaker_mappings(job_id, owner)
        }
        response = _segment_view(segment, mappings)
        response["export_refresh_pending"] = export_refresh_pending
        return response

    @app.patch("/v1/jobs/{job_id}/speakers/{speaker_key}")
    def update_speaker(job_id: str, speaker_key: str, patch: SpeakerPatch, owner: Owner) -> dict[str, Any]:
        live_owned_job(job_id, owner)
        if patch.identity_state not in {"cluster", "confirmed"}:
            raise HTTPException(status_code=422, detail="Invalid identity state")
        status = (
            IdentityStatus.CONFIRMED
            if patch.identity_state == "confirmed"
            else IdentityStatus.UNKNOWN
        )
        mapping = runtime.store.set_speaker_mapping(
            job_id,
            owner,
            speaker_key,
            patch.display_name,
            identity_status=status,
            identity_method=IdentityMethod.MANUAL,
            expected_revision=patch.expected_revision,
        )
        export_refresh_pending = False
        try:
            refresh_review_exports(runtime, job_id, owner)
        except ReviewExportError:
            export_refresh_pending = True
        response = _speaker_view(mapping, speaker_key)
        response["export_refresh_pending"] = export_refresh_pending
        return response

    @app.get("/v1/jobs/{job_id}/exports")
    def exports(job_id: str, owner: Owner) -> dict[str, Any]:
        job = live_owned_job(job_id, owner)
        if job.status is not JobStatus.SUCCEEDED:
            raise InvalidTransitionError("exports are not ready")
        try:
            refresh_review_exports(runtime, job_id, owner)
        except ReviewExportError as exc:
            raise InvalidTransitionError("reviewed exports are unavailable") from exc
        output = runtime.storage.job_paths(job_id, create=False).output
        items: list[dict[str, Any]] = []
        for path in sorted(output.iterdir()):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.name.startswith(".")
                or path.name == "ui-summary.json"
            ):
                continue
            token, _ = signer.issue(
                job_id=job_id,
                owner_key=owner,
                artifact=path.name,
                lifetime_seconds=runtime.settings.delivery_token_seconds,
            )
            items.append(
                {
                    "name": path.name,
                    "label": path.suffix.lstrip(".") or "file",
                    "format": path.suffix.lstrip("."),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    "download_url": delivery_url(token),
                }
            )
        return {"exports": items}

    @app.post("/v1/jobs/{job_id}/delivery-package")
    def delivery_package(job_id: str, request: DeliveryPackageRequest, owner: Owner) -> dict[str, Any]:
        job = live_owned_job(job_id, owner)
        if job.status is not JobStatus.SUCCEEDED:
            raise InvalidTransitionError("delivery package is not ready")
        try:
            refresh_review_exports(runtime, job_id, owner)
        except ReviewExportError as exc:
            raise InvalidTransitionError("reviewed exports are unavailable") from exc
        output = runtime.storage.job_paths(job_id, create=False).output
        packages = sorted(
            path
            for path in output.glob("*.delivery.zip")
            if path.is_file() and not path.is_symlink()
        )
        if not packages:
            raise InvalidTransitionError("delivery package is unavailable")
        package = packages[0]
        token, expires_at = signer.issue(
            job_id=job_id,
            owner_key=owner,
            artifact=package.name,
            purge_after_delivery=request.purge_after_download,
            lifetime_seconds=runtime.settings.delivery_token_seconds,
        )
        return {
            "download_url": delivery_url(token),
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            "purge_after_download": request.purge_after_download,
        }

    @app.post("/v1/batches/{batch_id}/delivery-package")
    def batch_delivery_package(
        batch_id: str,
        request: DeliveryPackageRequest,
        owner: Owner,
    ) -> dict[str, Any]:
        batch_jobs = owned_batch_jobs(batch_id, owner)
        if any(job.status not in TERMINAL_JOB_STATUSES for job in batch_jobs):
            raise InvalidTransitionError("batch delivery is not ready")
        succeeded_jobs = [job for job in batch_jobs if job.status is JobStatus.SUCCEEDED]
        if not succeeded_jobs:
            raise InvalidTransitionError("batch has no completed transcript")
        try:
            with batch_export_lock:
                archive = build_or_reuse_batch_archive(
                    runtime,
                    owner_key=owner,
                    batch_id=batch_id,
                    succeeded_jobs=succeeded_jobs,
                    omitted_jobs=len(batch_jobs) - len(succeeded_jobs),
                )
        except BatchExportError as exc:
            raise InvalidTransitionError("batch delivery is unavailable") from exc
        token, expires_at = signer.issue(
            job_id=archive.anchor_job_id,
            owner_key=owner,
            artifact=BATCH_ARCHIVE_ARTIFACT,
            purge_after_delivery=request.purge_after_download,
            lifetime_seconds=runtime.settings.delivery_token_seconds,
        )
        return {
            "batch_id": batch_id,
            "download_url": delivery_url(token),
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
            "purge_after_download": request.purge_after_download,
            "filename": BATCH_ARCHIVE_DOWNLOAD_NAME,
            "included_jobs": archive.included_jobs,
            "omitted_jobs": archive.omitted_jobs,
        }

    @app.get("/v1/delivery/{token}", include_in_schema=False)
    def deliver(token: str, request: Request):
        try:
            grant = signer.verify(token)
            job = live_owned_job(grant.job_id, grant.owner_key)
        except (InvalidDeliveryToken, NotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Delivery link is invalid or expired") from exc

        # A purge grant represents one complete package handoff.  Byte-range
        # requests (including a one-byte probe) must never trigger deletion.
        if grant.purge_after_delivery and request.headers.get("range"):
            raise HTTPException(
                status_code=416,
                detail="Range requests are unavailable for download-and-delete links",
            )

        batch_purge_snapshot: str | None = None
        if grant.artifact == BATCH_ARCHIVE_ARTIFACT:
            batch_id = job.options.batch_id
            if batch_id is None:
                raise HTTPException(status_code=404, detail="Delivery artifact is unavailable")
            try:
                # A signed URL proves scope, not archive freshness. Rebuild or
                # validate against current reviewed exports and the current
                # succeeded set immediately before streaming or purging.
                with batch_export_lock:
                    for _attempt in range(3):
                        before_snapshot = runtime.store.batch_state_snapshot(
                            job.id, grant.owner_key
                        )
                        batch_jobs = owned_batch_jobs(batch_id, grant.owner_key)
                        if any(
                            item.status not in TERMINAL_JOB_STATUSES
                            for item in batch_jobs
                        ):
                            raise BatchExportError("batch changed before delivery")
                        succeeded_jobs = [
                            item
                            for item in batch_jobs
                            if item.status is JobStatus.SUCCEEDED
                        ]
                        if not succeeded_jobs:
                            raise BatchExportError("batch has no successful output")
                        archive = build_or_reuse_batch_archive(
                            runtime,
                            owner_key=grant.owner_key,
                            batch_id=batch_id,
                            succeeded_jobs=succeeded_jobs,
                            omitted_jobs=len(batch_jobs) - len(succeeded_jobs),
                        )
                        after_snapshot = runtime.store.batch_state_snapshot(
                            job.id, grant.owner_key
                        )
                        if secrets.compare_digest(before_snapshot, after_snapshot):
                            batch_purge_snapshot = after_snapshot
                            break
                    else:
                        raise BatchExportError("batch kept changing before delivery")
            except (BatchExportError, NotFoundError) as exc:
                raise HTTPException(
                    status_code=404, detail="Delivery artifact is unavailable"
                ) from exc
            if archive.anchor_job_id != job.id:
                raise HTTPException(status_code=404, detail="Delivery artifact is unavailable")
            path = archive.path
            download_name = BATCH_ARCHIVE_DOWNLOAD_NAME
            media_type = "application/zip"
        elif grant.artifact.startswith("@source:"):
            file_id = grant.artifact.partition(":")[2]
            job_files = runtime.store.list_files(job.id, grant.owner_key)
            item = next((candidate for candidate in job_files if candidate.id == file_id), None)
            if item is None:
                raise HTTPException(status_code=404, detail="Delivery artifact is unavailable")
            path = runtime.storage.resolve_relative(item.relative_path)
            input_root = runtime.storage.job_paths(
                job.id, create=False
            ).input.resolve()
            if not path.is_relative_to(input_root):
                raise HTTPException(status_code=404, detail="Delivery artifact is unavailable")
            download_name = item.safe_name
            media_type = item.media_type or mimetypes.guess_type(item.safe_name)[0]
        else:
            if Path(grant.artifact).name != grant.artifact:
                raise HTTPException(status_code=404, detail="Delivery artifact is unavailable")
            output = runtime.storage.job_paths(job.id, create=False).output.resolve()
            candidate = output / grant.artifact
            if candidate.is_symlink() or not candidate.is_file():
                raise HTTPException(status_code=404, detail="Delivery artifact is unavailable")
            path = candidate.resolve()
            if path.parent != output:
                raise HTTPException(status_code=404, detail="Delivery artifact is unavailable")
            download_name = grant.artifact
            media_type = mimetypes.guess_type(grant.artifact)[0]

        background = None
        if grant.purge_after_delivery:
            token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            with consumed_delivery_lock:
                now = int(time.time())
                for digest, expiry in list(consumed_delivery_tokens.items()):
                    if expiry < now:
                        consumed_delivery_tokens.pop(digest, None)
                if token_digest in consumed_delivery_tokens:
                    raise HTTPException(status_code=404, detail="Delivery link was already used")
                consumed_delivery_tokens[token_digest] = grant.expires_at
            if batch_purge_snapshot is not None:
                background = BackgroundTask(
                    runtime.store.delete_batch_if_unchanged,
                    job.id,
                    grant.owner_key,
                    batch_purge_snapshot,
                    delete_files=runtime.storage.delete_job_tree,
                )
            else:
                background = BackgroundTask(
                    runtime.store.delete_job,
                    job.id,
                    grant.owner_key,
                    delete_files=runtime.storage.delete_job_tree,
                )
        response_headers = {"Cache-Control": "no-store"}
        if grant.purge_after_delivery:
            response_headers["Accept-Ranges"] = "none"
        return FileResponse(
            path,
            filename=download_name,
            media_type=media_type or "application/octet-stream",
            background=background,
            headers=response_headers,
        )

    return app


app = create_app()


__all__ = ["app", "create_app"]
