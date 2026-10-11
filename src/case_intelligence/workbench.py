"""Milestone A staff workbench: matters, uploads, retrieval, generation, support."""
from __future__ import annotations

from .review_budget import DEFAULT_REVIEW_BUDGET, ReviewBudget, validate_primary_limit
from .investigation_planner import PLANNER_VERSION, initial_query, propose_searches, query_key, validate_proposal
from time import monotonic

import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import re
import stat
import sqlite3
import tempfile
import threading
import urllib.request
import uuid
from contextlib import asynccontextmanager, closing
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse
from zoneinfo import ZoneInfo

import anyio
import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.concurrency import run_in_threadpool

from .compat_env import env, header, header_conflicts, kerberos_headers, response_headers
from .static_assets import asset_url, static_cache_control
from .source_find import MAX_FIND_CHARS, find_in_source, highlight_find
from .answer_jobs import AnswerCoordinator, AnswerJobFailure, AnswerResult
from .answer_presentation import GENERATED_REVIEW_NOTICE, answer_content, answer_introduction, answer_limitation, answer_failure_notice, modality_coverage_notice, research_content, rejected_answer_content, rejected_answer_notice, review_rejection_notice, REJECTED_ANSWER_NOTICE
from .branding import PRODUCT_DESCRIPTION, PRODUCT_NAME, PRODUCT_TAGLINE
from .pdf_coverage import PDF_SAVED_RESULT_NOTICE
from .exact_search import QuerySyntaxError, parse_query
from .exact_search_results import (
    ExactSearchBackend, ExactSearchChanged, ExactSearchPage, ExactSearchUnavailable, ReferenceExactSearchBackend,
    build_search_query,
)
from .identity import (
    KERBEROS_SECRET_HEADER,
    KERBEROS_USER_HEADER,
    LOGIN_CHALLENGE_COOKIE,
    OIDC_STATE_COOKIE,
    SESSION_COOKIE,
    AuthContext,
    IdentityService,
    KerberosAuthenticationError,
    KerberosSettings,
    LocalAccountSettings,
    LocalAuthenticationError,
    local_principal_enabled,
    resolve_auth_mode,
    OidcAuthenticationError,
    OidcProviderClient,
    OidcSettings,
)
from .full_text_review import FullTextReviewLedger, iter_text_export, review_export_snapshot
from .text_ledger_response import TextLedgerStreamingResponse
from .generation import (
    EvidenceItem,
    GenerationGroundingRejected,
    GenerationRejected,
    GenerationUnavailable,
    GeneratorClient,
    GroundedGenerationService,
    VerifiedAnswer,
    generator_from_environment,
)
from .ingestion import IngestionCoordinator
from .report_review_basis import research_sections, review_sections
from .local_account_admin import register_local_account_routes
from .report_compilation import CompilationProblem
from .report_compilation_flow import ReportCompilationFlow
from .media_evidence import (
    MediaCoordinator,
    MediaExport,
    MediaProcessor,
    TranscriptSummaryResult,
    TranscriptionV2Client,
    export_media_summary,
    export_transcript,
    format_timestamp,
    render_clip,
    transcript_summary_basis,
    transcript_summary_windows,
    transcript_speaker_labels,
    transcript_processing_notices,
    transcript_units,
)
from .managed_storage import ManagedMatterStorage, StoragePolicy, format_bytes
from .malware_scan import MalwareScanner, scanner_from_environment, scanner_status
from .loose_file_preflight import evaluate_loose_file_preflight
from .intake_receipts import IntakeReceipts, IntakeUploadResumeMismatch
from .intake_receipt_exports import export_intake_receipt, LABELS as INTAKE_LABELS
from .matter_analysis import MAX_ANALYSIS_UNITS, analyze_candidates
from .media_playback import BrowserPlaybackCoordinator
from .model_portfolio import model_portfolio_projection
from .pilot_uploads import (
    AUDIO_MEDIA_TYPES,
    DOCX_MEDIA_TYPE,
    EMAIL_MEDIA_TYPES,
    IMAGE_MEDIA_TYPES,
    MEDIA_TYPES,
    SPREADSHEET_MEDIA_TYPES,
    VIDEO_MEDIA_TYPES,
    MAX_FILES,
    MAX_UPLOAD_CHUNK_BYTES,
    MAX_UPLOAD_INTAKE_ITEMS,
    MAX_UPLOAD_SESSION_ITEMS,
    PilotDocument,
    PilotStore,
    PilotStoreChange,
    PilotUnit,
    RawUploadLimitMiddleware,
    UploadProblem,
    canonical_media_type,
    is_media_type,
    maximum_file_bytes,
    media_kind,
    securely_delete_owned_tree,
)
from .review_bench import RetrievalUnavailable
from .review_bench_v2 import (
    Candidate,
    DeterministicEmbeddingAdapter,
    DeterministicReranker,
    HybridRetriever,
    InMemoryHybridBackend,
    PostgresHybridBackend,
    RemoteEmbeddingAdapter,
    RemoteRerankerAdapter,
    apply_postgres_migrations,
    delete_postgres_document,
    delete_postgres_matter,
    retain_postgres_workbench_matters,
    upsert_postgres_document,
)
from .review_quality import (
    broad_summary_queries,
    classify_question,
    filter_broad_summary_evidence,
    modality_coverage,
    prioritize_evidence_kinds,
    research_step_question,
    research_synthesis_question,
)
from .source_locations import (
    SourceLocationProblem,
    SourceLocationRegistry,
    source_registry_from_environment,
)
from .service_endpoints import validate_service_endpoint
from .workspace_store import (
    AUTOMATIC_DISCOVERY_PRINCIPAL,
    AnswerJobRecord,
    ConversationRecord,
    MatterLifecycleRecord,
    MatterReadinessRecord,
    MatterRecord,
    MatterNameConflict,
    MatterRetentionRecord,
    MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS,
    MediaClipRecord,
    MediaJobRecord,
    MediaSummaryRecord,
    MediaTranscriptRecord,
    MessageRecord,
    NOTEBOOK_STATUSES,
    NOTEBOOK_TYPES,
    NotebookItemRecord,
    NotebookReferenceRecord,
    ReportCitationRecord,
    ReportRecord,
    ReportSectionRecord,
    ResearchJobRecord,
    ReviewDecisionRecord,
    ReviewRunRecord,
    SourceCatalogRecord,
    SourceCollectionRecord,
    SourceSetRecord,
    SpeakerMappingRecord,
    TranscriptSegmentRecord,
    UploadItemRecord,
    WorkspaceProblem,
    ReportEditConflict,
    ReviewDecisionConflict,
    NotebookEditConflict,
    WorkspaceStore,
    is_full_text_synthesis,
)
from .workflow_jobs import (
    ResearchCoordinator,
    ReviewCoordinator,
    ReviewDecisionResult,
    WorkflowFailure,
)
from .work_product_exports import (
    ExportArtifact,
    ExportProblem,
    MAX_BUNDLE_UNCOMPRESSED_BYTES,
    export_answer,
    export_conversation,
    export_full_review,
    export_matter_bundle,
    export_notebook,
    export_research,
    validate_research_basis,
    export_report,
    safe_file_stem,
)

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_RUNTIME = PROJECT_ROOT / ".tmp/milestone-a-workbench"
MAX_SEARCH_CHARS = 512
MAX_QUESTION_CHARS = 2_000
MAX_FINAL_BUNDLE_LEDGER_ITEMS = 500
MAX_FINAL_BUNDLE_REPORTS = 500
MAX_FINAL_BUNDLE_REPORT_ROWS = 10_000
MAX_FINAL_BUNDLE_REPORT_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_PREFLIGHT_REQUEST_BYTES = 6 * 1024 * 1024
# One synthesis validation pass includes every frozen ready source, even when
# its review inventory is unsealed. These are aggregate, not per-source limits.
MAX_SYNTHESIS_SCAN_CHARS = 10_000_000
MAX_SYNTHESIS_SCAN_UNITS = 20_000
MAX_SYNTHESIS_SCAN_SERIALIZED_CHARS = 128_000_000
MAX_SYNTHESIS_SCAN_RECORD_CHARS = 120_065_536
MAX_SYNTHESIS_SCAN_SECONDS = 5.0
_SYNTHESIS_SCAN_LIMIT = (
    "The frozen review exceeds the synthesis source-validation limit. "
    "No synthesis result was admitted or released. Use the saved review and originals."
)
_WORD = re.compile(r"[a-z0-9]+")
_FOLLOWUP_WORDS = {"it", "that", "those", "they", "them", "this", "these", "he", "she", "there"}
_COLLECTION_WIDE_QUESTION = re.compile(
    r"\b(?:all|each|every|complete|comprehensive|entire|exhaustive)\b.{0,80}"
    r"\b(?:document|documents|record|records|source|sources|mention|mentions|"
    r"reference|references|chronology|timeline|list)\b|"
    r"\b(?:list|identify|find|show)\s+(?:me\s+)?(?:all|each|every)\b|"
    r"\b(?:complete|comprehensive|full)\s+(?:chronology|timeline|list|account)\b",
    re.IGNORECASE,
)
_MATTER_PURGE_ID = re.compile(r"^matter-purge-[0-9a-f]{32}$")
_DATE_REFERENCE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+[0-3]?\d(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b|"
    r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:\d{2}|\d{4})\b"
)
_PERSON_REFERENCE = re.compile(
    r"\b(?:Officer|Detective|Det\.|Agent|Sergeant|Sgt\.|Lieutenant|Lt\.|"
    r"Captain|Capt\.|Dr\.|Mr\.|Mrs\.|Ms\.|Judge|Attorney)\s+"
    r"[A-Z][A-Za-z'’-]{1,40}(?:\s+[A-Z][A-Za-z'’-]{1,40}){0,2}\b"
)
_PLACE_REFERENCE = re.compile(
    r"\b(?:at|near|inside|outside|located at|arrived at)\s+(?:the\s+)?"
    r"(?P<place>[A-Z][A-Za-z0-9'’.-]{1,40}(?:\s+(?:[A-Z][A-Za-z0-9'’.-]{1,40}|"
    r"Street|Road|Avenue|Boulevard|Drive|Lane|Park|Center|Hospital|School|Station)){0,3})"
)
ANSWER_STAGE_LABELS = {
    "queued": "Waiting in the answer queue",
    "retrieving": "Finding relevant support",
    "reranking": "Prioritizing support",
    "generating": "Drafting from the best support",
    "verifying": "Checking citations and text",
    "complete": "Answer ready",
    "failed": "Answer needs attention",
    "cancelling": "Cancelling this request",
    "cancelled": "Answer cancelled",
}
ANSWER_STAGE_MESSAGES = {
    "retrieving": "Searching this matter's searchable sources.",
    "reranking": "Comparing matching passages for relevance.",
    "generating": "Drafting only from the selected case support.",
    "repairing": "Rewriting the draft closer to the cited text for consistency checks.",
    "verifying": "Checking citation references and text consistency; meaning still needs human review.",
}

RESEARCH_COVERAGE_NOTICE = (
    "This investigation is limited to selected search passages; it did not check every source. "
    "Use Check every source for a document-by-document task."
)

def _source_coverage(
    readiness: MatterReadinessRecord,
    *, changed_since_retrieval: bool = False,
) -> dict[str, object]:
    """Describe query-time coverage without exposing source identity or content."""

    excluded = max(0, readiness.total_count - readiness.searchable_count)
    partial = excluded > 0
    notice = ""
    if partial:
        source_word = "source" if excluded == 1 else "sources"
        need_word = "needs" if excluded == 1 else "need"
        excluded_word = "is" if excluded == 1 else "are"
        notice = (
            f"Searchable text is available for {readiness.searchable_count:,} of "
            f"{readiness.total_count:,} sources. {excluded:,} {source_word} "
            f"{need_word} attention and {excluded_word} excluded."
        )
    if partial and (readiness.playback_only_count or readiness.recording_review_count):
        notice = (
            f"Searchable text is available for {readiness.searchable_count:,} of {readiness.total_count:,} sources. "
            f"{excluded:,} sources are not searchable and are excluded."
        )
    if partial and readiness.processing_count:
        processing = readiness.processing_count
        notice = (
            f"Searchable text is available for {readiness.searchable_count:,} of {readiness.total_count:,} sources. "
            f"{processing:,} source{' is' if processing == 1 else 's are'} still uploading or processing and excluded."
        )
        if readiness.attention_count:
            attention = readiness.attention_count
            notice += f" {attention:,} other source{' needs' if attention == 1 else 's need'} attention and cannot be searched."
    if partial and (readiness.playback_only_count or readiness.recording_review_count):
        notice += " Recordings without a transcript remain available for playback and review."
    if readiness.email_count:
        from .extended_extract import EMAIL_COVERAGE_NOTICE

        notice = " ".join(part for part in (notice, EMAIL_COVERAGE_NOTICE) if part)
    if readiness.pdf_count:
        from .pdf_coverage import PDF_COVERAGE_NOTICE

        notice = " ".join(part for part in (notice, PDF_COVERAGE_NOTICE) if part)
    if changed_since_retrieval:
        notice = " ".join(part for part in (notice,
            "Sources changed after the search started. This result may not include newly added or changed material. "
            "Run the question again when the sources you need are searchable.",
        ) if part)
    return {
        "mode": "partial" if partial or readiness.email_count or readiness.pdf_count or changed_since_retrieval else "complete",
        "searchable_count": readiness.searchable_count,
        "total_count": readiness.total_count,
        "excluded_count": excluded,
        "notice": notice,
    }

def _focused_answer_scope(
    question: str,
    readiness: MatterReadinessRecord,
    answer: VerifiedAnswer,
    evidence: Mapping[str, "WorkbenchCitation"],
) -> dict[str, object]:
    """Persist what focused RAG did without implying exhaustive review."""

    used = tuple(
        evidence[identifier]
        for identifier in answer.used_evidence_ids
        if identifier in evidence
    )
    cited_sources = len({citation.document_id for citation in used})
    candidate_sources = len(
        {citation.document_id for citation in evidence.values()}
    )
    broad_summary = classify_question(question).broad_summary
    collection_wide = bool(_COLLECTION_WIDE_QUESTION.search(question))
    if collection_wide:
        notice = (
            "This is a focused answer from the highest-ranked passages, not a "
            "document-by-document completeness review. Verify collection-wide "
            "lists, counts, and chronologies with source search and direct review."
        )
        mode = "focused"
    elif broad_summary:
        passage_word = "passage" if len(evidence) == 1 else "passages"
        source_word = "source" if candidate_sources == 1 else "sources"
        searchable_word = "source" if readiness.searchable_count == 1 else "sources"
        source_target = min(3, candidate_sources)
        if source_target >= 2 and cited_sources >= source_target:
            notice = (
                f"This broader orientation sampled {len(evidence):,} top {passage_word} "
                f"across {candidate_sources:,} {source_word} from "
                f"{readiness.searchable_count:,} searchable {searchable_word}. It is not "
                "an every-source review; preparation and retrieval can omit material."
            )
            mode = "broader_orientation"
        else:
            notice = (
                f"This focused result retained support from {cited_sources:,} cited "
                f"source{'s' if cited_sources != 1 else ''} after considering "
                f"{candidate_sources:,}. It is not a reliable matter orientation or an "
                "every-source review; refine the question or review additional sources."
            )
            mode = "focused_orientation"
    else:
        searchable_word = "source" if readiness.searchable_count == 1 else "sources"
        cited_word = "source" if cited_sources == 1 else "sources"
        notice = (
            f"At completion, {readiness.searchable_count:,} {searchable_word} had searchable text. "
            f"This focused answer compared the strongest matching passages and used "
            f"{cited_sources:,} cited {cited_word}; it did not review every source."
        )
        mode = "focused"
    scope = {
        "mode": mode,
        "collection_wide_request": collection_wide,
        "searchable_source_count": readiness.searchable_count,
        "candidate_passage_count": len(evidence),
        "candidate_source_count": candidate_sources,
        "cited_passage_count": len(used),
        "cited_source_count": cited_sources,
        "notice": notice,
    }
    if broad_summary:
        scope["broad_summary_request"] = True
    return scope


def _backup_status_projection() -> dict[str, object]:
    """Read one administrator-safe backup receipt, never repository content."""

    configured = env(
        "RECORDBENCH_BACKUP_STATUS_FILE",
        "/srv/recordbench/.local/state/recordbench-backup/status.json",
    ).strip()
    path = Path(configured)
    fallback = {
        "state": "not_initialized",
        "tone": "attention",
        "headline": "Backup setup required",
        "message": "No encrypted Exculpata disaster-recovery receipt is available.",
        "completed_at": "",
        "duration_seconds": 0,
        "snapshot_id": "",
        "snapshot_short": "",
        "retention": "14d",
        "stale": True,
    }
    if not configured or not path.is_absolute() or path == Path("/"):
        return fallback
    try:
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 65_536
        ):
            return fallback
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    state = payload.get("state")
    if state not in {"initialized", "succeeded", "deferred", "failed"}:
        return fallback
    completed_at = payload.get("completed_at")
    if not isinstance(completed_at, str) or len(completed_at) > 40:
        completed_at = ""
    stale = state != "succeeded"
    if state == "succeeded" and completed_at:
        try:
            completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            stale = (
                datetime.now(timezone.utc) - completed.astimezone(timezone.utc)
            ).total_seconds() > 36 * 3_600
        except ValueError:
            stale = True
    snapshot_id = payload.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not re.fullmatch(
        r"[0-9a-f]{0,64}", snapshot_id
    ):
        snapshot_id = ""
    retention = payload.get("retention")
    if not isinstance(retention, str) or not re.fullmatch(r"[0-9]{1,3}d", retention):
        retention = "14d"
    duration = payload.get("duration_seconds")
    if not isinstance(duration, int) or not 0 <= duration <= 7 * 86_400:
        duration = 0
    default_messages = {
        "initialized": "The encrypted repository is ready; its first snapshot has not completed.",
        "succeeded": "Encrypted NAS snapshot and repository verification completed.",
        "deferred": "The quiet-hour backup deferred because background work was active; it will retry.",
        "failed": "The most recent backup needs administrator attention.",
    }
    raw_message = payload.get("message")
    message = (
        " ".join(raw_message.split())[:320]
        if isinstance(raw_message, str) and raw_message.strip()
        else default_messages[state]
    )
    if state == "succeeded" and stale:
        message = (
            "The last successful backup is older than 36 hours. Review the "
            "backup timer and NAS mount."
        )
    return {
        "state": state,
        "tone": "ready" if state == "succeeded" and not stale else "attention",
        "headline": (
            "Protected and current"
            if state == "succeeded" and not stale
            else "Backup needs attention"
        ),
        "message": message,
        "completed_at": completed_at,
        "duration_seconds": duration,
        "snapshot_id": snapshot_id,
        "snapshot_short": snapshot_id[:12],
        "retention": retention,
        "stale": stale,
    }


class _FullTextSynthesisSourceScan:
    """Cooperative bounded reads, without materializing a source population."""

    def __init__(self):
        self.units = self.characters = self.serialized_characters = 0
        self.deadline = monotonic() + MAX_SYNTHESIS_SCAN_SECONDS

    def check(self):
        if (self.units > MAX_SYNTHESIS_SCAN_UNITS
                or self.characters > MAX_SYNTHESIS_SCAN_CHARS
                or self.serialized_characters > MAX_SYNTHESIS_SCAN_SERIALIZED_CHARS
                or monotonic() >= self.deadline):
            raise WorkspaceProblem(_SYNTHESIS_SCAN_LIMIT)

    def charge_read(self, characters):
        # The text reader charges <=65,536 Unicode characters per read, then
        # refuses over-budget input before retaining/decoding another record.
        # Serialized UTF-8 bytes are at most four times the character count.
        self.serialized_characters += characters
        self.check()

    def iter_units(self, document):
        from .unit_stream import UnitRecordLimit
        self.check()
        try:
            for unit in document.iter_parsed_units(budget_check=self.check,
                    read_check=self.charge_read, max_record_chars=MAX_SYNTHESIS_SCAN_RECORD_CHARS):
                self.units += 1
                self.characters += len(unit.text)
                # Refuse an oversized unit before making a UTF-8/hash copy.
                self.check()
                yield unit
            self.check()
        except WorkspaceProblem:
            raise
        except UnitRecordLimit as exc:
            raise WorkspaceProblem(_SYNTHESIS_SCAN_LIMIT) from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise WorkspaceProblem("A frozen synthesis source has no valid bounded text reader.") from exc


@dataclass(frozen=True)
class SourceRow:
    document_id: str
    name: str
    relative_path: str
    kind: str
    state: str
    tone: str
    count_label: str
    action_token: str
    retryable: bool
    removable: bool = True
    review_state: str = "unreviewed"
    collection_id: str = ""
    collection_name: str = "Unfiled"
    added_at: str = ""
    byte_size: int = 0
    origin: str = "upload"
    byte_match_count: int = 0


@dataclass(frozen=True)
class SourceLibraryPage:
    items: tuple[SourceRow, ...]
    page: int
    page_size: int
    total: int
    total_pages: int
    first_item: int
    last_item: int
    stats: Mapping[str, int]
    type_counts: Mapping[str, int]
    review_counts: Mapping[str, int]
    view: str
    query: str
    status: str
    kind: str
    review: str
    collection_id: str
    source_set_id: str
    sort: str
    folder: str = ""
    same_content: str = ""
    matching_only: bool = False
    comparison_available: bool = True


@dataclass(frozen=True)
class SourceReviewView:
    row: SourceRow
    document: PilotDocument
    unit: PilotUnit | None
    unit_index: int
    unit_count: int
    position_label: str
    previous_href: str
    next_href: str
    embedded_content_href: str


@dataclass(frozen=True)
class MediaReviewView:
    row: SourceRow
    document: PilotDocument
    job: MediaJobRecord | None
    transcript: MediaTranscriptRecord | None
    summary: MediaSummaryRecord | None
    segments: tuple[TranscriptSegmentRecord, ...]
    speakers: tuple[SpeakerMappingRecord, ...]
    clips: tuple[MediaClipRecord, ...]
    playback_href: str
    playback_media_type: str
    playback_is_compatible_copy: bool
    player_kind: str
    start_ms: int
    focus_segment_id: str


@dataclass(frozen=True)
class MediaSummaryFailureView:
    category: str
    explanation: str
    attempt_label: str
    retry_status: str
    recovery: str


def _media_summary_failure_view(
    summary: MediaSummaryRecord | None,
) -> MediaSummaryFailureView | None:
    if summary is None or summary.state != "failed":
        return None
    message = summary.message.casefold()
    if "answering" in message and ("unavailable" in message or "offline" in message):
        category = "Local answering unavailable"
        explanation = (
            "The optional overview could not use the local answer service. "
            "Playback and the full transcript are still ready."
        )
        recovery = "Check local answering status, then try the overview again."
    elif "cited transcript support" in message or "supported points" in message:
        category = "Supported overview not available"
        explanation = (
            "The overview did not retain enough transcript support to show as "
            "reliable orientation. Playback and the full transcript are still ready."
        )
        recovery = "Review or correct the transcript, then try the overview again."
    elif "changed" in message:
        category = "Transcript changed during overview"
        explanation = (
            "The optional overview stopped because its transcript basis changed. "
            "The current transcript and playback remain ready."
        )
        recovery = "Review the current transcript, then try the overview again."
    elif "no passages" in message:
        category = "No transcript passages available"
        explanation = (
            "There was no supported transcript text for the optional overview. "
            "Playback remains available."
        )
        recovery = "Review the transcript state before trying again."
    else:
        category = "Overview generation error"
        explanation = (
            "The optional overview stopped after a local processing error. "
            "Playback and the full transcript are still ready."
        )
        recovery = (
            "Try the overview again; if it repeats, ask an administrator to check "
            "local answering."
        )

    attempts = max(int(summary.attempts), 0)
    limit = MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS
    if attempts >= limit:
        attempt_label = "Automatic recovery limit reached"
        retry_status = (
            f"{attempts} total attempts recorded. Automatic recovery stops after "
            f"attempt {limit}; a deliberate manual retry remains available."
        )
    else:
        remaining = limit - attempts
        attempt_label = f"Attempt {attempts} of {limit}"
        retry_status = (
            f"{remaining} automatic recovery attempt"
            f"{'s' if remaining != 1 else ''} remain."
        )
    return MediaSummaryFailureView(
        category=category,
        explanation=explanation,
        attempt_label=attempt_label,
        retry_status=retry_status,
        recovery=recovery,
    )


@dataclass(frozen=True)
class WorkbenchCitation:
    source_name: str
    location: str
    excerpt: str
    href: str
    support_token: str
    matter_id: str
    document_id: str
    source_version_id: str
    excerpt_digest: str
    chunk_id: str
    unit_number: int
    line_start: int | None
    line_end: int | None
    evidence_kind: str

    def staff_payload(self) -> dict[str, str]:
        return {
            "source_name": self.source_name,
            "location": self.location,
            "href": self.href,
            "support_token": self.support_token,
            "evidence_kind": self.evidence_kind,
        }


@dataclass(frozen=True)
class SupportView:
    source_name: str
    kind: str
    location: str
    position_label: str
    lines: tuple[str, ...]
    previous_href: str
    next_href: str
    support_token: str
    evidence_kind: str
    source_review_href: str
    playback_href: str
    playback_media_type: str
    player_kind: str
    start_ms: int
    end_ms: int
    autoplay: bool


class MatterMaintenanceCoordinator:
    """Run bounded lifecycle maintenance without tying it to an HTTP request."""

    def __init__(
        self,
        run_once: Callable[[], Mapping[str, int]],
        *,
        interval_seconds: float = 900.0,
    ) -> None:
        self._run_once = run_once
        self._interval = max(float(interval_seconds), 30.0)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._last_run_at = ""
        self._last_result: dict[str, int] = {
            "uploads_cleaned": 0,
            "matters_purged": 0,
            "matters_deferred": 0,
        }
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._loop,
            name="recordbench-maintenance",
            daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = dict(self._run_once())
                with self._lock:
                    self._last_result = {
                        "uploads_cleaned": int(result.get("uploads_cleaned", 0)),
                        "matters_purged": int(result.get("matters_purged", 0)),
                        "matters_deferred": int(result.get("matters_deferred", 0)),
                    }
                    self._last_run_at = datetime.now(timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    )
                    self._last_error = ""
            except Exception:
                with self._lock:
                    self._last_run_at = datetime.now(timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    )
                    self._last_error = "Maintenance needs administrator attention."
            if self._stop.wait(self._interval):
                return

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": True,
                "last_run_at": self._last_run_at,
                "last_error": self._last_error,
                **self._last_result,
            }

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


LOGGER = logging.getLogger("recordbench.workbench")
MAX_AUTOMATIC_DISCOVERY_UNITS = 20000
# Bounds one matter's steps per run; each step seals or processes durable work.
MAX_AUTOMATIC_DISCOVERY_STEPS = 400


@dataclass(frozen=True)
class AutomaticDiscoveryPass:
    units: int
    progress: int
    failed_matters: tuple[str, ...] = ()


class AutomaticDiscoveryCoordinator:
    """Find suggested people, things and dates after sources finish processing.

    Woken when a source becomes ready, and sweeps every active matter on start
    and at a slow interval so restarts and missed wake-ups converge. Work is
    bounded per pass; failures stay in the ledger and never reach a page.
    """

    def __init__(
        self,
        run_once: Callable[..., AutomaticDiscoveryPass],
        *,
        sweep_seconds: float = 900.0,
    ) -> None:
        self._run_once = run_once
        self._sweep = max(float(sweep_seconds), 30.0)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._sweep_due = True
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._loop, name="recordbench-discovery", daemon=True
        )
        self._thread.start()

    def wake(self, matter_id: str) -> None:
        with self._lock:
            self._pending.add(matter_id)
        self._wake.set()

    def _loop(self) -> None:
        last_sweep: float | None = None  # The first pass always sweeps.
        while not self._stop.is_set():
            self._wake.clear()
            with self._lock:
                matter_ids = tuple(sorted(self._pending))
                self._pending.clear()
            sweep = last_sweep is None or monotonic() - last_sweep >= self._sweep
            try:
                if sweep:
                    last_sweep = monotonic()
                    result = self._run_once(None, should_stop=self._stop.is_set)
                else:
                    result = (self._run_once(matter_ids, should_stop=self._stop.is_set)
                              if matter_ids else AutomaticDiscoveryPass(0, 0))
                handled = result.progress
                with self._lock:
                    self._last_error = (
                        "Automatic discovery needs administrator attention." if result.failed_matters else "")
                    if handled:
                        # More work may remain; continue without waiting.
                        if sweep:
                            last_sweep = None
                        self._pending.update(matter_ids)
                        self._wake.set()
            except Exception:
                with self._lock:
                    self._last_error = "Automatic discovery needs administrator attention."
            if self._stop.is_set():
                return
            self._wake.wait(self._sweep)

    def status(self) -> dict[str, object]:
        with self._lock:
            return {"enabled": True, "last_error": self._last_error}

    def close(self) -> None:
        # Wait for the worker to finish: a step checks the stop flag before every
        # source and unit, and callers close the source stores right after this.
        self._stop.set()
        self._wake.set()
        self._thread.join()


class CaseIntelligenceWorkbench:
    """Matter-isolated Milestone A runtime behind an explicit actor boundary."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        generator: GeneratorClient | None = None,
        postgres_connection=None,
        embedding=None,
        reranker=None,
        learned_retrieval: bool | None = None,
        source_registry: SourceLocationRegistry | None = None,
        background_ingestion: bool = False,
        ingestion_workers: int = 2,
        answer_workers: int = 2,
        media_processor: MediaProcessor | None = None,
        media_poll_seconds: float = 2.0,
        managed_storage_root: Path | None = None,
        storage_policy: StoragePolicy | None = None,
        malware_scanner: MalwareScanner | None = None,
        malware_scan_mode: str | None = None,
        exact_search_backend: ExactSearchBackend | None = None,
        principal_enabled: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).absolute()
        self.exact_search_backend = exact_search_backend or ReferenceExactSearchBackend()
        self._prepare_runtime()
        self.storage_policy = storage_policy or StoragePolicy.from_environment()
        configured_storage = managed_storage_root
        if configured_storage is None:
            configured_value = os.getenv(
                "CASE_INTELLIGENCE_MANAGED_STORAGE_ROOT", ""
            ).strip()
            configured_storage = Path(configured_value) if configured_value else None
        if configured_storage is None:
            self.storage = ManagedMatterStorage.legacy_runtime(
                self.runtime_dir, policy=self.storage_policy
            )
        else:
            legacy_matters = self.runtime_dir / "matters"
            if legacy_matters.exists() and any(legacy_matters.iterdir()):
                raise RuntimeError(
                    "legacy matter bytes require an explicit migration before managed storage activation"
                )
            self.storage = ManagedMatterStorage(
                Path(configured_storage).absolute(),
                policy=self.storage_policy,
                require_marker=True,
            )
        self.workspace = WorkspaceStore(self.runtime_dir / "workbench.sqlite", principal_enabled=principal_enabled)
        from .entity_unit_reader import EntityUnitReader
        self.entity_unit_reader = EntityUnitReader()
        self.workspace.recover_interrupted_matter_purges()
        self.workspace.recover_running_analysis_runs()
        self._stores: dict[str, PilotStore] = {}
        self._store_lock = threading.RLock()
        self._matter_source_locks: dict[str, threading.RLock] = {}
        self._storage_reservation_lock = threading.RLock()
        self._playback_reservations: dict[tuple[str, str], int] = {}
        self._clip_export_lock = threading.RLock()
        self._clip_exports: dict[str, set[Path]] = {}
        self._response_lease_lock = threading.RLock()
        self._response_leases: dict[str, set[str]] = {}
        self._reconcile_clip_exports()
        self.malware_scanner = malware_scanner or scanner_from_environment()
        self.malware_scan_mode = (
            malware_scan_mode
            if malware_scan_mode is not None
            else os.getenv("CASE_INTELLIGENCE_MALWARE_SCAN_MODE", "extended")
        ).strip().casefold()
        if self.malware_scan_mode not in {"disabled", "extended", "all"}:
            raise RuntimeError("CASE_INTELLIGENCE_MALWARE_SCAN_MODE is invalid")
        self._postgres_lock = threading.RLock()
        self.source_registry = source_registry or source_registry_from_environment()
        self.background_ingestion = bool(background_ingestion)
        self.ingestion: IngestionCoordinator | None = None
        self.media: MediaCoordinator | None = None
        self.playback: BrowserPlaybackCoordinator | None = None
        self.answers: AnswerCoordinator | None = None
        self.research: ResearchCoordinator | None = None
        self.full_review: ReviewCoordinator | None = None
        self.maintenance: MatterMaintenanceCoordinator | None = None
        self.automatic_discovery: AutomaticDiscoveryCoordinator | None = None
        self._automatic_discovery_reported: set[str] = set()
        try:
            generation_concurrency = int(
                os.getenv("CASE_INTELLIGENCE_GENERATION_CONCURRENCY", "2")
            )
        except ValueError:
            generation_concurrency = 2
        self.generator = GroundedGenerationService(
            generator or generator_from_environment(),
            maximum_active=generation_concurrency,
        )
        configured_postgres_dsn = self._postgres_dsn_from_environment()
        self._postgres_projection_configured = bool(
            postgres_connection is not None or configured_postgres_dsn
        )
        self.postgres_connection = postgres_connection
        self._owns_postgres = False
        if self.postgres_connection is None:
            self.postgres_connection = self._connect_postgres(configured_postgres_dsn)
            self._owns_postgres = self.postgres_connection is not None
        self.embedding = embedding
        self.reranker = reranker
        worker_ready = False
        if self.embedding is None or self.reranker is None:
            worker_url = os.getenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "").strip()
            worker_ready = self._retrieval_worker_ready(worker_url) if worker_url else False
            if worker_ready:
                self.embedding = RemoteEmbeddingAdapter(worker_url)
                self.reranker = RemoteRerankerAdapter(worker_url)
        if self.embedding is None:
            self.embedding = DeterministicEmbeddingAdapter()
        if self.reranker is None:
            self.reranker = DeterministicReranker()
        if learned_retrieval is None:
            learned_retrieval = worker_ready
        self.learned_retrieval = bool(
            learned_retrieval and self.postgres_connection is not None
        )
        self.postgres_ready = False
        if self.postgres_connection is not None:
            try:
                with self.postgres_connection.cursor() as cursor:
                    apply_postgres_migrations(cursor)
                self.postgres_connection.commit()
                self.postgres_ready = True
            except Exception:
                self.postgres_connection.rollback()
                self.learned_retrieval = False
        self._rebuild_all_indexes()
        if self.background_ingestion:
            self.ingestion = IngestionCoordinator(
                self.workspace,
                self.source_registry,
                self.storage.ingestion_staging,
                resolve_matter=self._matter_by_id,
                resolve_store=self.source_store,
                index_document=self._index_document,
                workers=ingestion_workers,
            )
        configured_processor = media_processor or TranscriptionV2Client.from_environment()
        self.media = MediaCoordinator(
            self.workspace,
            configured_processor,
            resolve_matter=self._matter_by_id,
            resolve_store=self.source_store,
            project_transcript=self._project_media_transcript,
            summarize_transcript=self._summarize_media_transcript,
            poll_seconds=media_poll_seconds,
        )
        self.playback = BrowserPlaybackCoordinator(
            resolve_matter=self._matter_by_id,
            resolve_store=self.source_store,
            reserve_capacity=self._reserve_browser_playback,
            release_capacity=self._release_browser_playback,
            admit_matter=lambda matter: (
                self.workspace.matter_lifecycle(matter.matter_id).state == "active"
            ),
        )
        self.answers = AnswerCoordinator(
            self.workspace,
            process=self._process_answer_job,
            finish=self._finish_answer_job,
            workers=answer_workers,
        )
        try:
            research_workers = int(
                os.getenv("CASE_INTELLIGENCE_RESEARCH_WORKERS", "1")
            )
        except ValueError:
            research_workers = 1
        try:
            review_workers = int(
                os.getenv("CASE_INTELLIGENCE_FULL_REVIEW_WORKERS", "1")
            )
        except ValueError:
            review_workers = 1
        try:
            review_source_concurrency = int(
                os.getenv("CASE_INTELLIGENCE_FULL_REVIEW_SOURCE_CONCURRENCY", "2")
            )
        except ValueError:
            review_source_concurrency = 2
        self.research = ResearchCoordinator(
            self.workspace,
            process=self._process_research_job,
            finish=self._finish_research_job,
            workers=research_workers,
        )
        self.full_review = ReviewCoordinator(
            self.workspace,
            process=self._process_review_decision,
            record=self._record_review_decision,
            finish=self._finish_review_run,
            workers=review_workers,
            source_concurrency=review_source_concurrency,
        )
        self.report_compilation = ReportCompilationFlow(self,
            full_text_support_resolver=self._resolve_full_text_report_support)
        if os.getenv("CASE_INTELLIGENCE_MAINTENANCE_ENABLED", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            try:
                maintenance_interval = float(
                    os.getenv("CASE_INTELLIGENCE_MAINTENANCE_INTERVAL_SECONDS", "900")
                )
            except ValueError:
                maintenance_interval = 900.0
            self.maintenance = MatterMaintenanceCoordinator(
                self.run_maintenance_once,
                interval_seconds=maintenance_interval,
            )
        if os.getenv("CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY", "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            self.automatic_discovery = AutomaticDiscoveryCoordinator(
                self.automatic_discovery_pass
            )

    def _prepare_runtime(self) -> None:
        if self.runtime_dir.exists() and (
            self.runtime_dir.is_symlink() or not self.runtime_dir.is_dir()
        ):
            raise RuntimeError("workbench runtime directory is unsafe")
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _postgres_dsn_from_environment() -> str:
        inline = os.getenv("CASE_INTELLIGENCE_POSTGRES_DSN", "").strip()
        configured_file = os.getenv(
            "CASE_INTELLIGENCE_POSTGRES_DSN_FILE", ""
        ).strip()
        if inline and configured_file:
            raise RuntimeError(
                "Configure only one of CASE_INTELLIGENCE_POSTGRES_DSN or "
                "CASE_INTELLIGENCE_POSTGRES_DSN_FILE"
            )
        if not configured_file:
            return inline
        path = Path(configured_file)
        try:
            metadata = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or metadata.st_size > 4_096
            ):
                raise RuntimeError("PostgreSQL connection file is unsafe")
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError("PostgreSQL connection file is unavailable") from exc
        if not value or any(character in value for character in "\r\n\x00"):
            raise RuntimeError("PostgreSQL connection file is invalid")
        return value

    @staticmethod
    def _connect_postgres(dsn: str):
        if not dsn:
            return None
        try:
            import psycopg

            return psycopg.connect(dsn, connect_timeout=2, autocommit=True)
        except Exception:
            return None

    @staticmethod
    def _retrieval_worker_ready(url: str) -> bool:
        try:
            endpoint = validate_service_endpoint(
                url,
                environment_name="CASE_INTELLIGENCE_RETRIEVAL_ALLOWED_HOSTS",
                label="retrieval worker endpoint",
                schemes=frozenset({"http"}),
                origin_only=True,
            )
        except ValueError:
            return False
        try:
            with urllib.request.urlopen(endpoint + "/health", timeout=2) as response:
                payload = json.load(response)
            return bool(
                isinstance(payload, dict)
                and payload.get("status") == "ok"
                and payload.get("embedding_loaded") is True
                and payload.get("reranker_loaded") is True
            )
        except Exception:
            return False

    def close(self) -> None:
        if self.automatic_discovery is not None:
            self.automatic_discovery.close()
        if self.maintenance is not None:
            self.maintenance.close()
        if getattr(self, "report_compilation", None) is not None:
            self.report_compilation.close()
        if self.full_review is not None:
            self.full_review.close()
        if self.research is not None:
            self.research.close()
        if self.answers is not None:
            self.answers.close()
        if self.playback is not None:
            self.playback.close()
        if self.media is not None:
            self.media.close()
        if self.ingestion is not None:
            self.ingestion.close()
        with self._store_lock:
            stores = tuple(self._stores.values())
            self._stores.clear()
        for store in stores:
            store.close()
        self.workspace.close()
        if self.postgres_connection is not None:
            self.postgres_connection.close()

    def matters(
        self, actor_id: str, *, administrator: bool = False
    ) -> tuple[MatterRecord, ...]:
        if administrator:
            self.workspace.get_principal(actor_id)
            return self.workspace.all_matters()
        return self.workspace.list_matters(actor_id)

    def matter(
        self, slug: str, actor_id: str, *, administrator: bool = False
    ) -> MatterRecord:
        if administrator:
            self.workspace.get_principal(actor_id)
            return self.workspace.get_active_matter(slug)
        return self.workspace.get_matter(slug, actor_id)

    def _matter_by_id(self, matter_id: str) -> MatterRecord:
        return self.workspace.get_matter_by_id(matter_id)

    def create_matter(
        self,
        name: str,
        descriptor: str,
        actor_id: str,
        *,
        retention_days: int | None = None,
    ) -> MatterRecord:
        matter = self.workspace.create_matter(
            name,
            descriptor,
            actor_id,
            retention_days=retention_days,
        )
        self.source_store(matter)
        return matter

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    def retention_projection(self, matter: MatterRecord) -> dict[str, object]:
        retention = self.workspace.matter_retention(matter.matter_id)
        now = self.workspace.current_time()
        office_zone = ZoneInfo("America/New_York")
        local_today = now.astimezone(office_zone).date()
        minimum_date = (local_today + timedelta(days=1)).isoformat()
        maximum_date = (local_today + timedelta(days=365)).isoformat()
        if retention is None:
            return {
                "record": None,
                "status": "unscheduled",
                "tone": "attention",
                "label": "Set a review end date",
                "short_label": "Set date",
                "detail": (
                    "This existing matter has no automatic deletion schedule. "
                    "Set one before expanding the pilot."
                ),
                "show_warning": True,
                "expires_on": "",
                "purge_on": "",
                "days_remaining": None,
                "minimum_date": minimum_date,
                "maximum_date": maximum_date,
            }
        expires = self._parse_utc(retention.expires_at)
        purge_after = self._parse_utc(retention.purge_after)
        seconds_to_expiry = (expires - now).total_seconds()
        seconds_to_purge = (purge_after - now).total_seconds()
        expiry_date = expires.astimezone(office_zone).date().isoformat()
        purge_date = purge_after.astimezone(office_zone).date().isoformat()
        if seconds_to_purge <= 0:
            status = "due"
            tone = "danger"
            label = "Automatic deletion is due"
            short_label = "Due"
            detail = "Finish active work or extend the review end date now."
            days_remaining: int | None = 0
        elif seconds_to_expiry <= 0:
            days_to_purge = max(math.ceil(seconds_to_purge / 86_400), 1)
            status = "grace"
            tone = "danger"
            label = f"Review period ended · deletion in {days_to_purge} day{'s' if days_to_purge != 1 else ''}"
            short_label = f"{days_to_purge}d grace"
            detail = "Export the work product or extend this temporary matter before deletion."
            days_remaining = 0
        else:
            days_remaining = max(math.ceil(seconds_to_expiry / 86_400), 1)
            status = "warning" if days_remaining <= 14 else "scheduled"
            tone = "attention" if status == "warning" else "quiet"
            label = f"Scheduled to close in {days_remaining} day{'s' if days_remaining != 1 else ''}"
            short_label = f"{days_remaining}d"
            detail = f"Automatic deletion begins after the seven-day grace period on {purge_date}."
        return {
            "record": retention,
            "status": status,
            "tone": tone,
            "label": label,
            "short_label": short_label,
            "detail": detail,
            "show_warning": status in {"warning", "grace", "due"},
            "expires_on": expiry_date,
            "purge_on": purge_date,
            "days_remaining": days_remaining,
            "minimum_date": minimum_date,
            "maximum_date": maximum_date,
        }

    def retention_for_date(
        self,
        matter: MatterRecord,
        actor_id: str,
        expires_on: str,
        *,
        administrator_override: bool = False,
    ) -> MatterRetentionRecord:
        try:
            selected_date = date.fromisoformat((expires_on or "").strip())
        except ValueError as exc:
            raise WorkspaceProblem("Choose a valid review end date.") from exc
        office_zone = ZoneInfo("America/New_York")
        selected = datetime.combine(selected_date, time(23, 59, 59), office_zone)
        return self.workspace.set_matter_retention(
            matter.matter_id,
            actor_id,
            selected,
            administrator_override=administrator_override,
        )

    def source_store(self, matter: MatterRecord) -> PilotStore:
        with self._store_lock:
            existing = self._stores.get(matter.matter_id)
            if existing is not None:
                return existing
            if not re.fullmatch(r"ci-matter-[0-9a-f]{32}", matter.matter_id):
                raise RuntimeError("stored matter identity is invalid")
            root = self.storage.matters / matter.matter_id / "sources"
            store = PilotStore(
                root,
                document_file_limit=self.storage_policy.document_file_bytes,
                media_file_limit=self.storage_policy.media_file_bytes,
                upload_session_limit=self.storage_policy.upload_session_bytes,
                on_change=lambda change: self._apply_source_store_change(matter, change),
                mutation_lock=self._matter_source_locks.setdefault(
                    matter.matter_id, threading.RLock()
                ),
                malware_scanner=self.malware_scanner,
                malware_scan_mode=self.malware_scan_mode,
            )
            self._stores[matter.matter_id] = store
            if store.documents:
                self.workspace.reconcile_source_organizations(
                    matter.matter_id,
                    tuple(
                        (
                            document.document_id,
                            document.origin,
                            document.relative_path or document.display_name,
                        )
                        for document in store.documents.values()
                    ),
                )
            self._sync_source_catalog(matter, tuple(store.documents.values()))
            return store

    def matter_storage_projection(self, matter: MatterRecord) -> dict[str, object]:
        used = self.storage.matter_payload_usage_bytes(matter.matter_id)
        reserved = self.workspace.pending_upload_bytes(
            matter.matter_id
        ) + self._playback_reserved_bytes(matter.matter_id)
        projected = used + reserved
        quota = self.storage_policy.matter_quota_bytes
        remaining = max(quota - projected, 0)
        return {
            "ready": projected <= quota,
            "used_bytes": used,
            "reserved_bytes": reserved,
            "projected_bytes": projected,
            "quota_bytes": quota,
            "remaining_bytes": remaining,
            "used_label": format_bytes(used),
            "reserved_label": format_bytes(reserved),
            "quota_label": format_bytes(quota),
            "remaining_label": format_bytes(remaining),
            "percent": min(round((projected / quota) * 100, 1), 100.0),
        }

    def upload_preflight_capacity_projection(
        self,
        matter: MatterRecord,
        actor_id: str,
        *,
        checkpoint_session_id: str = "",
        checkpoint_collection_id: str = "",
    ) -> dict[str, int]:
        """Return a matter-scoped capacity snapshot without reserving bytes."""

        with self._storage_reservation_lock:
            used = self.storage.matter_payload_usage_bytes(matter.matter_id)
            total_reserved = self.workspace.pending_upload_bytes(
                matter.matter_id
            ) + self._playback_reserved_bytes(matter.matter_id)
            checkpoint_validated = False
            checkpoint_remaining = 0
            if checkpoint_session_id and checkpoint_collection_id:
                try:
                    session, items = self.workspace.upload_session(
                        matter.matter_id, actor_id, checkpoint_session_id
                    )
                except KeyError:
                    session = None
                    items = ()
                if (
                    session is not None
                    and session.state in {"open", "complete", "partial"}
                    and session.collection_id == checkpoint_collection_id
                ):
                    checkpoint_validated = True
                    if session.state == "open":
                        checkpoint_remaining = sum(
                            max(item.expected_size - item.received_size, 0)
                            for item in items
                            if item.state in {"pending", "uploading", "uploaded"}
                        )
            checkpoint_remaining = min(checkpoint_remaining, total_reserved)
            other_reserved = total_reserved - checkpoint_remaining
            quota = self.storage_policy.matter_quota_bytes
            fresh_available = max(quota - used - total_reserved, 0)
            available = max(quota - used - other_reserved, 0)
        return {
            "version": 2,
            "quota_bytes": quota,
            "used_bytes": used,
            "total_reserved_bytes": total_reserved,
            "fresh_available_bytes": fresh_available,
            "checkpoint_validated": checkpoint_validated,
            "checkpoint_remaining_bytes": checkpoint_remaining,
            "other_reserved_bytes": other_reserved,
            "available_bytes": available,
        }

    def storage_capacity_projection(self, *, include_managed_usage: bool) -> dict[str, object]:
        try:
            capacity = self.storage.capacity(
                include_managed_usage=include_managed_usage
            )
        except (OSError, RuntimeError):
            return {
                "ready": False,
                "free_bytes": 0,
                "reserve_bytes": self.storage_policy.reserve_bytes,
                "available_bytes": 0,
                "managed_bytes": -1,
                "optimized_finalize": False,
                "free_label": "Unavailable",
                "reserve_label": format_bytes(self.storage_policy.reserve_bytes),
                "available_label": "Unavailable",
                "managed_label": "Unavailable",
            }
        return {
            "ready": capacity.ready,
            "free_bytes": capacity.free_bytes,
            "reserve_bytes": capacity.reserve_bytes,
            "available_bytes": capacity.available_bytes,
            "managed_bytes": capacity.managed_bytes,
            "optimized_finalize": capacity.optimized_finalize,
            "free_label": format_bytes(capacity.free_bytes),
            "reserve_label": format_bytes(capacity.reserve_bytes),
            "available_label": format_bytes(capacity.available_bytes),
            "managed_label": (
                format_bytes(capacity.managed_bytes)
                if capacity.managed_bytes >= 0
                else "Not calculated"
            ),
        }

    def create_upload_session(
        self,
        matter: MatterRecord,
        actor_id: str,
        collection_name: str,
        files: Sequence[Mapping[str, object]],
        *,
        collection_id: str = "",
        intake_receipt_id: str = "",
        intake_ordinals: Sequence[int] = (),
    ):
        requested = sum(int(item.get("expected_size") or 0) for item in files)
        with self._storage_reservation_lock:
            if intake_receipt_id:
                with self.workspace._lock:
                    existing = IntakeReceipts(self.workspace).validate_upload_locked(
                        matter.matter_id, actor_id, intake_receipt_id, intake_ordinals, files
                    )
                    if existing:
                        return self.workspace.upload_session(matter.matter_id, actor_id, existing)
            used = self.storage.matter_payload_usage_bytes(matter.matter_id)
            matter_reserved = self.workspace.pending_upload_bytes(
                matter.matter_id
            ) + self._playback_reserved_bytes(matter.matter_id)
            if used + matter_reserved + requested > self.storage_policy.matter_quota_bytes:
                remaining = max(
                    self.storage_policy.matter_quota_bytes - used - matter_reserved,
                    0,
                )
                raise UploadProblem(
                    "This matter has "
                    f"{format_bytes(remaining)} of upload capacity remaining. "
                    "Remove unneeded sources or split the review into another matter.",
                    413,
                )
            global_reserved = (
                self.workspace.pending_upload_bytes()
                + self._playback_reserved_bytes()
            )
            try:
                self.storage.ensure_capacity(global_reserved + requested)
            except RuntimeError as exc:
                raise UploadProblem(
                    "Managed matter storage does not have enough protected free space for "
                    "this upload collection. Ask an administrator to review capacity.",
                    507,
                ) from exc
            return self.workspace.create_upload_session(
                matter.matter_id,
                actor_id,
                collection_name,
                files,
                collection_id=collection_id,
                intake_receipt_id=intake_receipt_id,
                intake_ordinals=intake_ordinals,
            )

    def ensure_upload_write_capacity(self) -> None:
        with self._storage_reservation_lock:
            try:
                self.storage.ensure_capacity(
                    self.workspace.pending_upload_bytes()
                    + self._playback_reserved_bytes()
                )
            except RuntimeError as exc:
                raise UploadProblem(
                    "The upload paused because managed storage reached its protected reserve. "
                    "Its saved progress can be resumed after an administrator restores capacity.",
                    507,
                ) from exc

    def ensure_direct_upload_capacity(self, matter: MatterRecord, requested: int) -> None:
        """Protect the bounded non-JavaScript compatibility upload route."""

        with self._storage_reservation_lock:
            used = self.storage.matter_payload_usage_bytes(matter.matter_id)
            matter_reserved = self.workspace.pending_upload_bytes(
                matter.matter_id
            ) + self._playback_reserved_bytes(matter.matter_id)
            if used + matter_reserved + requested > self.storage_policy.matter_quota_bytes:
                raise UploadProblem("This matter does not have enough upload capacity remaining.", 413)
            try:
                self.storage.ensure_capacity(
                    self.workspace.pending_upload_bytes()
                    + self._playback_reserved_bytes()
                    + requested
                )
            except RuntimeError as exc:
                raise UploadProblem(
                    "Managed matter storage does not have enough protected free space.", 507
                ) from exc

    def _reserve_browser_playback(
        self, matter_id: str, document_id: str, requested: int
    ) -> None:
        """Reserve bounded temporary space before a video rendition is written."""

        amount = max(int(requested), 1)
        key = (matter_id, document_id)
        with self._storage_reservation_lock:
            if key in self._playback_reservations:
                return
            matter_reserved = sum(
                value
                for (reserved_matter, _), value in self._playback_reservations.items()
                if reserved_matter == matter_id
            )
            used = self.storage.matter_payload_usage_bytes(matter_id)
            pending_uploads = self.workspace.pending_upload_bytes(matter_id)
            if (
                used + pending_uploads + matter_reserved + amount
                > self.storage_policy.matter_quota_bytes
            ):
                raise UploadProblem(
                    "This matter does not have enough temporary capacity for a browser-compatible video copy.",
                    507,
                )
            all_reserved = sum(self._playback_reservations.values())
            try:
                self.storage.ensure_capacity(
                    self.workspace.pending_upload_bytes() + all_reserved + amount
                )
            except RuntimeError as exc:
                raise UploadProblem(
                    "Managed matter storage does not have enough protected capacity for a browser-compatible video copy.",
                    507,
                ) from exc
            self._playback_reservations[key] = amount

    def _playback_reserved_bytes(self, matter_id: str | None = None) -> int:
        with self._storage_reservation_lock:
            return sum(
                value
                for (reserved_matter, _), value in self._playback_reservations.items()
                if matter_id is None or reserved_matter == matter_id
            )

    def _release_browser_playback(self, matter_id: str, document_id: str) -> None:
        with self._storage_reservation_lock:
            self._playback_reservations.pop((matter_id, document_id), None)

    @staticmethod
    def _validate_clip_export_matter_id(matter_id: str) -> str:
        if not re.fullmatch(r"ci-matter-[0-9a-f]{32}", matter_id or ""):
            raise RuntimeError("clip export matter identity is invalid")
        return matter_id

    def _reconcile_clip_exports(self) -> None:
        """Remove abandoned matter-owned clip files before serving requests."""

        legacy_root = self.runtime_dir / "clip-exports"
        if legacy_root.exists() or legacy_root.is_symlink():
            securely_delete_owned_tree(
                legacy_root, allowed_parent=self.runtime_dir
            )
        with os.scandir(self.storage.matters) as entries:
            matter_roots = tuple(
                Path(entry.path)
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
                and re.fullmatch(r"ci-matter-[0-9a-f]{32}", entry.name)
            )
        for matter_root in matter_roots:
            clip_root = matter_root / "clip-exports"
            if clip_root.exists() or clip_root.is_symlink():
                securely_delete_owned_tree(clip_root, allowed_parent=matter_root)

    def begin_media_clip_export(self, matter: MatterRecord) -> Path:
        """Admit one matter-owned clip render before a purge can be claimed."""

        matter_id = self._validate_clip_export_matter_id(matter.matter_id)
        lock = self._matter_source_locks.setdefault(matter_id, threading.RLock())
        with lock:
            if self.workspace.matter_lifecycle(matter_id).state != "active":
                raise WorkspaceProblem(
                    "This matter is closing and cannot start another clip download."
                )
            matter_root = self.storage.matters / matter_id
            if matter_root.is_symlink() or not matter_root.is_dir():
                raise WorkspaceProblem("Clip export storage is unavailable.")
            clip_root = matter_root / "clip-exports"
            clip_root.mkdir(mode=0o700, exist_ok=True)
            if clip_root.is_symlink() or not clip_root.is_dir():
                raise WorkspaceProblem("Clip export storage is unavailable.")
            directory = Path(tempfile.mkdtemp(prefix="clip-", dir=clip_root))
            with self._clip_export_lock:
                self._clip_exports.setdefault(matter_id, set()).add(directory)
            return directory

    def finish_media_clip_export(self, matter_id: str, directory: Path) -> bool:
        """Remove one rendered response tree and release its purge admission."""

        matter_id = self._validate_clip_export_matter_id(matter_id)
        matter_root = self.storage.matters / matter_id
        clip_root = matter_root / "clip-exports"
        complete = True
        with self._clip_export_lock:
            try:
                securely_delete_owned_tree(Path(directory), allowed_parent=clip_root)
                try:
                    clip_root.rmdir()
                    PilotStore._fsync_directory(matter_root)
                except FileNotFoundError:
                    pass
                except OSError:
                    # Another admitted export still owns a sibling directory.
                    pass
            except Exception:
                complete = False
            finally:
                active = self._clip_exports.get(matter_id)
                if active is not None:
                    active.discard(Path(directory))
                    if not active:
                        self._clip_exports.pop(matter_id, None)
        return complete

    def _active_clip_export_count(self, matter_id: str) -> int:
        with self._clip_export_lock:
            return len(self._clip_exports.get(matter_id, ()))

    def begin_matter_response(
        self,
        matter: MatterRecord,
        *,
        allowed_states: frozenset[str] = frozenset({"active"}),
    ) -> str:
        """Lease a matter-bearing response before deletion can be claimed."""

        matter_id = self._validate_clip_export_matter_id(matter.matter_id)
        if not allowed_states or not allowed_states.issubset({"active", "purge_failed"}):
            raise ValueError("matter response states are invalid")
        lock = self._matter_source_locks.setdefault(matter_id, threading.RLock())
        with lock:
            if self.workspace.matter_lifecycle(matter_id).state not in allowed_states:
                raise WorkspaceProblem(
                    "This matter is closing and cannot start another download."
                )
            lease_id = f"response-{uuid.uuid4().hex}"
            with self._response_lease_lock:
                self._response_leases.setdefault(matter_id, set()).add(lease_id)
            return lease_id

    def finish_matter_response(self, matter_id: str, lease_id: str) -> None:
        """Release a response lease after its body has finished sending."""

        matter_id = self._validate_clip_export_matter_id(matter_id)
        if not re.fullmatch(r"response-[0-9a-f]{32}", lease_id or ""):
            raise RuntimeError("matter response lease identity is invalid")
        with self._response_lease_lock:
            active = self._response_leases.get(matter_id)
            if active is None or lease_id not in active:
                return
            active.remove(lease_id)
            if not active:
                self._response_leases.pop(matter_id, None)

    def _active_matter_response_count(self, matter_id: str) -> int:
        with self._response_lease_lock:
            return len(self._response_leases.get(matter_id, ()))

    def _cleanup_matter_clip_exports(self, matter_id: str) -> None:
        matter_id = self._validate_clip_export_matter_id(matter_id)
        if self._active_clip_export_count(matter_id):
            raise RuntimeError("a clip export is still active")
        matter_root = self.storage.matters / matter_id
        clip_root = matter_root / "clip-exports"
        if clip_root.exists() or clip_root.is_symlink():
            securely_delete_owned_tree(clip_root, allowed_parent=matter_root)

    def matter_for_closure(
        self,
        slug: str,
        actor_id: str,
        *,
        administrator_override: bool = False,
    ) -> tuple[MatterRecord, MatterLifecycleRecord]:
        return self.workspace.matter_for_closure(
            slug,
            actor_id,
            administrator_override=administrator_override,
        )

    def matter_active_work_counts(self, matter_id: str) -> dict[str, int]:
        """Combine durable queues with process-local playback preparation."""

        active = self.workspace.active_matter_work_counts(matter_id)
        playback_active = bool(
            (self.playback is not None and self.playback.has_matter_work(matter_id))
            or self._playback_reserved_bytes(matter_id)
        )
        return {
            **active,
            "playback": int(playback_active),
            "exports": (
                self._active_clip_export_count(matter_id)
                + self._active_matter_response_count(matter_id)
            ),
        }

    def _matter_source_count_for_purge(
        self, matter: MatterRecord, lifecycle: MatterLifecycleRecord
    ) -> int:
        if lifecycle.state != "active":
            return lifecycle.source_count
        source_root = self.storage.matters / matter.matter_id / "sources"
        if source_root.exists():
            return self.source_store(matter).document_count()
        return lifecycle.source_count

    def begin_matter_purge(
        self,
        slug: str,
        actor_id: str,
        confirmed_name: str,
        *,
        administrator_override: bool = False,
    ) -> tuple[MatterRecord, MatterLifecycleRecord]:
        matter, lifecycle = self.matter_for_closure(
            slug,
            actor_id,
            administrator_override=administrator_override,
        )
        lock = self._matter_source_locks.setdefault(
            matter.matter_id, threading.RLock()
        )
        with lock:
            if any(self.matter_active_work_counts(matter.matter_id).values()):
                raise WorkspaceProblem(
                    "Wait for current uploads, source and media processing, transcript overviews, analysis, browser playback preparation, work-product downloads, answers, investigations, and every-source checks to finish before deleting this matter."
                )
            return self.workspace.begin_matter_purge(
                slug,
                actor_id,
                confirmed_name,
                source_count=self._matter_source_count_for_purge(matter, lifecycle),
                administrator_override=administrator_override,
            )

    def execute_matter_purge(
        self, matter: MatterRecord, lifecycle: MatterLifecycleRecord
    ) -> MatterLifecycleRecord:
        purge_id = lifecycle.purge_id or ""
        if not _MATTER_PURGE_ID.fullmatch(purge_id):
            raise RuntimeError("matter purge identity is invalid")
        matters_root = self.storage.matters
        matter_root = matters_root / matter.matter_id
        source_root = matter_root / "sources"
        quarantine_root = self.storage.purging
        quarantine = quarantine_root / purge_id

        if self.playback is not None and not self.playback.cancel_matter(
            matter.matter_id
        ):
            self.workspace.fail_matter_purge(
                matter.matter_id, purge_id, "media_playback"
            )
            raise WorkspaceProblem(
                "Matter deletion is waiting for browser playback preparation to stop. Try the deletion again shortly."
            )

        if self.media is not None and not self.media.cancel_matter(matter.matter_id):
            self.workspace.fail_matter_purge(
                matter.matter_id, purge_id, "media_processing"
            )
            raise WorkspaceProblem(
                "Matter deletion is waiting for an active media transfer to stop. Try the deletion again shortly."
            )

        try:
            self._cleanup_matter_clip_exports(matter.matter_id)
        except Exception as exc:
            self.workspace.fail_matter_purge(
                matter.matter_id, purge_id, "clip_exports"
            )
            raise WorkspaceProblem(
                "Matter deletion stopped while removing temporary work-product files. Try the deletion again."
            ) from exc

        try:
            if source_root.is_symlink() or matter_root.is_symlink():
                raise RuntimeError("matter storage is unsafe")
            if source_root.exists() and quarantine.exists():
                raise RuntimeError("matter purge storage has conflicting copies")
            if source_root.exists():
                store = self.source_store(matter)
                store.quarantine(quarantine)
                with self._store_lock:
                    self._stores.pop(matter.matter_id, None)
        except Exception as exc:
            self.workspace.fail_matter_purge(
                matter.matter_id, purge_id, "storage"
            )
            raise WorkspaceProblem(
                "Matter deletion stopped before completion. No ordinary access was restored; try the deletion again."
            ) from exc

        try:
            if self._postgres_projection_configured:
                if self.postgres_connection is None or not self.postgres_ready:
                    raise RuntimeError("derived search projection is unavailable")
                with self._postgres_lock:
                    delete_postgres_matter(
                        self.postgres_connection, matter_id=matter.matter_id
                    )
        except Exception as exc:
            self.workspace.fail_matter_purge(
                matter.matter_id, purge_id, "projection"
            )
            raise WorkspaceProblem(
                "Matter deletion stopped because derived search data could not be verified. Try again when search is available."
            ) from exc

        try:
            securely_delete_owned_tree(quarantine, allowed_parent=quarantine_root)
            if matter_root.exists():
                if matter_root.is_symlink() or matter_root.parent != matters_root:
                    raise RuntimeError("matter storage boundary is unsafe")
                with os.scandir(matter_root) as entries:
                    if any(True for _ in entries):
                        raise RuntimeError("matter storage contains an unhandled component")
                matter_root.rmdir()
                PilotStore._fsync_directory(matters_root)
        except Exception as exc:
            self.workspace.fail_matter_purge(
                matter.matter_id, purge_id, "storage"
            )
            raise WorkspaceProblem(
                "Matter deletion stopped while removing processing files. Try the deletion again."
            ) from exc

        try:
            return self.workspace.complete_matter_purge(matter.matter_id, purge_id)
        except Exception as exc:
            self.workspace.fail_matter_purge(
                matter.matter_id, purge_id, "control_store"
            )
            raise WorkspaceProblem(
                "Matter files were removed, but the closure record still needs attention. Try the deletion again."
            ) from exc

    def cleanup_abandoned_uploads(self, *, maximum_age_hours: int = 24) -> int:
        """Cancel old open sessions through the normal store and staging APIs."""

        cleaned = 0
        for session in self.workspace.abandoned_upload_sessions(
            maximum_age_hours=maximum_age_hours
        ):
            try:
                matter = self.workspace.get_matter_by_id(session.matter_id)
                cancelled = self.workspace.cancel_upload_session(
                    session.matter_id,
                    session.actor_id,
                    session.upload_session_id,
                )
                store = self.source_store(matter)
                for item in cancelled:
                    store.discard_resumable_upload(item.upload_item_id)
                self.workspace.append_audit_event(
                    actor_principal_id=None,
                    session_id=None,
                    matter_id=session.matter_id,
                    request_id=f"maintenance-{uuid.uuid4().hex}",
                    action="upload.cleanup",
                    outcome="success",
                    object_type="upload_session",
                    object_id=session.upload_session_id,
                    details={"count": len(cancelled), "state": "cancelled"},
                )
                cleaned += 1
            except Exception:
                try:
                    self.workspace.append_audit_event(
                        actor_principal_id=None,
                        session_id=None,
                        matter_id=session.matter_id,
                        request_id=f"maintenance-{uuid.uuid4().hex}",
                        action="upload.cleanup",
                        outcome="failure",
                        object_type="upload_session",
                        object_id=session.upload_session_id,
                        details={"state": "failed"},
                    )
                except Exception:
                    pass
        return cleaned

    def purge_due_matters(self) -> dict[str, int]:
        purged = 0
        deferred = 0
        for retention in self.workspace.due_matter_retentions():
            try:
                matter = self.workspace.get_matter_by_id(retention.matter_id)
                lifecycle = self.workspace.matter_lifecycle(matter.matter_id)
                lock = self._matter_source_locks.setdefault(
                    matter.matter_id, threading.RLock()
                )
                with lock:
                    if any(self.matter_active_work_counts(matter.matter_id).values()):
                        deferred += 1
                        continue
                    source_count = self._matter_source_count_for_purge(matter, lifecycle)
                    claimed = self.workspace.begin_due_matter_purge(
                        matter.matter_id,
                        source_count=source_count,
                    )
                if claimed is None:
                    deferred += 1
                    continue
                claimed_matter, claimed_lifecycle = claimed
                self.workspace.append_audit_event(
                    actor_principal_id=None,
                    session_id=None,
                    matter_id=claimed_matter.matter_id,
                    request_id=f"maintenance-{uuid.uuid4().hex}",
                    action="matter.purge_scheduled",
                    outcome="success",
                    object_type="matter_purge",
                    object_id=claimed_lifecycle.purge_id,
                    details={"state": "purging", "source_count": source_count},
                )
                completed = self.execute_matter_purge(
                    claimed_matter, claimed_lifecycle
                )
                self.workspace.append_audit_event(
                    actor_principal_id=None,
                    session_id=None,
                    matter_id=claimed_matter.matter_id,
                    request_id=f"maintenance-{uuid.uuid4().hex}",
                    action="matter.purge_scheduled",
                    outcome="success",
                    object_type="matter_purge",
                    object_id=completed.purge_id,
                    details={"state": "deleted", "source_count": source_count},
                )
                purged += 1
            except Exception:
                deferred += 1
        return {"matters_purged": purged, "matters_deferred": deferred}

    def run_maintenance_once(self) -> dict[str, int]:
        uploads_cleaned = self.cleanup_abandoned_uploads(maximum_age_hours=24)
        purge = self.purge_due_matters()
        return {"uploads_cleaned": uploads_cleaned, **purge}

    def _rebuild_all_indexes(self) -> None:
        """Incrementally reconcile derived search rows without a startup rebuild."""

        matters = self.workspace.all_matters()
        postgres_reconciliation_ready = bool(
            self.postgres_ready and self.postgres_connection is not None
        )
        if postgres_reconciliation_ready:
            try:
                retain_postgres_workbench_matters(
                    self.postgres_connection,
                    matter_ids=tuple(matter.matter_id for matter in matters),
                )
            except Exception:
                try:
                    self.postgres_connection.rollback()
                except Exception:
                    pass
                self.postgres_ready = False
                self.learned_retrieval = False
                postgres_reconciliation_ready = False
        for matter in matters:
            store = self.source_store(matter)
            if postgres_reconciliation_ready:
                try:
                    with self.postgres_connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT document_id,current_source_version_id FROM review_document "
                            "WHERE matter_id=%s",
                            (matter.matter_id,),
                        )
                        indexed = {row[0]: row[1] for row in cursor.fetchall()}
                    ready = {item.document_id: item for item in store.ready_documents()}
                    for document_id in indexed.keys() - ready.keys():
                        delete_postgres_document(
                            self.postgres_connection,
                            matter_id=matter.matter_id,
                            document_id=document_id,
                        )
                    for document in ready.values():
                        if indexed.get(document.document_id) != document.version_id:
                            self._index_document(matter, document)
                except Exception:
                    try:
                        self.postgres_connection.rollback()
                    except Exception:
                        pass
                    self.postgres_ready = False
                    self.learned_retrieval = False
                    postgres_reconciliation_ready = False

    @staticmethod
    def _kind(document: PilotDocument) -> str:
        if document.media_type == "application/pdf":
            return "PDF"
        if document.media_type == DOCX_MEDIA_TYPE:
            return "DOCX"
        if document.media_type in IMAGE_MEDIA_TYPES:
            return "IMAGE"
        if document.media_type in EMAIL_MEDIA_TYPES:
            return "EMAIL"
        if document.media_type in SPREADSHEET_MEDIA_TYPES:
            return "SPREADSHEET"
        if is_media_type(document.media_type):
            return "VIDEO" if document.has_video else "AUDIO"
        return "TXT"

    @staticmethod
    def _count_label(document: PilotDocument) -> str:
        if document.media_type == "application/pdf":
            count = document.page_count
            return f"{count} page{'s' if count != 1 else ''}" if count else "—"
        if document.media_type == DOCX_MEDIA_TYPE:
            count = document.page_count
            return f"{count} section{'s' if count != 1 else ''}" if count else "—"
        if document.media_type in IMAGE_MEDIA_TYPES:
            count = document.page_count
            return f"{count} frame{'s' if count != 1 else ''}" if count else "—"
        if document.media_type in EMAIL_MEDIA_TYPES:
            count = document.page_count
            return f"{count} section{'s' if count != 1 else ''}" if count else "—"
        if document.media_type in SPREADSHEET_MEDIA_TYPES:
            count = document.page_count
            return f"{count} section{'s' if count != 1 else ''}" if count else "—"
        if is_media_type(document.media_type):
            if not document.duration_ms:
                return "—"
            return format_timestamp(document.duration_ms)
        count = document.page_count
        return f"{count} line{'s' if count != 1 else ''}" if count else "—"

    def _ensure_source_organizations(self, matter: MatterRecord, store: PilotStore) -> None:
        self.workspace.reconcile_source_organizations(
            matter.matter_id,
            tuple(
                (
                    document.document_id,
                    document.origin,
                    document.relative_path or document.display_name,
                )
                for document in store.documents.values()
            ),
        )

    def _sync_source_catalog(
        self, matter: MatterRecord, documents: Sequence[PilotDocument]
    ) -> None:
        self.workspace.replace_source_catalog(
            matter.matter_id,
            tuple(self._source_catalog_projection(document) for document in documents),
        )

    def _apply_source_store_change(
        self, matter: MatterRecord, change: PilotStoreChange
    ) -> None:
        if change.upserted:
            self.workspace.upsert_source_catalog(
                matter.matter_id,
                tuple(
                    self._source_catalog_projection(document)
                    for document in change.upserted
                ),
            )
            if self.automatic_discovery is not None and any(
                document.state == "ready" for document in change.upserted
            ):
                self.automatic_discovery.wake(matter.matter_id)
        if change.removed_document_ids:
            self.workspace.delete_source_catalog(
                matter.matter_id, change.removed_document_ids
            )

    def _source_catalog_projection(
        self, document: PilotDocument
    ) -> Mapping[str, object]:
        state_label, tone = self._source_state(document)
        return {
            "document_id": document.document_id,
            "version_id": document.version_id,
            "action_token": PilotStore.action_token(document),
            "display_name": document.display_name,
            "relative_path": document.relative_path or document.display_name,
            "media_type": document.media_type,
            "kind": self._kind(document),
            "source_state": document.state,
            "tone": tone,
            "state_label": state_label,
            "count_label": self._count_label(document),
            "processing_stage": document.processing_stage,
            "completed_units": document.completed_units,
            "total_units": document.total_units,
            "page_count": document.page_count,
            "duration_ms": document.duration_ms,
            "byte_size": document.size,
            "source_sha256": document.digest,
            "origin": document.origin,
            "retryable": document.state in {"failed", "needs_ocr"},
            "removable": document.state not in {"queued", "processing"},
            "has_video": bool(document.has_video),
            "content_basis_digest": self._document_content_basis(document),
        }

    @staticmethod
    def _document_content_basis(document: PilotDocument) -> str:
        digest = hashlib.sha256()
        digest.update(('{"source_version":' + json.dumps(document.version_id) + ',"units":[').encode())
        for ordinal, unit in enumerate(document.iter_parsed_units()):
            if ordinal:
                digest.update(b',')
            digest.update(json.dumps({
                "number": unit.number, "digest": unit.excerpt_digest,
                "line_start": unit.line_start, "line_end": unit.line_end,
                "start_ms": unit.start_ms, "end_ms": unit.end_ms,
            }, separators=(",", ":"), sort_keys=True).encode())
        digest.update(b']}')
        return digest.hexdigest()

    @staticmethod
    def _source_state(document: PilotDocument) -> tuple[str, str]:
        if document.state == "ready":
            if document.media_type == "application/pdf" and document.message:
                return document.message, "ready"
            if "remain unreadable" in document.message or "no searchable text" in document.message:
                return document.message, "ready"
            return "Searchable", "ready"
        if document.state in {"queued", "processing"}:
            progress = ""
            if document.total_units:
                progress = (
                    f" · {document.completed_units}%"
                    if is_media_type(document.media_type)
                    else f" · {document.completed_units} of {document.total_units}"
                )
            return (document.processing_stage or "Processing") + progress, "processing"
        if document.state == "failed":
            return document.message, "attention"
        if document.state == "needs_ocr":
            return "Needs attention — scanned pages could not be read", "attention"
        return document.message or "Needs attention", "attention"

    def _source_rows(self, matter: MatterRecord) -> tuple[SourceRow, ...]:
        store = self.source_store(matter)
        self._ensure_source_organizations(matter, store)
        organizations = {
            item.document_id: item
            for item in self.workspace.source_organizations(matter.matter_id)
        }
        collections = {
            item.collection_id: item
            for item in self.workspace.source_collections(matter.matter_id)
        }
        rows: list[SourceRow] = []
        for document in store.documents.values():
            organization = organizations.get(document.document_id)
            if organization is None:
                raise RuntimeError("source organization reconciliation failed")
            collection = collections.get(organization.collection_id or "")
            state, tone = self._source_state(document)
            rows.append(
                SourceRow(
                    document.document_id,
                    document.display_name,
                    organization.relative_path,
                    self._kind(document),
                    state,
                    tone,
                    self._count_label(document),
                    store.action_token(document),
                    document.state in {"failed", "needs_ocr"},
                    document.state not in {"queued", "processing"},
                    organization.review_state,
                    organization.collection_id or "",
                    collection.name if collection is not None else "Unfiled",
                    organization.added_at,
                    document.size,
                    document.origin,
                )
            )
        return tuple(rows)

    def sources(self, matter: MatterRecord) -> tuple[SourceRow, ...]:
        """Return the complete inventory for exports and bounded internal uses."""

        return self._source_rows(matter)

    @staticmethod
    def _catalog_source_row(item) -> SourceRow:
        """Project one catalog row without materializing a matter manifest."""

        return SourceRow(
            item.document_id,
            item.display_name,
            item.relative_path,
            item.kind,
            item.state_label,
            item.tone,
            item.count_label,
            item.action_token,
            bool(item.retryable),
            bool(item.removable),
            item.review_state,
            item.collection_id,
            item.collection_name,
            item.added_at,
            item.byte_size,
            item.origin,
            item.byte_match_count,
        )

    def source_library(
        self,
        matter: MatterRecord,
        *,
        view: str = "overview",
        query: str = "",
        status: str = "",
        kind: str = "",
        review: str = "",
        collection_id: str = "",
        source_set_id: str = "",
        folder: str = "",
        same_content: str = "",
        matching_only: bool = False,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 50,
    ) -> SourceLibraryPage:
        view_value = view if view in {"overview", "list"} else "overview"
        query_value = " ".join((query or "").split())[:240]
        query_key = query_value.casefold()
        status_value = status if status in {"", "ready", "processing", "attention"} else ""
        kind_value = (
            kind.upper()
            if kind.upper()
            in {
                "PDF",
                "DOCX",
                "TXT",
                "AUDIO",
                "VIDEO",
                "IMAGE",
                "EMAIL",
                "SPREADSHEET",
            }
            else ""
        )
        review_value = review if review in {"", "unreviewed", "reviewed", "flagged"} else ""
        sort_value = sort if sort in {"newest", "oldest", "name", "name_desc", "status"} else "newest"
        page_size_value = page_size if page_size in {25, 50, 100} else 50
        source_set_value = source_set_id.strip()
        if source_set_value:
            self.workspace.source_set(matter.matter_id, source_set_value)
        folder_value = self.workspace.source_folder_path(folder)
        collection_value = collection_id.strip()
        if collection_value:
            self.workspace.source_collection(matter.matter_id, collection_value)
        if view_value == "overview" and not any(
            (query_value, status_value, kind_value, review_value, collection_value, source_set_value, folder_value, same_content, matching_only)
        ):
            page_size_value = 8
        requested_page = max(int(page), 1)
        catalog = self.workspace.source_catalog_page(
            matter.matter_id,
            query_key=query_key,
            tone=status_value,
            kind=kind_value,
            review_state=review_value,
            collection_id=collection_value,
            source_set_id=source_set_value,
            folder=folder_value,
            same_content=same_content, matching_only=matching_only,
            sort=sort_value,
            limit=page_size_value,
            offset=(requested_page - 1) * page_size_value,
        )
        total = catalog.total
        total_pages = max(math.ceil(total / page_size_value), 1)
        page_value = min(requested_page, total_pages)
        start = (page_value - 1) * page_size_value
        if page_value != requested_page:
            catalog = self.workspace.source_catalog_page(
                matter.matter_id,
                query_key=query_key,
                tone=status_value,
                kind=kind_value,
                review_state=review_value,
                collection_id=collection_value,
                source_set_id=source_set_value,
                folder=folder_value,
                same_content=same_content, matching_only=matching_only,
                sort=sort_value,
                limit=page_size_value,
                offset=start,
            )
        items = tuple(self._catalog_source_row(item) for item in catalog.items)
        stats = dict(catalog.stats)
        type_counts = {
            file_kind: int(catalog.type_counts.get(file_kind, 0))
            for file_kind in (
                "PDF",
                "DOCX",
                "TXT",
                "AUDIO",
                "VIDEO",
                "IMAGE",
                "EMAIL",
                "SPREADSHEET",
            )
        }
        review_counts = {
            state: int(catalog.review_counts.get(state, 0))
            for state in ("unreviewed", "reviewed", "flagged")
        }
        return SourceLibraryPage(
            items,
            page_value,
            page_size_value,
            total,
            total_pages,
            start + 1 if total else 0,
            min(start + len(items), total),
            stats,
            type_counts,
            review_counts,
            view_value,
            query_value,
            status_value,
            kind_value,
            review_value,
            collection_value,
            source_set_value,
            sort_value,
            folder_value,
            same_content,
            matching_only,
            not same_content or self.workspace.source_byte_comparison_available(matter.matter_id, same_content),
        )

    def source_review(
        self, matter: MatterRecord, token: str, *, unit_number: int = 1
    ) -> SourceReviewView:
        store = self.source_store(matter)
        self._ensure_source_organizations(matter, store)
        document = store.get_by_action_token(token)
        row = self._catalog_source_row(
            self.workspace.source_catalog_record(matter.matter_id, document.document_id)
        )
        units = document.parsed_units() if document.state == "ready" else ()
        if units:
            index = min(max(unit_number, 1), len(units)) - 1
            unit = units[index]
            if document.media_type == "application/pdf":
                position = f"Page {unit.number} of {document.page_count or len(units)}"
            elif document.media_type == DOCX_MEDIA_TYPE:
                position = f"Section {unit.number} of {len(units)}"
            else:
                position = unit.location
        else:
            index = 0
            unit = None
            position = "Content is not searchable yet"

        def adjacent(target: int) -> str:
            if target < 0 or target >= len(units):
                return ""
            return f"/matters/{matter.slug}/sources/{token}?unit={target + 1}"

        embedded = ""
        if document.origin == "upload" and (
            document.media_type == "application/pdf"
            or document.media_type in IMAGE_MEDIA_TYPES
        ):
            embedded = f"/matters/{matter.slug}/sources/{token}/content"
            if unit is not None and document.media_type == "application/pdf":
                embedded += f"#page={unit.number}"
        return SourceReviewView(
            row,
            document,
            unit,
            index + 1,
            len(units),
            position,
            adjacent(index - 1),
            adjacent(index + 1),
            embedded,
        )

    def media_review(
        self,
        matter: MatterRecord,
        token: str,
        *,
        start_ms: int = 0,
        focus_segment_id: str = "",
    ) -> MediaReviewView:
        store = self.source_store(matter)
        self._ensure_source_organizations(matter, store)
        document = store.get_by_action_token(token)
        if not is_media_type(document.media_type):
            raise KeyError(token)
        if self.playback is not None and document.has_video:
            document = self.playback.ensure(matter, document)
        row = self._catalog_source_row(
            self.workspace.source_catalog_record(matter.matter_id, document.document_id)
        )
        job = self.workspace.media_job(
            matter.matter_id, document.document_id, document.version_id
        )
        transcript = self.workspace.media_transcript(
            matter.matter_id, document.document_id, document.version_id
        )
        summary = self.workspace.media_summary(
            matter.matter_id, document.document_id, document.version_id
        ) if transcript is not None else None
        segments = self.workspace.transcript_segments(
            matter.matter_id, document.document_id, document.version_id
        ) if transcript is not None else ()
        speakers = self.workspace.transcript_speakers(
            matter.matter_id, document.document_id, document.version_id
        ) if transcript is not None else ()
        clips = self.workspace.media_clips(
            matter.matter_id, document.document_id, document.version_id
        ) if transcript is not None else ()
        bounded_start = min(max(int(start_ms), 0), max(document.duration_ms - 1, 0))
        focus = focus_segment_id if any(
            item.segment_id == focus_segment_id for item in segments
        ) else ""
        return MediaReviewView(
            row=row,
            document=document,
            job=job,
            transcript=transcript,
            summary=summary,
            segments=segments,
            speakers=speakers,
            clips=clips,
            playback_href=f"/matters/{matter.slug}/sources/{token}/content",
            playback_media_type=(
                document.playback_media_type
                if document.playback_state in {"original", "ready"}
                and document.playback_media_type
                else document.media_type
            ),
            playback_is_compatible_copy=document.playback_state == "ready",
            player_kind="video" if document.has_video else "audio",
            start_ms=bounded_start,
            focus_segment_id=focus,
        )

    def _candidate(
        self, matter: MatterRecord, document: PilotDocument, unit: PilotUnit, ordinal: int
    ) -> Candidate:
        return Candidate(
            matter.matter_id,
            document.document_id,
            f"chunk-{ordinal}",
            document.display_name,
            unit.number,
            unit.text,
            line_start=unit.line_start,
            line_end=unit.line_end,
            source_version_id=document.version_id,
            excerpt_digest=unit.excerpt_digest,
            evidence_kind=(
                "transcript" if is_media_type(document.media_type) else "document"
            ),
        )

    def _candidates(
        self,
        matter: MatterRecord,
        document_ids: frozenset[str] | None = None,
        maximum: int | None = None,
    ) -> tuple[Candidate, ...]:
        store = self.source_store(matter)
        candidates: list[Candidate] = []
        limit = max(int(maximum), 0) if maximum is not None else None
        for document in store.ready_documents():
            if document_ids is not None and document.document_id not in document_ids:
                continue
            for ordinal, unit in enumerate(document.parsed_units(), 1):
                candidates.append(self._candidate(matter, document, unit, ordinal))
                if limit is not None and len(candidates) >= limit:
                    return tuple(candidates)
        return tuple(candidates)

    def _retriever(
        self,
        matter: MatterRecord,
        document_ids: frozenset[str] | None = None,
    ) -> HybridRetriever:
        if self.learned_retrieval and self.postgres_ready and self.postgres_connection is not None:
            return HybridRetriever(
                PostgresHybridBackend(
                    self.postgres_connection,
                    lock=self._postgres_lock,
                    document_ids=document_ids,
                ),
                self.embedding,
                self.reranker,
                minimum_dense_score=0.72,
                minimum_rerank_score=0.80,
            )
        return HybridRetriever(
            InMemoryHybridBackend(
                self._candidates(matter, document_ids),
                document_ids=document_ids,
            ),
            DeterministicEmbeddingAdapter(),
            DeterministicReranker(),
        )

    @staticmethod
    def _support_token(candidate: Candidate) -> str:
        if candidate.evidence_kind == "transcript":
            material = "\x00".join(
                (
                    "case-intelligence-transcript-support-v2",
                    candidate.matter_id,
                    candidate.document_id,
                    candidate.source_version_id,
                    candidate.chunk_id,
                    str(candidate.line_start if candidate.line_start is not None else ""),
                    str(candidate.line_end if candidate.line_end is not None else ""),
                )
            )
            return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]
        return CaseIntelligenceWorkbench._legacy_support_token(candidate)

    @staticmethod
    def _legacy_support_token(candidate: Candidate) -> str:
        material = "\x00".join(
            (
                "case-intelligence-support-v1",
                candidate.matter_id,
                candidate.document_id,
                candidate.source_version_id,
                candidate.chunk_id,
                candidate.excerpt_digest,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]

    @classmethod
    def _support_tokens(cls, candidate: Candidate) -> frozenset[str]:
        """Return exact current tokens, including the pre-v2 transcript identity."""

        return frozenset(
            (cls._support_token(candidate), cls._legacy_support_token(candidate))
        )

    def _citation(self, matter: MatterRecord, candidate: Candidate, store: PilotStore | None = None) -> WorkbenchCitation:
        evidence_kind = "document"
        try:
            document = (store if store is not None else self.source_store(matter)).get(candidate.document_id)
        except KeyError:
            document = None
        if document is not None and is_media_type(document.media_type):
            evidence_kind = "transcript"
        # The PostgreSQL retrieval projection predates media evidence and can
        # return transcript chunks with Candidate's default "document" kind.
        # Canonicalize from the authoritative matter source before deriving the
        # support token so answer-finalization validates the same token later.
        if candidate.evidence_kind != evidence_kind:
            candidate = replace(candidate, evidence_kind=evidence_kind)
        token = self._support_token(candidate)
        href = f"/matters/{matter.slug}?support={token}#support-pane"
        if evidence_kind == "transcript":
            href = f"/matters/{matter.slug}?support={token}&play=1#support-pane"
        return WorkbenchCitation(
            candidate.source_name,
            candidate.citation,
            candidate.text,
            href,
            token,
            candidate.matter_id,
            candidate.document_id,
            candidate.source_version_id,
            candidate.excerpt_digest,
            candidate.chunk_id,
            candidate.page_number,
            candidate.line_start,
            candidate.line_end,
            evidence_kind,
        )

    def _validate_candidates(
        self, matter: MatterRecord, candidates: Sequence[Candidate]
    ) -> bool:
        store = self.source_store(matter)
        for candidate in candidates:
            if candidate.matter_id != matter.matter_id:
                return False
            try:
                document = store.get(candidate.document_id)
            except KeyError:
                return False
            matched = any(
                unit.number == candidate.page_number
                and unit.text == candidate.text
                and unit.line_start == candidate.line_start
                and unit.line_end == candidate.line_end
                and unit.excerpt_digest == candidate.excerpt_digest
                for unit in document.parsed_units()
            )
            if (
                document.state != "ready"
                or document.version_id != candidate.source_version_id
                or not matched
            ):
                return False
        return True

    def exact_search(
        self, matter: MatterRecord, query: str, *, source_set_id: str = "",
        collection_id: str = "", page: int = 1, page_size: int = 25,
        expected_fingerprint: str = "",
    ) -> ExactSearchPage:
        """Enumerate an already-authorized matter, independently of retrieval."""
        store = self.source_store(matter)
        # Source changes project into workspace while holding this same lock.
        with store._lock:
            store._ensure_active()
            # Capture organization membership briefly; text matching must not
            # hold the global workspace lock or block work in other matters.
            with self.workspace._lock:
                allowed = None
                if source_set_id:
                    allowed = self.workspace.source_set_document_ids(matter.matter_id, source_set_id)
                if collection_id:
                    self.workspace.source_collection(matter.matter_id, collection_id)
                    members = frozenset(item.document_id for item in
                        self.workspace.source_organizations(matter.matter_id)
                        if item.collection_id == collection_id)
                    allowed = members if allowed is None else allowed & members
            documents = (item for item in store.documents.values()
                         if allowed is None or item.document_id in allowed)
            return self.exact_search_backend.search(
                documents, query, scope=(matter.matter_id, source_set_id, collection_id),
                page=page, page_size=page_size, expected_fingerprint=expected_fingerprint,
            )

    def search(
        self,
        matter: MatterRecord,
        query: str,
        *,
        limit: int = 12,
        stage_callback: Callable[[str], None] | None = None,
        document_ids: frozenset[str] | None = None,
    ) -> tuple[WorkbenchCitation, ...]:
        value = " ".join((query or "").split()).strip()
        if not value:
            return ()
        if len(value) > MAX_SEARCH_CHARS:
            raise WorkspaceProblem("Search is too long.")
        try:
            candidates = self._retriever(matter, document_ids).search(
                matter.matter_id,
                value,
                limit=limit,
                stage_callback=stage_callback,
            )
        except Exception as exc:
            raise RetrievalUnavailable("matter search could not run") from exc
        if not self._validate_candidates(matter, candidates):
            raise RetrievalUnavailable("matter search returned invalid support")
        return tuple(self._citation(matter, item) for item in candidates)

    def _answer_source_boundary(
        self, matter: MatterRecord, source_set_id: str | None,
    ) -> tuple[frozenset[str] | None, dict[str, str]]:
        if not source_set_id:
            return None, {}
        self.source_store(matter)
        with self.workspace._lock:
            document_ids = self.workspace.source_set_document_ids(matter.matter_id, source_set_id)
            fingerprint = self.workspace.source_availability_fingerprint(matter.matter_id, source_set_id)
        return document_ids, {"source_set_id": source_set_id, "source_fingerprint": fingerprint}

    def _answer_search(
        self,
        matter: MatterRecord,
        question: str,
        retrieval_query: str,
        *,
        stage_callback: Callable[[str], None] | None = None,
        document_ids: frozenset[str] | None = None,
        expand_broad_summary: bool = True,
        primary_limit: int = 20,
        retrieval_boundary: dict[str, str] | None = None,
    ) -> tuple[WorkbenchCitation, ...]:
        """Run bounded answer retrieval with explicit scope and modality policy."""

        if retrieval_boundary is not None and not retrieval_boundary.get("source_fingerprint"):
            self.source_store(matter)
            retrieval_boundary["source_fingerprint"] = self.workspace.source_availability_fingerprint(
                matter.matter_id, retrieval_boundary.get("source_set_id"))
        intent = classify_question(question)
        queries = (
            broad_summary_queries(retrieval_query, maximum_chars=MAX_SEARCH_CHARS)
            if intent.broad_summary and expand_broad_summary
            else (retrieval_query,)
        )
        combined: list[WorkbenchCitation] = []
        seen: set[str] = set()
        excluded_kinds = frozenset(intent.excluded_evidence_kinds)

        def extend(rows: Sequence[WorkbenchCitation]) -> None:
            for row in rows:
                if (
                    row.evidence_kind not in excluded_kinds
                    and row.support_token not in seen
                ):
                    combined.append(row)
                    seen.add(row.support_token)

        for index, query in enumerate(queries):
            extend(
                self.search(
                    matter,
                    query,
                    limit=validate_primary_limit(primary_limit),
                    stage_callback=stage_callback if index == 0 else None,
                    document_ids=document_ids,
                )
            )

        # An explicit source-kind request gets one bounded retrieval attempt
        # inside each requested kind when the ordinary top set omitted it.
        # Every scoped search still passes the same matter and citation checks.
        present_kinds = {item.evidence_kind for item in combined}
        missing_kinds = tuple(
            kind for kind in intent.required_evidence_kinds if kind not in present_kinds
        )
        if missing_kinds:
            store = self.source_store(matter)
            allowed = document_ids
            for kind in missing_kinds:
                scoped = frozenset(
                    document.document_id
                    for document in store.ready_documents()
                    if (allowed is None or document.document_id in allowed)
                    and (
                        is_media_type(document.media_type)
                        if kind == "transcript"
                        else not is_media_type(document.media_type)
                    )
                )
                if scoped:
                    extend(
                        self.search(
                            matter,
                            retrieval_query,
                            limit=DEFAULT_REVIEW_BUDGET.supplemental_candidates_per_kind,
                            document_ids=scoped,
                        )
                    )
        rows = tuple(combined)
        if intent.broad_summary:
            rows = filter_broad_summary_evidence(rows)
        return rows

    def _completion_source_coverage(
        self, matter: MatterRecord, retrieval_boundary: Mapping[str, str],
    ) -> tuple[MatterReadinessRecord, dict[str, object]]:
        # Read counts and the identity boundary under the same control-store
        # lock, without holding source mutations across retrieval/generation.
        with self.workspace._lock:
            readiness = self.workspace.matter_readiness(matter.matter_id)
            current = self.workspace.source_availability_fingerprint(
                matter.matter_id, retrieval_boundary.get("source_set_id"))
        return readiness, _source_coverage(readiness,
            changed_since_retrieval=retrieval_boundary.get("source_fingerprint") != current)

    def _answer_evidence_citations(
        self,
        matter: MatterRecord,
        citations: Sequence[WorkbenchCitation],
        *,
        maximum: int = 12,
        required_kinds: Sequence[str] = (),
        candidate_callback: Callable[[WorkbenchCitation], None] | None = None,
    ) -> tuple[WorkbenchCitation, ...]:
        """Diversify top anchors, then use remaining slots for media neighbors."""

        limit = min(max(int(maximum), 1), 12)
        anchor_limit = min(limit, 8)
        ordered = prioritize_evidence_kinds(
            citations,
            maximum=len(citations),
            required_kinds=required_kinds,
        )
        anchors: list[WorkbenchCitation] = []
        document_counts: dict[str, int] = {}
        for citation in ordered:
            if document_counts.get(citation.document_id, 0) >= 2:
                continue
            anchors.append(citation)
            document_counts[citation.document_id] = (
                document_counts.get(citation.document_id, 0) + 1
            )
            if len(anchors) >= anchor_limit:
                break
        # A highly focused result can legitimately need more than two passages
        # from one source. Backfill only when source diversity left empty slots.
        if len(anchors) < anchor_limit:
            anchor_tokens = {citation.support_token for citation in anchors}
            for citation in ordered:
                if citation.support_token in anchor_tokens:
                    continue
                anchors.append(citation)
                anchor_tokens.add(citation.support_token)
                if len(anchors) >= anchor_limit:
                    break
        selected: list[WorkbenchCitation] = []
        seen: set[str] = set()

        def append(item: WorkbenchCitation) -> None:
            if len(selected) < limit and item.support_token not in seen:
                selected.append(item)
                seen.add(item.support_token)

        for citation in tuple(anchors):
            append(citation)
        if len(selected) >= limit:
            return tuple(selected)

        store = self.source_store(matter)
        for citation in tuple(anchors):
            if len(selected) >= limit:
                break
            try:
                document = store.get(citation.document_id)
            except KeyError:
                continue
            if (
                not is_media_type(document.media_type)
                or document.state != "ready"
                or document.version_id != citation.source_version_id
            ):
                continue
            units = document.parsed_units()
            anchor_index = citation.unit_number - 1
            if not 0 <= anchor_index < len(units):
                continue
            anchor = units[anchor_index]
            if (
                anchor.excerpt_digest != citation.excerpt_digest
                or anchor.line_start != citation.line_start
                or anchor.line_end != citation.line_end
            ):
                continue
            for neighbor_index in (anchor_index - 1, anchor_index + 1):
                if not 0 <= neighbor_index < len(units):
                    continue
                candidate = self._candidate(
                    matter,
                    document,
                    units[neighbor_index],
                    neighbor_index + 1,
                )
                neighbor = self._citation(matter, candidate)
                if candidate_callback is not None:
                    candidate_callback(neighbor)
                append(neighbor)
                if len(selected) >= limit:
                    break
        return tuple(selected)

    def _find_support(
        self, matter: MatterRecord, token: str
    ) -> tuple[PilotDocument, tuple[PilotUnit, ...], int]:
        if not re.fullmatch(r"[0-9a-f]{40}", token):
            raise KeyError(token)
        store = self.source_store(matter)
        for document in store.ready_documents():
            try:
                units = document.parsed_units()
            except (OSError, RuntimeError, TypeError, ValueError):
                # Unreadable derived text makes only this source's passages
                # unavailable; every other source's support still opens.
                continue
            for ordinal, unit in enumerate(units, 1):
                candidate = self._candidate(matter, document, unit, ordinal)
                if token in self._support_tokens(candidate):
                    return document, units, ordinal - 1
            if not is_media_type(document.media_type):
                continue
            history = self.workspace.transcript_support_history(
                matter.matter_id,
                document.document_id,
                document.version_id,
            )
            for ordinal, excerpts in history.items():
                index = ordinal - 1
                if not 0 <= index < len(units):
                    continue
                candidate = self._candidate(matter, document, units[index], ordinal)
                for excerpt in excerpts:
                    historical = Candidate(
                        candidate.matter_id,
                        candidate.document_id,
                        candidate.chunk_id,
                        candidate.source_name,
                        candidate.page_number,
                        excerpt,
                        line_start=candidate.line_start,
                        line_end=candidate.line_end,
                        source_version_id=candidate.source_version_id,
                        excerpt_digest=hashlib.sha256(
                            excerpt.encode("utf-8")
                        ).hexdigest(),
                        evidence_kind="transcript",
                    )
                    if self._legacy_support_token(historical) == token:
                        return document, units, index
        raise KeyError(token)

    def support(
        self,
        matter: MatterRecord,
        token: str,
        *,
        conversation_id: str = "",
        autoplay: bool = False,
    ) -> SupportView:
        document, units, index = self._find_support(matter, token)
        unit = units[index]
        evidence_kind = "document"
        source_review_href = ""
        playback_href = ""
        playback_media_type = ""
        player_kind = ""
        start_ms = 0
        end_ms = 0
        action_token = self.source_store(matter).action_token(document)
        if is_media_type(document.media_type):
            evidence_kind = "transcript"
            candidate = self._candidate(matter, document, unit, index + 1)
            location = candidate.citation
            position = f"Transcript passage {index + 1} of {len(units)}"
            segments = self.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            )
            if index < len(segments) and (
                segments[index].current_revision
                or segments[index].speaker_identity_state == "confirmed"
            ):
                position += " · current reviewed text"
            start_ms = max(int(unit.start_ms or unit.line_start or 0), 0)
            end_ms = max(int(unit.end_ms or unit.line_end or start_ms), start_ms)
            source_review_href = (
                _query_url(
                    f"/matters/{matter.slug}/sources/{action_token}",
                    start_ms=str(start_ms),
                )
                + f"#segment-{index + 1}"
            )
            playback_href = (
                f"/matters/{matter.slug}/sources/{action_token}/content"
            )
            if self.playback is not None and document.has_video:
                document = self.playback.ensure(matter, document)
            playback_media_type = (
                document.playback_media_type
                if document.playback_state in {"original", "ready"}
                and document.playback_media_type
                else document.media_type
            )
            player_kind = "video" if document.has_video else "audio"
        elif unit.line_start is not None:
            location = unit.location
            position = location
            source_review_href = _query_url(
                f"/matters/{matter.slug}/sources/{action_token}",
                unit=str(index + 1),
            )
        elif document.media_type == DOCX_MEDIA_TYPE:
            location = f"Section {unit.number}"
            position = f"Section {unit.number} of {len(units)}"
            source_review_href = _query_url(
                f"/matters/{matter.slug}/sources/{action_token}",
                unit=str(index + 1),
            )
        else:
            location = f"Page {unit.number}"
            position = f"Page {unit.number} of {document.page_count or len(units)}"
            source_review_href = _query_url(
                f"/matters/{matter.slug}/sources/{action_token}",
                unit=str(index + 1),
            )

        def adjacent(target_index: int) -> str:
            if target_index < 0 or target_index >= len(units):
                return ""
            target = self._candidate(matter, document, units[target_index], target_index + 1)
            return _query_url(
                f"/matters/{matter.slug}",
                support=self._support_token(target),
                conversation=conversation_id,
                play="1" if evidence_kind == "transcript" else "",
            ) + "#support-pane"

        return SupportView(
            document.display_name,
            self._kind(document),
            location,
            position,
            tuple(unit.text.splitlines()) or (unit.text,),
            adjacent(index - 1),
            adjacent(index + 1),
            token,
            evidence_kind,
            source_review_href,
            playback_href,
            playback_media_type,
            player_kind,
            start_ms,
            end_ms,
            bool(autoplay),
        )

    def entity_service(self, matter, *, automatic=False):
        from .entity_service import EntityService, current_reference_indexes, current_source_indexes
        return EntityService(
            self.workspace.entity_repository(automatic=automatic),
            source_guard=self.source_store(matter).mutation_guard,
            resolve_support=lambda token: self.notebook_reference_from_support(matter, token),
            load_note=self.workspace.notebook_item,
            load_references=self.workspace.notebook_references,
            validate_references=lambda references: current_reference_indexes(references,
                load_document=self.source_store(matter).get,
                candidate_for=lambda document, unit, ordinal: self._candidate(matter, document, unit, ordinal),
                support_tokens=self._support_tokens,
                read_units=lambda document, ordinals: self.entity_unit_reader.iter_selected(
                    self.source_store(matter), document, ordinals)),
            current_sources=lambda references: current_source_indexes(references, self.source_store(matter).get),
        )

    def assertion_service(self, matter):
        from .assertion_service import AssertionService
        return AssertionService(self.workspace.assertion_repository(), self.entity_service(matter))

    def entity_discovery(self, matter):
        from itertools import groupby
        from .entity_discovery import EntityDiscovery
        def load_units(coverages):
            for document_id, group in groupby(coverages, key=lambda row: row['document_id']):
                requested = {row['unit_ordinal']: row for row in group}
                try:
                    document = self.source_store(matter).get(document_id)
                    if document.state != 'ready':
                        raise KeyError(document_id)
                    for ordinal, unit in self.entity_unit_reader.iter_selected(self.source_store(matter), document, requested):
                        coverage = requested.get(ordinal)
                        if coverage is not None:
                            loaded = (unit.text, self._discovery_reference(matter, document, unit, ordinal))
                            requested.pop(ordinal)
                            yield coverage, loaded
                        if not requested:
                            break
                except (KeyError, OSError, ValueError, RuntimeError, TypeError):
                    pass
                for coverage in requested.values():
                    yield coverage, None
        return EntityDiscovery(self.entity_service(matter), load_units=load_units)

    def _discovery_reference(self, matter, document, unit, ordinal):
        candidate = self._candidate(matter, document, unit, ordinal)
        token = sorted(self._support_tokens(candidate))[0]
        return dict(document_id=candidate.document_id,
            source_version_id=candidate.source_version_id, source_name=candidate.source_name,
            location=candidate.citation, unit_number=unit.number, chunk_id=candidate.chunk_id,
            excerpt_digest=candidate.excerpt_digest, excerpt=candidate.text[:6000], support_token=token)

    def automatic_entity_discovery(self, matter, should_stop=None):
        """Discovery that runs as the inactive system principal after processing.

        should_stop() is also polled for every bounded read inside the source
        readers, so shutdown can interrupt a long scan or index build.
        """
        from .entity_discovery import DiscoveryStopped, EntityDiscovery
        def read_check(_chars):
            if should_stop():
                raise DiscoveryStopped()
        check = read_check if should_stop is not None else None
        def load_document(document_id, source_version_id, ordinals=None):
            store = self.source_store(matter)
            document = store.get(document_id)
            if document.state != 'ready' or document.version_id != source_version_id:
                raise KeyError(document_id)
            if ordinals is None:
                parsed = (document.iter_parsed_units(read_check=check) if check is not None
                          else document.iter_parsed_units())
                units = enumerate(parsed, 1)
            else:
                units = self.entity_unit_reader.iter_selected(store, document, ordinals, read_check=check)
            for ordinal, unit in units:
                if ordinal > MAX_AUTOMATIC_DISCOVERY_UNITS:
                    raise ValueError('Source exceeds the automatic discovery unit ceiling.')
                yield ordinal, unit.text, self._discovery_reference(matter, document, unit, ordinal)
        return EntityDiscovery(self.entity_service(matter, automatic=True), load_document=load_document)

    def automatic_discovery_progress(self, matter):
        """Coverage counts when automatic discovery is enabled; otherwise None."""
        if self.automatic_discovery is None:
            return None
        try:
            return self.automatic_entity_discovery(matter).automatic_progress(matter.matter_id)
        except KeyError:
            return None

    def wake_automatic_discovery(self, matter):
        if self.automatic_discovery is not None:
            self.automatic_discovery.wake(matter.matter_id)

    def run_automatic_discovery_once(self, matter_ids=None, *, unit_limit=200):
        """Process newly ready sources for active matters; returns units handled."""
        return self.automatic_discovery_pass(matter_ids, unit_limit=unit_limit).units

    def automatic_discovery_pass(self, matter_ids=None, *, unit_limit=200,
                                 should_stop=None) -> "AutomaticDiscoveryPass":
        # unit_limit applies to each matter, so one large matter cannot starve the rest.
        """One bounded pass over active matters.

        Sealing a source (including an empty or unavailable one) counts as
        progress, so callers continue instead of waiting for the next sweep.
        A failure in one matter is recorded and the remaining matters still run.
        Every committed unit is audited in the same transaction as the unit.
        """
        units = progress = 0
        failed = []
        if matter_ids is None:
            matter_ids = self.workspace.active_matter_ids()
        stopping = should_stop or (lambda: False)
        for matter_id in matter_ids:
            if stopping():
                break
            try:
                matter = self.workspace.get_matter_by_id(matter_id)
            except KeyError:
                continue
            run_id = f"automatic-discovery-{uuid.uuid4().hex}"
            def audit_unit(event, matter=matter, run_id=run_id):
                # Called inside the unit's repository transaction (same
                # connection, re-entrant lock), so the record commits with it.
                self.workspace._append_audit_event_locked(
                    actor_principal_id=AUTOMATIC_DISCOVERY_PRINCIPAL, session_id=None,
                    matter_id=matter.matter_id, request_id=run_id, action="entity.discovery_unit",
                    outcome="success" if event["state"] == "processed" else "failure",
                    object_type="source", object_id=event["document_id"],
                    details={"count": event["count"], "unit_count": 1,
                             "state": {"processed": "completed", "failed": "failed",
                                       "invalidated": "attention"}[event["state"]]})
            matter_units = 0
            try:
                discovery = self.automatic_entity_discovery(matter, should_stop=stopping)
                discovery.prune_automatic(matter.matter_id)
                for _ in range(MAX_AUTOMATIC_DISCOVERY_STEPS):
                    if matter_units >= unit_limit or stopping():
                        break
                    try:
                        handled, sealed, retried = discovery.automatic_step(
                            matter.matter_id, unit_limit=min(25, unit_limit - matter_units),
                            audit_unit=audit_unit, should_stop=stopping)
                    except KeyError:
                        # Expected only when the matter just closed; anything else
                        # (such as a refused system principal) must be reported.
                        try:
                            self.workspace.get_matter_by_id(matter.matter_id)
                        except KeyError:
                            break
                        raise
                    if not handled and not sealed and not retried:
                        break
                    # Sealing and waiting-for-space attempts use the allowance too,
                    # so a matter that cannot admit anything does bounded work.
                    # Repeated attempts are not progress: the worker then waits
                    # for the next sweep or wake instead of retrying at once.
                    matter_units += handled + sealed + retried
                    units += handled
                    progress += handled + sealed
            except Exception:
                # Keep one matter's unreadable sources from stalling the others,
                # and leave a durable, content-free record for administrators.
                failed.append(matter_id)
                LOGGER.warning("Automatic discovery skipped a matter after an error.", exc_info=True)
                if matter_id not in self._automatic_discovery_reported:
                    try:
                        self.workspace.append_audit_event(
                            actor_principal_id=None, session_id=None, matter_id=matter_id,
                            request_id=run_id, action="entity.discovery_automatic", outcome="failure",
                            object_type="matter", object_id=matter_id, details={"state": "attention"})
                        self._automatic_discovery_reported.add(matter_id)
                    except Exception:
                        LOGGER.warning("Automatic discovery could not record a matter failure.", exc_info=True)
            else:
                self._automatic_discovery_reported.discard(matter_id)
        return AutomaticDiscoveryPass(units=units, progress=progress, failed_matters=tuple(failed))

    def notebook_reference_from_support(
        self, matter: MatterRecord, token: str
    ) -> dict[str, object]:
        document, units, index = self._find_support(matter, token)
        unit = units[index]
        candidate = self._candidate(matter, document, unit, index + 1)
        citation = self._citation(matter, candidate)
        if token not in self._support_tokens(candidate):
            raise KeyError(token)
        return {
            "document_id": candidate.document_id,
            "source_version_id": candidate.source_version_id,
            "source_name": candidate.source_name,
            "location": candidate.citation,
            "unit_number": unit.number,
            "chunk_id": candidate.chunk_id,
            "excerpt_digest": candidate.excerpt_digest,
            "excerpt": candidate.text[:6_000],
            "support_token": token,
        }

    def available_notebook_support_tokens(
        self,
        matter: MatterRecord,
        references: Sequence[NotebookReferenceRecord],
    ) -> frozenset[str]:
        store = self.source_store(matter)
        by_document: dict[str, list[NotebookReferenceRecord]] = {}
        for reference in references:
            by_document.setdefault(reference.document_id, []).append(reference)
        available: set[str] = set()
        for document_id, expected in by_document.items():
            try:
                document = store.get(document_id)
            except KeyError:
                continue
            if document.state != "ready":
                continue
            units = document.parsed_units()
            for reference in expected:
                try:
                    ordinal = int(reference.chunk_id.removeprefix("chunk-"))
                except ValueError:
                    continue
                if not 1 <= ordinal <= len(units):
                    continue
                unit = units[ordinal - 1]
                candidate = self._candidate(matter, document, unit, ordinal)
                if (
                    document.version_id == reference.source_version_id
                    and unit.number == reference.unit_number
                    and unit.excerpt_digest == reference.excerpt_digest
                    and reference.support_token in self._support_tokens(candidate)
                ):
                    available.add(reference.support_token)
        return frozenset(available)

    @staticmethod
    def _citation_support_token(value: object) -> str:
        if not isinstance(value, Mapping):
            raise KeyError("citation")
        href = value.get("href")
        if not isinstance(href, str):
            raise KeyError("citation")
        tokens = parse_qs(urlparse(href).query).get("support", [])
        if len(tokens) != 1 or not re.fullmatch(r"[0-9a-f]{40}", tokens[0]):
            raise KeyError("citation")
        return tokens[0]

    @staticmethod
    def _notebook_title(value: str, maximum: int = 120) -> str:
        title = " ".join(value.split()).strip()
        if len(title) <= maximum:
            return title
        shortened = title[: maximum - 1].rstrip()
        if " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0].rstrip(".,;:!?-")
        return (shortened or title[: maximum - 1]).rstrip() + "…"

    def save_answer_to_notebook(
        self,
        matter: MatterRecord,
        actor_id: str,
        conversation_id: str,
        message_id: str,
        claim_index: int,
        *,
        citation_index: int | None = None,
    ) -> tuple[NotebookItemRecord, bool]:
        self.workspace.membership(matter.matter_id, actor_id)
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            return self._save_answer_to_notebook(matter, actor_id, conversation_id,
                message_id, claim_index, citation_index=citation_index)

    def _answer_claim(self, matter: MatterRecord, conversation_id: str, message_id: str,
                      claim_index: int) -> Mapping[str, object]:
        """One verified claim of a saved generated answer, or KeyError."""
        messages = self.workspace.messages(matter.matter_id, conversation_id)
        message = next(
            (
                item
                for item in messages
                if item.message_id == message_id and item.role == "assistant"
            ),
            None,
        )
        if message is None or message.payload.get("kind") != "generated":
            raise KeyError(message_id)
        claims = message.payload.get("claims")
        if not isinstance(claims, list) or not 0 <= claim_index < len(claims):
            raise KeyError(str(claim_index))
        claim = claims[claim_index]
        if not isinstance(claim, Mapping) or not isinstance(claim.get("text"), str):
            raise KeyError(str(claim_index))
        return claim

    def _verified_answer_units(self, matter: MatterRecord, citations: Sequence[Mapping[str, object]]):
        """One bounded check of saved citations, returning their current units; all or nothing."""
        from .report_materials import resolve_saved_answer_units

        values = []
        for citation in citations:
            token = self._citation_support_token(citation)
            if citation.get("support_token") not in (None, "", token):
                raise WorkspaceProblem("The saved answer has conflicting source support. Ask the question again in a new conversation.")
            values.append({**citation, "support_token": token})
        return resolve_saved_answer_units(self, matter, values)

    def _answer_claim_passages(self, matter: MatterRecord, conversation_id: str, message_id: str,
                               claim_index: int):
        """The claim's distinct cited passages (up to 12) for discovery.

        Call under the source mutation guard and the workspace lock. Any changed
        or unavailable passage refuses the whole claim.
        """
        claim = self._answer_claim(matter, conversation_id, message_id, claim_index)
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations:
            raise WorkspaceProblem("That answer passage no longer has source support.")
        selected, tokens = [], set()
        for citation in citations:
            if not isinstance(citation, Mapping):
                raise WorkspaceProblem("The cited passages changed or are unavailable, so nothing was suggested.")
            token = self._citation_support_token(citation)
            if token not in tokens and len(selected) < 12:
                tokens.add(token)
                selected.append(citation)
        try:
            units = self._verified_answer_units(matter, selected)
        except WorkspaceProblem as exc:
            raise WorkspaceProblem("The cited passages changed or are unavailable, so nothing was suggested.") from exc
        passages, seen = [], set()
        for _reference, document, unit, ordinal in units:
            key = (document.document_id, ordinal)
            if key in seen:
                continue
            seen.add(key)
            passages.append((
                dict(document_id=document.document_id, source_version_id=document.version_id,
                     unit_ordinal=ordinal, unit_digest=hashlib.sha256(unit.text.encode()).hexdigest()),
                (unit.text, self._discovery_reference(matter, document, unit, ordinal)),
            ))
        return passages

    @staticmethod
    def _suggestion_basis(passages, proposal) -> str:
        """Names a proposal: the exact passages read and what it would create.

        The proposal holds the extractor version and each occurrence's key,
        label and kind, so a revised extractor cannot change what Add creates.
        """
        return hashlib.sha256(json.dumps([
            [[unit["document_id"], unit["source_version_id"], unit["unit_ordinal"], unit["unit_digest"]]
             for unit, _loaded in passages], proposal], separators=(",", ":")).encode()).hexdigest()

    def preview_answer_claim_suggestions(
        self, matter: MatterRecord, actor_id: str, conversation_id: str, message_id: str,
        claim_index: int,
    ) -> dict[str, object]:
        """What suggesting from a claim's cited passages would add; nothing is written."""
        self.workspace.membership(matter.matter_id, actor_id)
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            passages = self._answer_claim_passages(matter, conversation_id, message_id, claim_index)
            found, proposal, failed = self.entity_discovery(matter).preview_passages(matter.matter_id, actor_id, passages)
        return {"items": [{"label": label, "kind": kind, "count": count} for label, kind, count in found],
                "basis": self._suggestion_basis(passages, proposal), "failed": failed,
                "passages": [f"{loaded[1]['source_name']} · {loaded[1]['location']}" for _unit, loaded in passages]}

    def suggest_from_answer_claim(
        self, matter: MatterRecord, actor_id: str, conversation_id: str, message_id: str,
        claim_index: int, basis: str, *, record=None,
    ) -> dict[str, object]:
        """Add the previewed suggestions from a verified claim's cited passages.

        Runs the deterministic extractor that discovery uses on the cited
        passages, as the reviewer, in one transaction. New identities stay
        Suggested and record that they came from an answer; occurrences that
        discovery already suggested are not added again. The basis binds the
        passages and the exact occurrences previewed: if either changed since,
        nothing is written. record(added, passages), when given, runs inside the
        same transaction, so an audit event commits with the suggestions.
        """
        self.workspace.membership(matter.matter_id, actor_id)
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            passages = self._answer_claim_passages(matter, conversation_id, message_id, claim_index)

            def expect(proposal):
                # Checked inside the write transaction, so no other writer can interleave.
                if not hmac.compare_digest(str(basis), self._suggestion_basis(passages, proposal)):
                    raise WorkspaceProblem("The cited passages or their suggestions changed since the preview. Review the suggestions again.")
            added, kinds, failed = self.entity_discovery(matter).suggest_from_passages(
                matter.matter_id, actor_id, passages, expect=expect,
                record=(lambda count, _kinds: record(count, len(passages))) if record is not None else None)
        return {"added": added, "dates": kinds.count("date"), "passages": len(passages), "failed": failed}

    def _saved_answer_references(
        self, matter: MatterRecord, citations: Sequence[Mapping[str, object]],
        *, notebook_preview: bool = False, source_navigation: bool = False,
    ) -> tuple[dict[str, object], ...]:
        from .report_materials import resolve_saved_answer_references

        values = []
        for citation in citations:
            token = self._citation_support_token(citation)
            if citation.get("support_token") not in (None, "", token):
                raise WorkspaceProblem("The saved answer has conflicting source support. Ask the question again in a new conversation.")
            values.append({**citation, "support_token": token})
        return resolve_saved_answer_references(self, matter, values,
            notebook_preview=notebook_preview, source_navigation=source_navigation)

    def _save_answer_to_notebook(
        self,
        matter: MatterRecord,
        actor_id: str,
        conversation_id: str,
        message_id: str,
        claim_index: int,
        *,
        citation_index: int | None = None,
    ) -> tuple[NotebookItemRecord, bool]:
        claim = self._answer_claim(matter, conversation_id, message_id, claim_index)
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations:
            raise WorkspaceProblem("That answer passage no longer has source support.")
        if citation_index is None:
            selected_citations = citations[:12]
            references = self._saved_answer_references(matter, selected_citations,
                notebook_preview=True)
            body = str(claim["text"])
            title = self._notebook_title(body)
            status = "suggested"
            origin = "answer"
            dedupe = f"answer:{message_id}:{claim_index}"
            item_type = "fact"
        else:
            if not 0 <= citation_index < len(citations):
                raise KeyError(str(citation_index))
            selected_citations = [citations[citation_index]]
            references = self._saved_answer_references(matter, selected_citations,
                notebook_preview=True)
            reference = references[0]
            body = str(reference["excerpt"])
            title = self._notebook_title(
                f"{reference['source_name']} · {reference['location']}"
            )
            status = "needs_review"
            origin = "citation"
            dedupe = f"citation:{message_id}:{claim_index}:{citation_index}"
            item_type = "note"
        return self.workspace.create_notebook_item(
            matter.matter_id,
            actor_id,
            item_type=item_type,
            status=status,
            title=title,
            body=body,
            origin=origin,
            source_conversation_id=conversation_id,
            source_message_id=message_id,
            dedupe_key=dedupe,
            references=references,
        )

    @staticmethod
    def source_note_basis(document: PilotDocument) -> str:
        """Fingerprint the displayed extraction without introducing stored state."""
        units = document.parsed_units() if document.state == "ready" else ()
        return hashlib.sha256(json.dumps([
            document.version_id,
            [(unit.number, unit.excerpt_digest, unit.start_ms, unit.end_ms,
              unit.line_start, unit.line_end) for unit in units],
        ], separators=(",", ":")).encode()).hexdigest()

    def save_source_note(
        self, matter: MatterRecord, actor_id: str, token: str, *,
        source_version_id: str, source_basis: str, body: str, request_key: str,
        unit_number: int = 1, start_ms: int = 0,
    ) -> tuple[NotebookItemRecord, bool]:
        """Capture a human note against a currently available canonical source unit."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_key):
            raise WorkspaceProblem("Reload the source before saving this note.")
        if not body.strip():
            raise WorkspaceProblem("Write a note before saving it.")
        store = self.source_store(matter)
        with store.mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            document = store.get_by_action_token(token)
            if document.version_id != source_version_id or document.state != "ready":
                raise WorkspaceProblem("This source changed or is unavailable. Reload it before saving your note.")
            if source_basis != self.source_note_basis(document):
                raise WorkspaceProblem("The source text changed while you were writing. Reload and check the source before saving your note.")
            units = document.parsed_units()
            if is_media_type(document.media_type):
                index = next((index for index, candidate in enumerate(units)
                    if int(candidate.start_ms if candidate.start_ms is not None else candidate.line_start or 0)
                    <= start_ms < int(candidate.end_ms if candidate.end_ms is not None else candidate.line_end or 0)), None)
                if index is None:
                    raise WorkspaceProblem("Choose a transcribed playback position before saving a source-linked note.")
            else:
                index = unit_number - 1
                if not 0 <= index < len(units):
                    raise WorkspaceProblem("Choose an available source page before saving your note.")
            candidate = self._candidate(matter, document, units[index], index + 1)
            support = self._citation(matter, candidate).support_token
            reference = self.notebook_reference_from_support(matter, support)
            # A draft identity belongs to one author. Reusing it with changed
            # content or source must not silently create or overwrite a note.
            digest = hashlib.sha256(json.dumps([actor_id, request_key]).encode()).hexdigest()
            item, created = self.workspace.create_notebook_item(
                matter.matter_id, actor_id, item_type="note", status="needs_review",
                title=self._notebook_title(body), body=body, origin="manual",
                dedupe_key=f"manual-source:{digest}", references=(reference,),
            )
            if not created:
                references = self.workspace.notebook_references(matter.matter_id, actor_id, item.item_id)
                normalized_body = self.workspace._derived_text(
                    body, label="Notebook details", maximum=20_000, required=False, multiline=True)
                if (item.origin != "manual" or item.body != normalized_body
                        or len(references) != 1 or references[0].support_token != support):
                    raise WorkspaceProblem("This draft was already saved with different text or source support. Start a new note to save these changes.")
            return item, created

    def save_support_to_notebook(
        self, matter: MatterRecord, actor_id: str, token: str
    ) -> tuple[NotebookItemRecord, bool]:
        reference = self.notebook_reference_from_support(matter, token)
        return self.workspace.create_notebook_item(
            matter.matter_id,
            actor_id,
            item_type="note",
            status="needs_review",
            title=self._notebook_title(
                f"{reference['source_name']} · {reference['location']}"
            ),
            body=str(reference["excerpt"]),
            origin="citation",
            dedupe_key=f"support:{token}",
            references=(reference,),
        )

    @staticmethod
    def _suggestion_context(text: str, start: int, end: int) -> str:
        left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start))
        right_candidates = [
            position for position in (text.find(".", end), text.find("\n", end))
            if position >= 0
        ]
        right = min(right_candidates) + 1 if right_candidates else min(len(text), end + 240)
        context = " ".join(text[left + 1 : right].split()).strip()
        if len(context) > 500:
            context = context[:499].rstrip() + "…"
        return context

    def suggest_notebook_references(
        self,
        matter: MatterRecord,
        actor_id: str,
        *,
        source_set_id: str | None = None,
        maximum: int = 75,
        candidates_override: Sequence[Candidate] | None = None,
    ) -> dict[str, int | bool]:
        self.workspace.membership(matter.matter_id, actor_id)
        document_ids = (
            self.workspace.source_set_document_ids(matter.matter_id, source_set_id)
            if source_set_id
            else None
        )
        candidates = (
            tuple(candidates_override)
            if candidates_override is not None
            else self._candidates(matter, document_ids, maximum=2_501)
        )
        created_count = 0
        matched_count = 0
        scanned_units = 0
        seen: set[str] = set()
        capped = False
        for candidate in candidates:
            if scanned_units >= 2_500 or created_count >= maximum:
                capped = True
                break
            scanned_units += 1
            text = candidate.text[:6_000]
            reference = {
                "document_id": candidate.document_id,
                "source_version_id": candidate.source_version_id,
                "source_name": candidate.source_name,
                "location": candidate.citation,
                "unit_number": candidate.page_number,
                "chunk_id": candidate.chunk_id,
                "excerpt_digest": candidate.excerpt_digest,
                "excerpt": text,
                "support_token": self._support_token(candidate),
            }
            matches: list[tuple[str, str, re.Match[str]]] = []
            matches.extend(("date", match.group(0), match) for match in _DATE_REFERENCE.finditer(text))
            matches.extend(("person", match.group(0), match) for match in _PERSON_REFERENCE.finditer(text))
            matches.extend(("place", match.group("place"), match) for match in _PLACE_REFERENCE.finditer(text))
            matches.sort(key=lambda value: value[2].start())
            for item_type, raw_title, match in matches:
                title = " ".join(raw_title.split()).strip(" ,.;:")
                key_value = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
                dedupe = f"extraction:{item_type}:{key_value}"[:200]
                if not key_value or dedupe in seen:
                    continue
                seen.add(dedupe)
                matched_count += 1
                _, created = self.workspace.create_notebook_item(
                    matter.matter_id,
                    actor_id,
                    item_type=item_type,
                    status="suggested",
                    title=self._notebook_title(title),
                    body=self._suggestion_context(text, match.start(), match.end()),
                    date_label=title if item_type == "date" else "",
                    origin="extraction",
                    dedupe_key=dedupe,
                    references=(reference,),
                )
                if created:
                    created_count += 1
                if created_count >= maximum:
                    capped = True
                    break
        return {
            "created": created_count,
            "matched": matched_count,
            "scanned_units": scanned_units,
            "capped": capped,
        }

    def run_review_analysis(
        self, matter: MatterRecord, actor_id: str
    ):
        run = self.workspace.start_analysis_run(matter.matter_id, actor_id)
        try:
            candidates = self._candidates(
                matter, maximum=MAX_ANALYSIS_UNITS + 1
            )
            result = analyze_candidates(
                candidates,
                support_token=self._support_token,
            )
            suggestions = self.suggest_notebook_references(
                matter,
                actor_id,
                maximum=250,
                candidates_override=candidates[:MAX_ANALYSIS_UNITS],
            )
            completed = self.workspace.complete_analysis_run(
                run.analysis_id,
                matter.matter_id,
                actor_id,
                source_count=result.source_count,
                unit_count=result.unit_count,
                entity_count=result.entity_count,
                capped=bool(result.capped or suggestions["capped"]),
                findings=tuple(
                    {
                        "kind": finding.kind,
                        "signature": finding.signature,
                        "title": finding.title,
                        "summary": finding.summary,
                        "references": finding.references,
                    }
                    for finding in result.findings
                ),
            )
            return completed
        except Exception:
            self.workspace.fail_analysis_run(
                run.analysis_id,
                matter.matter_id,
                "The bounded review scan did not finish. No prior review decisions were removed.",
            )
            raise

    def _report_citation_from_reference(
        self, matter: MatterRecord, reference
    ) -> dict[str, object]:
        document = self.source_store(matter).get(reference.document_id)
        return {
            "kind": "transcript" if is_media_type(document.media_type) else "source",
            "document_id": reference.document_id,
            "source_version_id": reference.source_version_id,
            "source_name": reference.source_name,
            "location": reference.location,
            "support_token": reference.support_token,
            "excerpt": reference.excerpt,
        }

    def add_notebook_item_to_report(
        self,
        matter: MatterRecord,
        actor_id: str,
        report_id: str,
        item_id: str,
        *, expected_status: str,
    ):
        item = self.workspace.notebook_item(matter.matter_id, actor_id, item_id)
        references = self.workspace.notebook_references(
            matter.matter_id, actor_id, item_id
        )
        details = "\n\n".join(
            value
            for value in (
                f"Date or time: {item.date_label}" if item.date_label else "",
                item.body,
                f"Review status when added: {item.status.replace('_', ' ')}.",
            )
            if value
        )
        return self.workspace.add_report_section(
            matter.matter_id,
            report_id,
            actor_id,
            expected_status=expected_status,
            heading=item.title,
            body=details,
            origin="notebook",
            origin_id=item.item_id,
            citations=tuple(
                self._report_citation_from_reference(matter, reference)
                for reference in references
            ),
        )

    def add_finding_to_report(
        self,
        matter: MatterRecord,
        actor_id: str,
        report_id: str,
        finding_id: str,
        *, expected_status: str,
    ):
        finding = next(
            (
                item
                for item in self.workspace.review_findings(matter.matter_id)
                if item.finding_id == finding_id
            ),
            None,
        )
        if finding is None:
            raise KeyError(finding_id)
        references = self.workspace.review_finding_references(
            matter.matter_id, finding_id
        )
        return self.workspace.add_report_section(
            matter.matter_id,
            report_id,
            actor_id,
            expected_status=expected_status,
            heading=finding.title,
            body=(
                finding.summary
                + "\n\nReview status when added: "
                + finding.status.replace("_", " ")
                + "."
            ),
            origin="finding",
            origin_id=finding.finding_id,
            citations=tuple(
                self._report_citation_from_reference(matter, reference)
                for reference in references
            ),
        )

    def add_media_clip_to_report(
        self,
        matter: MatterRecord,
        actor_id: str,
        report_id: str,
        clip_id: str,
        *, expected_status: str,
    ):
        clip = self.workspace.media_clip(matter.matter_id, clip_id)
        document = self.source_store(matter).get(clip.document_id)
        if document.version_id != clip.source_version_id or not is_media_type(
            document.media_type
        ):
            raise KeyError(clip_id)
        location = f"{format_timestamp(clip.start_ms)}–{format_timestamp(clip.end_ms)}"
        return self.workspace.add_report_section(
            matter.matter_id,
            report_id,
            actor_id,
            expected_status=expected_status,
            heading=clip.title,
            body=(
                "Selected recording segment. Play the cited timestamp and review the "
                "current transcript, speaker attribution, and surrounding media before use."
            ),
            origin="media_clip",
            origin_id=clip.clip_id,
            citations=(
                {
                    "kind": "media_clip",
                    "document_id": document.document_id,
                    "source_version_id": document.version_id,
                    "source_name": document.display_name,
                    "location": location,
                    "excerpt": "",
                    "media_clip_id": clip.clip_id,
                    "start_ms": clip.start_ms,
                    "end_ms": clip.end_ms,
                },
            ),
        )

    def export_report_work_product(
        self,
        matter: MatterRecord,
        report: ReportRecord,
        sections: Sequence[
            tuple[ReportSectionRecord, Sequence[ReportCitationRecord]]
        ],
        format_name: str,
        *,
        frozen_source_catalog: Sequence[SourceCatalogRecord] | None = None,
        exported_at: str | None = None,
    ) -> ExportArtifact:
        return self.export_report_work_products(
            matter, report, sections, (format_name,),
            frozen_source_catalog=frozen_source_catalog,
            exported_at=exported_at,
        )[0]

    def export_report_work_products(
        self,
        matter: MatterRecord,
        report: ReportRecord,
        sections: Sequence[
            tuple[ReportSectionRecord, Sequence[ReportCitationRecord]]
        ],
        format_names: Sequence[str],
        *,
        frozen_source_catalog: Sequence[SourceCatalogRecord] | None = None,
        exported_at: str | None = None,
    ) -> tuple[ExportArtifact, ...]:
        """Resolve citations once, then render formats inside the same boundary."""

        def render() -> tuple[ExportArtifact, ...]:
            return tuple(
                export_report(
                    matter, report, sections, format_name, exported_at=exported_at,
                )
                for format_name in format_names
            )

        if not any(citations for _section, citations in sections):
            return render()
        if frozen_source_catalog is not None:
            # Saved Report citations do not retain the full verification basis
            # available in investigation ledgers. Never reopen quarantined
            # processing state or claim these references have been revalidated.
            raise ExportProblem(
                "No complete bundle was created. Saved Report sources cannot be "
                "verified after closing was interrupted. Contact an administrator "
                "to restore source access before retrying the export."
            )
        store = self.source_store(matter)
        with store.mutation_guard():
            try:
                self._assert_current_report_section_citations(matter, (
                    {"citations": tuple(asdict(citation) for citation in citations if citation.kind in {"source", "transcript"})}
                    for _section, citations in sections
                ))
            except (WorkspaceProblem, KeyError, OSError, ValueError, RuntimeError) as exc:
                raise ExportProblem(
                    "A report citation no longer resolves to its saved source. "
                    "Open the Report to repair or remove unavailable source "
                    "support, then retry the export."
                ) from exc
            for _section, citations in sections:
                for citation in citations:
                    try:
                        if citation.kind in {"source", "transcript"}:
                            # Already checked in one source stream above.
                            continue
                        elif citation.kind == "media_clip":
                            clip = self.workspace.media_clip(
                                matter.matter_id, citation.media_clip_id
                            )
                            document = store.get(clip.document_id)
                            location = (
                                f"{format_timestamp(clip.start_ms)}–"
                                f"{format_timestamp(clip.end_ms)}"
                            )
                            if (
                                clip.document_id != citation.document_id
                                or clip.source_version_id
                                != citation.source_version_id
                                or clip.start_ms != citation.start_ms
                                or clip.end_ms != citation.end_ms
                                or document.version_id != citation.source_version_id
                                or document.display_name != citation.source_name
                                or location != citation.location
                            ):
                                raise KeyError(citation.citation_id)
                        else:
                            raise KeyError(citation.citation_id)
                    except KeyError as exc:
                        raise ExportProblem(
                            "A report citation no longer resolves to its saved source. "
                            "Open the Report to repair or remove unavailable source "
                            "support, then retry the export."
                        ) from exc
            return render()

    def _assert_current_research_ledger(self, matter: MatterRecord, job: ResearchJobRecord) -> None:
        """Caller holds source mutation guard; validate all copied investigation support."""
        validate_research_basis(matter, job)
        if is_full_text_synthesis(job.plan, job.result):
            try:
                self._validate_full_text_synthesis_sources(matter, job.result)
            except (WorkspaceProblem, WorkflowFailure, ValueError, KeyError, TypeError) as exc:
                raise ExportProblem("The frozen full-text synthesis sources are unavailable or changed.") from exc
            return
        raw_evidence = job.result.get("evidence")
        if not isinstance(raw_evidence, list):
            raise ExportProblem(
                "The investigation evidence ledger could not be resolved."
            )
        try:
            citations = tuple(
                self._workflow_citation(value)
                for value in raw_evidence
                if isinstance(value, Mapping)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExportProblem(
                "The investigation evidence ledger could not be resolved."
            ) from exc
        if len(citations) != len(raw_evidence) or any(
            self._current_workflow_citation(matter, citation) is None
            for citation in citations
        ):
            raise ExportProblem(
                "The investigation evidence ledger no longer resolves to its saved sources."
            )
        self._assert_current_payload_support(
            matter,
            job.result,
            failure=ExportProblem,
            message=(
                "The investigation evidence ledger no longer resolves to its "
                "saved sources."
            ),
        )

    def _resolve_full_text_report_support(self, matter, run, decision, *, citations=None):
        """Freeze full-text support for Report consumers; leave legacy runs alone."""
        from .full_text_review import resolve_text_report_citations
        if not FullTextReviewLedger(self.workspace).enabled(run.run_id):
            return None
        try:
            if (run.matter_id != matter.matter_id or decision.matter_id != matter.matter_id
                    or decision.run_id != run.run_id):
                raise WorkspaceProblem("The saved full-text decision does not belong to this Report's source run.")
            document = self.source_store(matter).get(decision.document_id)
            if document.display_name != decision.source_name:
                raise WorkspaceProblem("The frozen full-text source identity changed. Reopen the original check.")
            return resolve_text_report_citations(self, matter,
                decision.citations if citations is None else citations,
                source_basis_digests={decision.document_id: decision.source_basis_digest},
                source_versions={decision.document_id: decision.source_version_id})
        except WorkspaceProblem:
            raise
        except (WorkflowFailure, KeyError, OSError, ValueError, RuntimeError) as exc:
            message = str(exc) if isinstance(exc, WorkflowFailure) else "The frozen full-text source is unavailable or changed. Reopen the original check."
            raise WorkspaceProblem(message) from exc

    def _hydrate_full_text_export_decisions(self, matter, run, decisions):
        """Copy exact portable support without replacing compact persisted locators."""
        if not FullTextReviewLedger(self.workspace).enabled(run.run_id):
            return decisions
        from .full_text_review import resolve_text_export_citations
        from .work_product_exports import MAX_WORKFLOW_EXPORT_BYTES
        remaining, hydrated = MAX_WORKFLOW_EXPORT_BYTES, []
        with self.source_store(matter).mutation_guard():
            for decision in decisions:
                if not decision.citations:
                    hydrated.append(decision)
                    continue
                try:
                    if (run.matter_id != matter.matter_id or decision.matter_id != matter.matter_id
                            or decision.run_id != run.run_id):
                        raise WorkflowFailure('The source check crossed a matter boundary.')
                    document = self.source_store(matter).get(decision.document_id)
                    if document.display_name != decision.source_name:
                        raise WorkflowFailure('The frozen source identity changed; no portable export was created.')
                    citations = resolve_text_export_citations(self, matter, decision.citations,
                        maximum_bytes=remaining,
                        source_basis_digests={decision.document_id: decision.source_basis_digest},
                        source_versions={decision.document_id: decision.source_version_id})
                    remaining -= sum(len(value['excerpt'].encode('utf-8')) for value in citations)
                    hydrated.append(replace(decision, citations=citations))
                except (WorkflowFailure, WorkspaceProblem, KeyError, OSError, ValueError, RuntimeError) as exc:
                    raise ExportProblem('The full-text source support is unavailable, changed, or exceeds the portable export limit. No partial export was created.') from exc
        return tuple(hydrated)

    def _assert_current_report_section_citations(self, matter, sections) -> None:
        """Validate exact copied passages with one stream per cited source."""
        store = self.source_store(matter)
        grouped = {}
        for section in sections:
            for value in section["citations"]:
                grouped.setdefault(str(value.get("document_id", "")), []).append(value)
        for document_id, pending in grouped.items():
            try:
                document = store.get(document_id)
            except KeyError as exc:
                raise WorkspaceProblem("A copied decision source is unavailable. Reopen the original check.") from exc
            if document.state != "ready":
                raise WorkspaceProblem("A copied decision citation no longer resolves. Repair or rerun the original source check.")
            # The optional streaming API is supplied by the full-text slice.
            # Older source stores remain supported without importing it.
            iterator = getattr(document, "iter_parsed_units", None)
            units = None
            try:
                units = iterator() if iterator is not None else iter(document.parsed_units())
                for ordinal, unit in enumerate(units, 1):
                    # Exhaustion validates the container's trailer and version.
                    # Once matched, drain without retaining or matching more units.
                    if not pending:
                        continue
                    candidate = self._candidate(matter, document, unit, ordinal)
                    tokens = self._support_tokens(candidate)
                    pending = [value for value in pending if not (
                        document.version_id == value.get("source_version_id")
                        and value.get("support_token") in tokens
                        and value.get("source_name") == document.display_name
                        and value.get("location") == candidate.citation
                        and value.get("excerpt") == unit.text
                        and value.get("kind") == ("transcript" if is_media_type(document.media_type) else "source"))]
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                raise WorkspaceProblem(
                    "A copied decision source could not be fully read. Repair or rerun the original source check."
                ) from exc
            finally:
                close = getattr(units, "close", None)
                if close is not None:
                    close()
            if pending:
                raise WorkspaceProblem("A copied decision citation no longer resolves. Repair or rerun the original source check.")

    def export_research_work_product(
        self,
        matter: MatterRecord,
        job: ResearchJobRecord,
        format_name: str,
        *,
        frozen_source_catalog: Sequence[SourceCatalogRecord] | None = None,
    ) -> ExportArtifact:
        """Resolve a saved investigation ledger before rendering any result text."""

        if frozen_source_catalog is not None:
            if is_full_text_synthesis(job.plan, job.result):
                raise ExportProblem(
                    "The full-text synthesis cannot revalidate all frozen originals after closing was interrupted. "
                    "Restore source access before exporting this work."
                )
            self._assert_frozen_research_ledger(
                matter, job, frozen_source_catalog
            )
            return export_research(matter, job, format_name)

        store = self.source_store(matter)
        with store.mutation_guard():
            self._assert_current_research_ledger(matter, job)
            return export_research(matter, job, format_name)

    def _assert_frozen_research_ledger(
        self,
        matter: MatterRecord,
        job: ResearchJobRecord,
        source_catalog: Sequence[SourceCatalogRecord],
    ) -> None:
        """Verify a save-time-validated ledger after source storage is frozen."""

        catalog: dict[str, SourceCatalogRecord] = {}
        for source in source_catalog:
            if (
                source.matter_id != matter.matter_id
                or source.document_id in catalog
            ):
                raise ExportProblem(
                    "The frozen investigation source catalog could not be resolved."
                )
            catalog[source.document_id] = source
        raw_evidence = job.result.get("evidence")
        if not isinstance(raw_evidence, list) or any(
            not isinstance(value, Mapping) for value in raw_evidence
        ):
            raise ExportProblem(
                "The investigation evidence ledger could not be resolved."
            )
        try:
            citations = tuple(
                self._workflow_citation(value) for value in raw_evidence
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExportProblem(
                "The investigation evidence ledger could not be resolved."
            ) from exc

        validated: dict[str, WorkbenchCitation] = {}
        for citation in citations:
            source = catalog.get(citation.document_id)
            expected_kind = (
                "transcript" if source is not None and is_media_type(source.media_type)
                else "document"
            )
            if (
                source is None
                or source.source_state != "ready"
                or citation.matter_id != matter.matter_id
                or source.version_id != citation.source_version_id
                or source.display_name != citation.source_name
                or citation.evidence_kind != expected_kind
                or citation.unit_number < 1
                or re.fullmatch(r"chunk-[1-9][0-9]*", citation.chunk_id) is None
                or citation.excerpt_digest
                != hashlib.sha256(citation.excerpt.encode("utf-8")).hexdigest()
            ):
                raise ExportProblem(
                    "The investigation evidence ledger no longer resolves to its frozen sources."
                )
            candidate = Candidate(
                matter.matter_id,
                citation.document_id,
                citation.chunk_id,
                citation.source_name,
                citation.unit_number,
                citation.excerpt,
                line_start=citation.line_start,
                line_end=citation.line_end,
                source_version_id=citation.source_version_id,
                excerpt_digest=citation.excerpt_digest,
                evidence_kind=citation.evidence_kind,
            )
            expected_href = (
                f"/matters/{matter.slug}?support={citation.support_token}"
                f"{'&play=1' if expected_kind == 'transcript' else ''}#support-pane"
            )
            if (
                citation.location != candidate.citation
                or citation.support_token not in self._support_tokens(candidate)
                or citation.href != expected_href
            ):
                raise ExportProblem(
                    "The investigation evidence ledger no longer resolves to its frozen sources."
                )
            prior = validated.get(citation.support_token)
            if prior is not None and prior != citation:
                raise ExportProblem(
                    "The investigation evidence ledger contains conflicting source support."
                )
            validated[citation.support_token] = citation

        stack = [job.result]
        while stack:
            value = stack.pop()
            if isinstance(value, Mapping):
                token_value = value.get("support_token")
                if token_value is not None:
                    citation = validated.get(str(token_value))
                    if citation is None:
                        raise ExportProblem(
                            "The investigation evidence ledger contains unresolved source support."
                        )
                    expected = self._workflow_citation_payload(citation)
                    if any(
                        key in value and value[key] != expected[key]
                        for key in expected
                    ):
                        raise ExportProblem(
                            "The investigation evidence ledger contains mismatched source support."
                        )
                stack.extend(value.values())
            elif isinstance(value, (list, tuple)):
                stack.extend(value)

    def assert_frozen_final_bundle_source_state(
        self,
        matter: MatterRecord,
        lifecycle: MatterLifecycleRecord,
        source_catalog: Sequence[SourceCatalogRecord],
    ) -> None:
        """Fail closed without opening or traversing a quarantined source tree."""

        purge_id = lifecycle.purge_id or ""
        if (
            lifecycle.state != "purge_failed"
            or _MATTER_PURGE_ID.fullmatch(purge_id) is None
            or lifecycle.source_count != len(source_catalog)
        ):
            raise ExportProblem(
                "The frozen source boundary for this final bundle could not be verified."
            )
        matter_root = self.storage.matters / matter.matter_id
        source_root = matter_root / "sources"
        quarantine = self.storage.purging / purge_id
        if (
            matter_root.is_symlink()
            or source_root.is_symlink()
            or quarantine.is_symlink()
        ):
            raise ExportProblem(
                "The frozen source boundary for this final bundle is unsafe."
            )
        source_present = source_root.exists()
        quarantine_present = quarantine.exists()
        if source_present and not source_root.is_dir():
            raise ExportProblem(
                "The frozen source boundary for this final bundle is unsafe."
            )
        if quarantine_present and not quarantine.is_dir():
            raise ExportProblem(
                "The frozen source boundary for this final bundle is unsafe."
            )
        if source_present and quarantine_present:
            raise ExportProblem(
                "The frozen source boundary has conflicting copies; no bundle was created."
            )
        if lifecycle.source_count and not (source_present or quarantine_present):
            raise ExportProblem(
                "The frozen sources are unavailable; no final bundle was created."
            )

    def add_answer_to_report(
        self,
        matter: MatterRecord,
        actor_id: str,
        report_id: str,
        conversation_id: str,
        message_id: str,
        *, expected_status: str,
    ):
        self.workspace.membership(matter.matter_id, actor_id)
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            return self._add_answer_to_report(matter, actor_id, report_id,
                conversation_id, message_id, expected_status=expected_status)

    def _add_answer_to_report(
        self,
        matter: MatterRecord,
        actor_id: str,
        report_id: str,
        conversation_id: str,
        message_id: str,
        *, expected_status: str,
    ):
        conversation = self.workspace.get_conversation(
            matter.matter_id, conversation_id
        )
        messages = self.workspace.messages(matter.matter_id, conversation_id)
        answer = next(
            (
                message
                for message in messages
                if message.message_id == message_id and message.role == "assistant"
            ),
            None,
        )
        if answer is None:
            raise KeyError(message_id)
        if answer.payload.get('recorded_context_job'):
            raise WorkspaceProblem('This answer has a context-supplied receipt. Export the answer with its receipt; Report copy cannot yet preserve that notice.')
        saved_citations = []
        payload = answer.payload
        claims = payload.get("claims")
        if isinstance(claims, list):
            for claim in claims:
                if not isinstance(claim, Mapping):
                    continue
                citations = claim.get("citations")
                if not isinstance(citations, list):
                    continue
                for citation in citations:
                    saved_citations.append(citation)
        source_limitation = (payload.get("source_limitation")
            if "source_limitation" in payload else
            payload.get("limitation") if not payload.get("verification_notice") else None)
        if isinstance(source_limitation, Mapping):
            limitation_citations = source_limitation.get("citations")
            if isinstance(limitation_citations, list):
                saved_citations.extend(limitation_citations)
        references = self._saved_answer_references(matter, saved_citations)
        # Validate every supplied copy before deduplicating rendered passages.
        citations = tuple({value["support_token"]: value for value in references}.values())[:100]
        question = next(
            (
                item.content
                for item in reversed(messages[: messages.index(answer)])
                if item.role == "user"
            ),
            "",
        )
        body = answer.content
        if payload.get("kind") == "generated":
            introduction = payload.get("introduction")
            body = answer_content(body, introduction if isinstance(introduction, str) else "", payload)
            body = GENERATED_REVIEW_NOTICE + "\n\n" + body
        elif payload.get("kind") == "not-supported":
            body = rejected_answer_content(body, payload)
        return self.workspace.add_report_section(
            matter.matter_id,
            report_id,
            actor_id,
            expected_status=expected_status,
            heading=question[:200] or conversation.title,
            body=body,
            origin="answer",
            origin_id=answer.message_id,
            citations=citations,
        )

    def _index_document(self, matter: MatterRecord, document: PilotDocument) -> None:
        if not self.postgres_ready or self.postgres_connection is None:
            return
        with self._postgres_lock:
            upsert_postgres_document(
                self.postgres_connection,
                matter_id=matter.matter_id,
                matter_name=matter.display_name,
                document_id=document.document_id,
                display_name=document.display_name,
                media_type=document.media_type,
                units=document.parsed_units(),
                embedder=(
                    self.embedding
                    if self.learned_retrieval
                    else DeterministicEmbeddingAdapter()
                ),
                source_version_id=document.version_id,
                content_digest=document.digest,
                byte_size=document.size,
                storage_key=(
                    document.stored_name
                    if document.origin == "upload"
                    else f"registered:{document.source_location_id}:{document.relative_path}"
                ),
                ensure_schema=False,
            )

    def _project_media_transcript(
        self,
        matter: MatterRecord,
        document: PilotDocument,
        segments: Sequence[TranscriptSegmentRecord],
        degraded: bool,
    ) -> None:
        store = self.source_store(matter)
        current = store.get(document.document_id)
        if current.version_id != document.version_id:
            raise RuntimeError("media source version changed during projection")
        projected = store.install_media_transcript(
            document.document_id,
            transcript_units(segments),
            degraded=degraded,
        )
        self._index_document(matter, projected)

    def _summarize_media_transcript(
        self,
        document: PilotDocument,
        segments: Sequence[TranscriptSegmentRecord],
    ) -> TranscriptSummaryResult:
        windows = transcript_summary_windows(segments)
        if not windows:
            raise GenerationRejected("The transcript does not contain summary material.")
        packet = tuple(
            EvidenceItem(
                window.evidence_id,
                document.display_name,
                window.location,
                window.excerpt,
                "transcript",
                document_id=document.document_id,
            )
            for window in windows
        )
        answer = self.generator.answer(
            (
                "Create a concise orientation overview of what this machine transcript "
                "appears to say. Identify major topics, requests, decisions, disagreements, "
                "and next steps only when they are present. Do not state that an underlying "
                "event occurred, and do not infer speaker identity or relationships."
            ),
            packet,
        )
        if not answer.answerable or not answer.claims:
            raise GenerationRejected("The transcript overview did not contain supported points.")
        window_map = {window.evidence_id: window for window in windows}
        claims: list[dict[str, object]] = []
        for claim in answer.claims:
            citations = [
                {
                    "location": window_map[identifier].location,
                    "start_ms": window_map[identifier].start_ms,
                    "end_ms": window_map[identifier].end_ms,
                    "ordinal": window_map[identifier].first_ordinal,
                }
                for identifier in claim.evidence_ids
                if identifier in window_map
            ]
            if citations:
                claims.append({"text": claim.text, "citations": citations})
        if not claims:
            raise GenerationRejected("The transcript overview did not retain citations.")
        covered = sum(window.segment_count for window in windows)
        total = len(segments)
        return TranscriptSummaryResult(
            payload={
                "kind": "media-transcript-overview",
                "introduction": "Orientation from the machine transcript",
                "evidence_notice": answer.evidence_notice.replace(
                    "this answer reports", "this overview reports"
                ),
                "claims": claims,
                "coverage": {
                    "mode": "full" if covered == total else "representative",
                    "covered_segment_count": covered,
                    "total_segment_count": total,
                },
            },
            basis_digest=transcript_summary_basis(segments),
            covered_segment_count=covered,
            total_segment_count=total,
        )

    def refresh_media_projection(
        self, matter: MatterRecord, document: PilotDocument
    ) -> None:
        segments = self.workspace.transcript_segments(
            matter.matter_id, document.document_id, document.version_id
        )
        job = self.workspace.media_job(
            matter.matter_id, document.document_id, document.version_id
        )
        if not segments or job is None:
            raise KeyError(document.document_id)
        self._project_media_transcript(
            matter, document, segments, bool(job.degraded)
        )

    def index_document(self, matter: MatterRecord, document_id: str) -> None:
        store = self.source_store(matter)
        document = store.get(document_id)
        if document.state != "ready" or not self.postgres_ready:
            return
        try:
            self._index_document(matter, document)
        except Exception:
            if self.postgres_connection is not None:
                with self._postgres_lock:
                    self.postgres_connection.rollback()
            document.state = "failed"
            document.message = "Saved, but processing did not finish. Choose Try again."
            store._save()

    def retry_document(
        self, matter: MatterRecord, token: str, actor_id: str | None = None
    ) -> None:
        store = self.source_store(matter)
        with store.mutation_guard():
            return self._retry_document_locked(matter, token, actor_id, store)

    def _retry_document_locked(
        self,
        matter: MatterRecord,
        token: str,
        actor_id: str | None,
        store: PilotStore,
    ) -> None:
        document = store.get_by_action_token(token)
        media_job = self.workspace.media_job(
            matter.matter_id, document.document_id, document.version_id
        )
        if media_job is not None:
            if media_job.state != "failed":
                raise UploadProblem("This source cannot be retried right now.", 409)
            document.state = "queued"
            document.message = "Queued for transcription"
            document.processing_stage = "Queued for transcription"
            document.completed_units = 0
            document.total_units = 0
            store._save()
            self.workspace.retry_media_job(
                matter.matter_id,
                document.document_id,
                actor_id or media_job.requested_by,
            )
            if self.media is not None:
                self.media.notify()
            if self.playback is not None and document.has_video:
                self.playback.ensure(
                    matter,
                    document,
                    retry=document.playback_state == "failed",
                )
            return
        job = self.workspace.ingest_job(matter.matter_id, document.document_id)
        if job is not None:
            if job.state != "failed":
                raise UploadProblem("This source cannot be retried right now.", 409)
            document.state = "queued"
            document.message = "Queued for processing"
            document.processing_stage = "Queued"
            store._save()
            self.workspace.retry_ingest_job(matter.matter_id, document.document_id)
            if self.ingestion is not None:
                self.ingestion.notify()
            return
        document = store.retry(document.document_id)
        self.index_document(matter, document.document_id)

    def remove_document(self, matter: MatterRecord, token: str) -> None:
        store = self.source_store(matter)
        with store.mutation_guard():
            self._remove_document_locked(matter, token, store)

    def _remove_document_locked(
        self, matter: MatterRecord, token: str, store: PilotStore
    ) -> None:
        document = store.get_by_action_token(token)
        media_job = self.workspace.media_job(
            matter.matter_id, document.document_id, document.version_id
        )
        if media_job is not None and media_job.state == "running":
            raise UploadProblem("This source is being processed and cannot be removed yet.", 409)
        media_summary = self.workspace.media_summary(
            matter.matter_id, document.document_id, document.version_id
        )
        if media_summary is not None and media_summary.state == "running":
            raise UploadProblem(
                "This transcript overview is still being created. Try removing the source again shortly.",
                409,
            )
        job = self.workspace.ingest_job(matter.matter_id, document.document_id)
        if job is not None and job.state == "running":
            raise UploadProblem("This source is being processed and cannot be removed yet.", 409)
        if media_job is not None and (
            self.media is None or not self.media.cancel_external(media_job)
        ):
            raise UploadProblem(
                "Temporary transcription files could not be removed yet. Try removing this source again shortly.",
                409,
            )
        if self.playback is not None and not self.playback.cancel_document(
            matter.matter_id, document.document_id
        ):
            raise UploadProblem(
                "This video is still preparing browser playback. Try removing it again shortly.",
                409,
            )
        if job is not None:
            self.workspace.cancel_document_ingest_job(matter.matter_id, document.document_id)
        if self.postgres_connection is not None and self.postgres_ready:
            with self._postgres_lock:
                delete_postgres_document(
                    self.postgres_connection,
                    matter_id=matter.matter_id,
                    document_id=document.document_id,
                )
        if media_job is not None:
            self.workspace.remove_media_document(
                matter.matter_id, document.document_id
            )
        store.remove(document.document_id)
        self.workspace.remove_source_organization(
            matter.matter_id, document.document_id
        )

    @staticmethod
    def _retrieval_question(
        question: str, messages: Sequence[MessageRecord]
    ) -> str:
        terms = tuple(_WORD.findall(question.casefold()))
        contextual = len(terms) < 6 or bool(_FOLLOWUP_WORDS.intersection(terms))
        if not contextual:
            return question
        previous = next(
            (message.content for message in reversed(messages) if message.role == "user"), ""
        )
        return f"{previous} {question}".strip()[:MAX_SEARCH_CHARS]

    @staticmethod
    def _notebook_working_context(items: Sequence[object]) -> str:
        lines: list[str] = []
        used = 0
        for item in items:
            title = str(getattr(item, "title", "")).strip()
            body = str(getattr(item, "body", "")).strip()
            date_label = str(getattr(item, "date_label", "")).strip()
            item_type = str(getattr(item, "item_type", "note")).strip()
            status = str(getattr(item, "status", "needs_review")).strip()
            details = " | ".join(value for value in (date_label, body) if value)
            line = f"[{status} {item_type}] {title}{' — ' + details if details else ''}"
            remaining = 12_000 - used
            if remaining <= 0:
                break
            line = line[:remaining]
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    @staticmethod
    def _saved_answer_citation_payload(citation: WorkbenchCitation) -> dict[str, object]:
        """Bind the complete source text without repeating it in every claim."""
        payload = CaseIntelligenceWorkbench._workflow_citation_payload(citation)
        # Existing saved-work validation compares this full-unit digest with
        # current exact text. Repeated full excerpts could exceed the bounded
        # conversation payload; omitting them does not shorten the text basis.
        del payload["excerpt"]
        return payload

    @staticmethod
    def _answer_payload(
        answer: VerifiedAnswer,
        evidence: Mapping[str, WorkbenchCitation],
    ) -> dict[str, object]:
        # This is persisted work, so display-only staff payloads are insufficient:
        # transcript moment links intentionally survive text corrections.
        def claim_payload(claim) -> dict[str, object]:
            return {
                "text": claim.text,
                "citations": [
                    CaseIntelligenceWorkbench._saved_answer_citation_payload(evidence[identifier])
                    for identifier in claim.evidence_ids
                    if identifier in evidence
                ],
            }
        # Keep the legacy combined limitation for conversation display, while
        # retaining provenance for saved-work consumers and later compilation.
        source_limitation = answer.source_limitation if answer.verification_notice else answer.limitation
        payload: dict[str, object] = {
            "kind": (
                "generated"
                if answer.model_called and answer.answerable
                else "not-supported"
            ),
            "answerable": answer.answerable,
            **({"generator_answerable": False} if answer.generator_answerable is False else {}),
            "introduction": answer.introduction,
            "claims": [claim_payload(claim) for claim in answer.claims],
            "limitation": claim_payload(answer.limitation) if answer.limitation else None,
            "source_limitation": claim_payload(source_limitation) if source_limitation else None,
            "verification_notice": answer.verification_notice,
            "missing_information": answer.missing_information,
            "model_called": answer.model_called,
            "elapsed_ms": answer.elapsed_ms,
            "omitted_claims": answer.omitted_claims,
        }
        if answer.evidence_notice:
            payload["evidence_notice"] = answer.evidence_notice
        if not answer.answerable and evidence:
            payload["source_matches"] = [
                CaseIntelligenceWorkbench._saved_answer_citation_payload(citation)
                for citation in evidence.values()
            ]
        return payload

    @staticmethod
    def _verification_abstention() -> VerifiedAnswer:
        return VerifiedAnswer(
            False,
            "The retrieved passages require direct review.",
            (),
            None,
            REJECTED_ANSWER_NOTICE,
            (),
            True,
            0,
        )

    def ask(
        self,
        matter: MatterRecord,
        conversation: ConversationRecord,
        question: str,
    ) -> MessageRecord:
        value = " ".join((question or "").split()).strip()
        if not value or len(value) > MAX_QUESTION_CHARS:
            raise WorkspaceProblem("Question must be between 1 and 2,000 characters.")
        prior = self.workspace.messages(matter.matter_id, conversation.conversation_id)
        self.workspace.append_message(
            matter.matter_id, conversation.conversation_id, "user", value
        )
        retrieval_query = self._retrieval_question(value, prior)
        intent = classify_question(value)
        retrieval_boundary: dict[str, str] = {}
        citations = self._answer_evidence_citations(
            matter,
            self._answer_search(matter, value, retrieval_query, retrieval_boundary=retrieval_boundary),
            required_kinds=intent.required_evidence_kinds,
        )
        evidence = {
            f"S{index}": citation
            for index, citation in enumerate(citations, 1)
        }
        packet = tuple(
            EvidenceItem(
                identifier,
                citation.source_name,
                citation.location,
                citation.excerpt[:6_000],
                citation.evidence_kind,
                document_id=citation.document_id,
            )
            for identifier, citation in evidence.items()
        )
        history = tuple((message.role, message.content) for message in prior[-6:])
        try:
            answer = self.generator.answer(value, packet, history=history)
        except GenerationGroundingRejected:
            answer = self._verification_abstention()
        payload = self._answer_payload(answer, evidence)
        with self.source_store(matter).mutation_guard():
            if any(
                self._current_workflow_citation(matter, citation) is None
                for citation in citations
            ):
                raise WorkspaceProblem(
                    "A source changed while this answer was being prepared. Ask the question again to use the current source."
                )
            self._assert_current_payload_support(
                matter,
                payload,
                failure=WorkspaceProblem,
                message=(
                    "A source changed while this answer was being prepared. "
                    "Ask the question again to use the current source."
                ),
            )
            with self.workspace._lock:
                readiness, payload["source_coverage"] = self._completion_source_coverage(matter, retrieval_boundary)
                evidence_shape = modality_coverage(value, evidence, answer.used_evidence_ids)
                if evidence_shape:
                    payload["modality_coverage"] = evidence_shape
                payload["review_scope"] = _focused_answer_scope(
                    value,
                    readiness,
                    answer,
                    evidence,
                )
                return self.workspace.append_message(
                    matter.matter_id,
                    conversation.conversation_id,
                    "assistant",
                    answer.text,
                    payload,
                )

    def queue_answer(
        self, matter, conversation, question, request_key, actor_id,
        source_set_id=None, notebook_mode=None, notebook_item_ids=(), *,
        use_saved_context=False, expected_selection_revision=None,
        check_authority=lambda: None,
    ):
        from .answer_context import freeze
        from .matter_context import MatterContextService
        # Guard order is source then workspace; the callback executes inside the
        # existing atomic job transaction, only for a new request key.
        with self.source_store(matter).mutation_guard():
            service = MatterContextService(self.assertion_service(matter))
            def snapshot():
                scope_ids = (self.workspace.source_set_document_ids(matter.matter_id, source_set_id)
                             if source_set_id else None)
                result = freeze(service, matter.matter_id, actor_id, expected_selection_revision,
                                scope_ids, source_set_id)
                from .answer_context import admit
                admit(result, (), lambda ref: self._context_original(matter, ref),
                      classify_question(question).excluded_evidence_kinds)
                return result
            job, created = self.workspace.queue_answer_job(
                matter.matter_id, conversation.conversation_id if conversation else None,
                actor_id, question, request_key, source_set_id, notebook_mode, notebook_item_ids,
                use_saved_context=use_saved_context, snapshot_builder=snapshot,
                check_authority=check_authority)
        if self.answers is not None:
            self.answers.notify()
        return job, created

    def _context_original(self, matter, reference):
        # Validate the complete reference (including its recorded prefix basis),
        # then admit the full original through ordinary citation controls.
        service = self.entity_service(matter)
        if service.validate_references([reference]) != frozenset({0}):
            return None
        document = self.source_store(matter).get(reference['document_id'])
        ordinal = int(reference['chunk_id'].removeprefix('chunk-'))
        unit = document.parsed_units()[ordinal - 1]
        citation = self._citation(matter, self._candidate(matter, document, unit, ordinal))
        return self._current_workflow_citation(matter, citation)

    def _validate_answer_context(self, matter, job, snapshot, citations=()):
        from .answer_context import AnswerContextRepository, references
        if (snapshot['matter_id'] != job.matter_id or snapshot['actor_id'] != job.actor_id or
            snapshot['job_id'] != job.job_id or snapshot['conversation_id'] != job.conversation_id):
            raise WorkspaceProblem('Accepted context does not match this answer request.')
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            AnswerContextRepository(self.workspace).fence(job)
            if snapshot['source_set_id']:
                ids = self.workspace.source_set_document_ids(matter.matter_id, snapshot['source_set_id'])
                if sorted(ids) != snapshot['source_ids']:
                    raise WorkspaceProblem('The selected source set changed. Submit a new request with a reviewed scope.')
            refs = references(snapshot)
            if self.entity_service(matter).validate_references(refs) != frozenset(range(len(refs))):
                raise WorkspaceProblem('Selected original support changed or became unavailable. Repair the selection and submit again.')
            if any(self._current_workflow_citation(matter, citation) is None for citation in citations):
                raise WorkspaceProblem('An admitted original changed before dispatch or save. Submit a new request.')

    def _process_answer_job(
        self,
        job: AnswerJobRecord,
        report_stage: Callable[[str, str], None],
        cancelled: Callable[[], bool],
    ) -> AnswerResult:
        try:
            try:
                self.workspace.membership(job.matter_id, job.actor_id)
            except KeyError as exc:
                raise AnswerJobFailure(
                    "Access to this matter was removed before the answer began."
                ) from exc
            matter = self._matter_by_id(job.matter_id)
            readiness = self.workspace.matter_readiness(matter.matter_id)
            if not readiness.can_query:
                raise AnswerJobFailure(
                    "Matter preparation changed before this answer began. Wait for "
                    "active processing to finish, or resolve an attention item if no "
                    "source is searchable, then try again."
                )
            conversation = self.workspace.get_conversation(
                matter.matter_id, job.conversation_id
            )
            messages = self.workspace.messages(
                matter.matter_id, conversation.conversation_id
            )
            question_message = next(
                (
                    message
                    for message in messages
                    if message.message_id == job.question_message_id
                    and message.role == "user"
                ),
                None,
            )
            if question_message is None or question_message.content != job.question:
                raise AnswerJobFailure(
                    "The saved question could not be matched to this conversation."
                )
            prior = tuple(
                message
                for message in messages
                if message.ordinal < question_message.ordinal
            )
            from .answer_context import AnswerContextRepository, orientation, admit
            from .recorded_generation import RecordedDispatch
            context_repository = AnswerContextRepository(self.workspace)
            with self.workspace._lock:
                selected_snapshot = context_repository.snapshot(job.matter_id, job.job_id)
            if selected_snapshot is not None:
                self._validate_answer_context(matter, job, selected_snapshot)
            retrieval_query = self._retrieval_question(job.question, prior)
            notebook_mode, notebook_items = self.workspace.answer_notebook_context(
                matter.matter_id, job.job_id
            )
            notebook_context = self._notebook_working_context(notebook_items)
            if notebook_context:
                retrieval_query = (
                    retrieval_query
                    + " "
                    + " ".join(
                        " ".join(
                            " ".join(
                                (
                                    item.title,
                                    item.date_label,
                                    item.body[:300],
                                )
                            ).split()
                        )
                        for item in notebook_items
                    )
                ).strip()[:MAX_SEARCH_CHARS]
            source_set_id = self.workspace.answer_source_set_id(
                matter.matter_id, job.job_id
            )
            scoped_document_ids, retrieval_boundary = self._answer_source_boundary(matter, source_set_id)
            if scoped_document_ids is not None and not scoped_document_ids:
                raise AnswerJobFailure(
                    "The selected source set is empty. Add sources to it or use all searchable sources."
                )

            def retrieval_stage(stage_key: str) -> None:
                report_stage(stage_key, ANSWER_STAGE_MESSAGES[stage_key])

            intent = classify_question(job.question)
            citations = self._answer_evidence_citations(
                matter,
                self._answer_search(
                    matter,
                    job.question,
                    retrieval_query,
                    stage_callback=retrieval_stage,
                    document_ids=scoped_document_ids,
                    retrieval_boundary=retrieval_boundary,
                ),
                required_kinds=intent.required_evidence_kinds,
            )
            recorded_dispatch = None
            if selected_snapshot is not None:
                with self.source_store(matter).mutation_guard(), self.workspace._lock:
                    self._validate_answer_context(matter, job, selected_snapshot)
                    citations, routes, omissions = admit(selected_snapshot, citations,
                        lambda ref: self._context_original(matter, ref), intent.excluded_evidence_kinds)
                notebook_context = orientation(selected_snapshot, citations)
                recorded_dispatch = RecordedDispatch(context_repository, job,
                    lambda: self._validate_answer_context(matter, job, selected_snapshot, citations),
                    dict(evidence=[dict(self._workflow_citation_payload(c), **route)
                                   for c, route in zip(citations, routes)], omissions=omissions,
                         selected_records='all; whole required support groups',
                         retrieval='existing bounded focused retrieval; not exhaustive',
                         history='existing last-six-message bounded history; independent of saved selection'))
            if cancelled():
                raise AnswerJobFailure("Answer cancelled.")
            evidence = {
                f"S{index}": citation
                for index, citation in enumerate(citations, 1)
            }
            packet = tuple(
                EvidenceItem(
                    identifier,
                    citation.source_name,
                    citation.location,
                    citation.excerpt[:6_000],
                    citation.evidence_kind,
                    document_id=citation.document_id,
                )
                for identifier, citation in evidence.items()
            )
            history = tuple(
                (message.role, message.content) for message in prior[-6:]
            )

            def generation_stage(stage_key: str) -> None:
                persisted_stage = "generating" if stage_key == "repairing" else stage_key
                report_stage(persisted_stage, ANSWER_STAGE_MESSAGES[stage_key])

            try:
                answer = self.generator.answer(
                    job.question,
                    packet,
                    history=history,
                    working_context=notebook_context,
                    stage_callback=generation_stage,
                    **({"recorded_dispatch": recorded_dispatch} if recorded_dispatch is not None else {}),
                )
            except GenerationGroundingRejected:
                answer = self._verification_abstention()
            try:
                self.workspace.membership(job.matter_id, job.actor_id)
            except KeyError as exc:
                raise AnswerJobFailure(
                    "Access to this matter was removed before the answer completed."
                ) from exc
            payload = self._answer_payload(answer, evidence)
            if selected_snapshot is not None:
                payload['recorded_context_job'] = job.job_id
                payload['recorded_context_attempt'] = job.attempts
            evidence_shape = modality_coverage(
                job.question,
                evidence,
                answer.used_evidence_ids,
            )
            if evidence_shape:
                payload["modality_coverage"] = evidence_shape
            readiness, payload["source_coverage"] = self._completion_source_coverage(matter, retrieval_boundary)
            payload["review_scope"] = _focused_answer_scope(
                job.question,
                readiness,
                answer,
                evidence,
            )
            if source_set_id is not None:
                payload["source_scope"] = self.workspace.source_set(
                    matter.matter_id, source_set_id
                ).name
            if notebook_items:
                payload["notebook_context"] = {
                    "mode": notebook_mode,
                    "count": len(notebook_items),
                    "items": [
                        {
                            "item_id": item.notebook_item_id,
                            "item_type": item.item_type,
                            "status": item.status,
                            "title": item.title,
                        }
                        for item in notebook_items
                    ],
                }
            return AnswerResult(answer.text, payload, tuple(citations),
                retrieval_source_fingerprint=retrieval_boundary.get("source_fingerprint", ""),
                verified_answer=answer)
        except AnswerJobFailure:
            raise
        except WorkspaceProblem as exc:
            raise AnswerJobFailure(str(exc)) from exc
        except RetrievalUnavailable as exc:
            raise AnswerJobFailure(
                "Search could not run. Your sources are still saved; try again in a moment."
            ) from exc
        except GenerationUnavailable as exc:
            raise AnswerJobFailure(
                "Answering is temporarily unavailable. Search and source review still work."
            ) from exc
        except GenerationRejected as exc:
            raise AnswerJobFailure(
                "No generated answer was retained after automated checks. Try a narrower question or search the matter."
            ) from exc

    def _assert_current_payload_support(
        self,
        matter: MatterRecord,
        value: object,
        *,
        failure: type[Exception],
        message: str,
    ) -> None:
        """Resolve every nested support token while source mutation is locked."""

        stack = [value]
        seen: set[str] = set()
        try:
            while stack:
                item = stack.pop()
                if isinstance(item, Mapping):
                    token_value = item.get("support_token")
                    if token_value is not None:
                        token = str(token_value)
                        if token not in seen:
                            support = self.support(matter, token)
                            seen.add(token)
                            if item.get("source_name") not in {
                                None,
                                support.source_name,
                            } or item.get("location") not in {
                                None,
                                support.location,
                            }:
                                raise KeyError(token)
                    stack.extend(item.values())
                elif isinstance(item, (list, tuple)):
                    stack.extend(item)
        except (KeyError, TypeError, ValueError) as exc:
            raise failure(message) from exc

    def _finish_answer_job(
        self, job: AnswerJobRecord, result: AnswerResult
    ) -> MessageRecord | None:
        matter = self._matter_by_id(job.matter_id)
        with self.source_store(matter).mutation_guard():
            if result.payload.get('recorded_context_job'):
                from .answer_context import AnswerContextRepository
                with self.workspace._lock:
                    snapshot = AnswerContextRepository(self.workspace).snapshot(job.matter_id, job.job_id)
                    if snapshot is None:
                        raise AnswerJobFailure('The accepted context snapshot is unavailable.')
                    self._validate_answer_context(matter, job, snapshot, result.citations)
            if any(
                not isinstance(citation, WorkbenchCitation)
                or self._current_workflow_citation(matter, citation) is None
                for citation in result.citations
            ):
                raise AnswerJobFailure(
                    "A source changed while this answer was being prepared. Try again to use the current source."
                )
            self._assert_current_payload_support(
                matter,
                result.payload,
                failure=AnswerJobFailure,
                message=(
                    "A source changed while this answer was being prepared. "
                    "Try again to use the current source."
                ),
            )
            with self.workspace._lock:
                payload = dict(result.payload)
                if result.retrieval_source_fingerprint is not None:
                    readiness, payload["source_coverage"] = self._completion_source_coverage(matter,
                        {"source_fingerprint": result.retrieval_source_fingerprint,
                         "source_set_id": self.workspace.answer_source_set_id(matter.matter_id, job.job_id) or ""})
                    if result.verified_answer is not None:
                        payload["review_scope"] = _focused_answer_scope(job.question, readiness,
                            result.verified_answer,
                            {f"S{index}": citation for index, citation in enumerate(result.citations, 1)})
                return self.workspace.finish_answer_job(
                    job.job_id,
                    content=result.content,
                    payload=payload,
                )

    @staticmethod
    def _workflow_citation_payload(citation: WorkbenchCitation) -> dict[str, object]:
        """Persist enough locator state to validate and reopen derived work product."""

        return {
            "source_name": citation.source_name,
            "location": citation.location,
            "excerpt": citation.excerpt,
            "href": citation.href,
            "support_token": citation.support_token,
            "matter_id": citation.matter_id,
            "document_id": citation.document_id,
            "source_version_id": citation.source_version_id,
            "excerpt_digest": citation.excerpt_digest,
            "chunk_id": citation.chunk_id,
            "unit_number": citation.unit_number,
            "line_start": citation.line_start,
            "line_end": citation.line_end,
            "evidence_kind": citation.evidence_kind,
        }

    @staticmethod
    def _workflow_citation(value: Mapping[str, object]) -> WorkbenchCitation:
        return WorkbenchCitation(
            str(value["source_name"]),
            str(value["location"]),
            str(value["excerpt"]),
            str(value["href"]),
            str(value["support_token"]),
            str(value["matter_id"]),
            str(value["document_id"]),
            str(value["source_version_id"]),
            str(value["excerpt_digest"]),
            str(value["chunk_id"]),
            int(value["unit_number"]),
            int(value["line_start"]) if value.get("line_start") is not None else None,
            int(value["line_end"]) if value.get("line_end") is not None else None,
            str(value["evidence_kind"]),
        )

    def _current_workflow_citation(
        self, matter: MatterRecord, citation: WorkbenchCitation, store: PilotStore | None = None
    ) -> WorkbenchCitation | None:
        """Re-resolve a checkpoint citation against the current source version."""

        if citation.matter_id != matter.matter_id:
            return None
        try:
            document = (store if store is not None else self.source_store(matter)).get(citation.document_id)
        except KeyError:
            return None
        if document.state != "ready" or document.version_id != citation.source_version_id:
            return None
        for ordinal, unit in enumerate(document.iter_parsed_units(), 1):
            if (
                unit.number != citation.unit_number
                or unit.text != citation.excerpt
                or unit.excerpt_digest != citation.excerpt_digest
                or unit.line_start != citation.line_start
                or unit.line_end != citation.line_end
            ):
                continue
            candidate = self._candidate(matter, document, unit, ordinal)
            current = self._citation(matter, candidate, store)
            if (
                citation.support_token in self._support_tokens(candidate)
                and current.chunk_id == citation.chunk_id
                and current.evidence_kind == citation.evidence_kind
            ):
                return replace(
                    current,
                    support_token=citation.support_token,
                    href=(
                        f"/matters/{matter.slug}?support={citation.support_token}"
                        f"{'&play=1' if current.evidence_kind == 'transcript' else ''}"
                        "#support-pane"
                    ),
                )
        return None

    def _full_text_synthesis_service(self, matter: MatterRecord):
        """Compose narrow snapshot, source-verifier and shared-generation services."""
        from .full_text_synthesis import FullTextSynthesisService
        from .full_text_synthesis_repository import FullTextSynthesisRepository
        from .full_text_review import resolve_text_report_citations
        return FullTextSynthesisService(
            repository=FullTextSynthesisRepository(connection=self.workspace.connection,
                lock=self.workspace._lock, authorize=self.workspace.membership),
            resolve_originals=lambda locators, **basis: resolve_text_report_citations(
                self, matter, locators, scan_units=_FullTextSynthesisSourceScan().iter_units,
                **basis), generator=self.generator)

    def _validate_full_text_synthesis_sources(self, matter, prepared):
        """Caller holds the source guard and authorizes the actual reader/writer.

        Stream every ready frozen source, including uncited and omitted units.
        The repository owns metadata checks; the existing verifier owns bytes.
        Completed outputs keep their historical human-decision context.
        """
        from .full_text_synthesis import validate_prepared
        from .full_text_review import compact_locator, resolve_text_report_citations
        receipt = validate_prepared(prepared)
        if receipt["matter_id"] != matter.matter_id:
            raise WorkspaceProblem("The synthesis input belongs to another matter.")
        repository = self._full_text_synthesis_service(matter).repository
        with self.workspace._lock:
            repository.validate_source_scope_locked(receipt)
        scan = _FullTextSynthesisSourceScan()
        originals = {}
        for value in prepared["evidence"]:
            ordinal = re.fullmatch(r"chunk-([1-9][0-9]*)", value["chunk_id"])
            if ordinal is None:
                raise WorkspaceProblem("A saved synthesis original has no valid unit ordinal.")
            originals.setdefault(value["document_id"], []).append(
                compact_locator(value, int(ordinal[1]), value["excerpt"]))
        ready = [source for source in receipt["sources"] if source["source_state"] == "ready"]
        for start in range(0, len(ready), 100):
            batch = ready[start:start + 100]
            # Resolve saved citations while scanning each source once. Sharing
            # scan across batches also bounds uncited, omitted and unsealed units.
            locators = [locator for source in batch for locator in originals.pop(source["document_id"], ())]
            resolve_text_report_citations(self, matter, locators, scan_units=scan.iter_units,
                source_basis_digests={source["document_id"]: source["source_basis_digest"] for source in batch},
                source_versions={source["document_id"]: source["source_version_id"] for source in batch})
        if originals:
            raise WorkspaceProblem("A saved synthesis original is missing or changed.")
        # Source-set edits use the workspace lock, not the source-file guard.
        # Revalidate after the potentially lengthy actual-original scan so an
        # overlapping set edit cannot release a result for the former scope.
        with self.workspace._lock:
            repository.validate_source_scope_locked(receipt)
        return receipt

    def queue_full_text_synthesis(self, matter, actor_id, run_id, request_key, *, expected_snapshot=None):
        if not self.generator.available:
            raise WorkspaceProblem("Automatic synthesis is unavailable. Saved findings and manual source review remain available.")
        service = self._full_text_synthesis_service(matter)
        with self.source_store(matter).mutation_guard():
            prepared = service.prepare(matter.matter_id, actor_id, run_id)
            receipt = self._validate_full_text_synthesis_sources(matter, prepared)
            if expected_snapshot is not None and expected_snapshot != receipt["snapshot_digest"]:
                raise WorkspaceProblem("This review or its human decisions changed after the page opened. Reload the terminal review before synthesizing.")
            return self.workspace.queue_research_job(matter.matter_id, actor_id,
                receipt["question"], receipt["title"], request_key, receipt["source_set_id"],
                initial_plan={"full_text_synthesis_version": 1, "synthesis_version": 1,
                              "run_id": run_id, "input_digest": receipt["input_digest"]},
                initial_result=prepared,
                validate_locked=lambda: service.repository.validate_locked(matter.matter_id, actor_id, receipt))

    def _process_full_text_synthesis(self, job, cancelled):
        from .full_text_synthesis import validate_job_input, input_notice
        from .hierarchical_synthesis import synthesis_answer
        matter = self._matter_by_id(job.matter_id)
        service = self._full_text_synthesis_service(matter)
        prepared = dict(job.result)
        receipt = validate_job_input(job, prepared)

        def validate_locked():
            service.repository.validate_locked(job.matter_id, job.actor_id, receipt)

        def boundary():
            if cancelled():
                raise WorkflowFailure("Research cancelled.")
            with self.source_store(matter).mutation_guard():
                self._validate_full_text_synthesis_sources(matter, prepared)
                with self.workspace._lock, self.workspace.connection:
                    self.workspace.connection.execute("BEGIN IMMEDIATE")
                    self.workspace._validate_research_attempt_locked(job)
                    validate_locked()

        def checkpoint(state):
            with self.source_store(matter).mutation_guard():
                self._validate_full_text_synthesis_sources(matter, prepared)
                self.workspace.checkpoint_research_job(job.job_id,
                    {**prepared, "hierarchical_synthesis": state}, expected_job=job,
                    validate_locked=validate_locked)

        state = service.run(job.question, prepared, checkpoint, boundary,
                            prepared.get("hierarchical_synthesis"))
        answer = synthesis_answer(state, prepared["evidence"])
        payload = self._answer_payload(answer, {f"S{number}": self._workflow_citation(source)
            for number, source in enumerate(prepared["evidence"], 1)})
        boundary()
        return {**prepared, "hierarchical_synthesis": state,
                "summary": answer.text, "answer": payload, "stop_reason": state["stop_reason"],
                "coverage": {"scope": "full_text_run", "search_pass_count": 0,
                    "candidate_passage_count": receipt["counts"]["candidate_findings"],
                    "evidence_passage_count": len(prepared["evidence"]),
                    "evidence_source_count": len({source["document_id"] for source in prepared["evidence"]}),
                    "notice": input_notice(receipt)}}

    def _research_checkpoint_stale(self, matter: MatterRecord, job: ResearchJobRecord, store: PilotStore | None = None) -> bool:
        """Use one source and locator predicate for display and recovery admission."""
        if is_full_text_synthesis(job.plan, job.result):
            try:
                from .full_text_synthesis import validate_job_input
                validate_job_input(job)
                with self.source_store(matter).mutation_guard():
                    self._validate_full_text_synthesis_sources(matter, job.result)
                    if job.state != "succeeded":
                        service = self._full_text_synthesis_service(matter)
                        with self.workspace._lock, self.workspace.connection:
                            self.workspace.connection.execute("BEGIN")
                            service.repository.validate_locked(job.matter_id, job.actor_id,
                                job.result["full_text_synthesis_input"])
                return False
            except (KeyError, ValueError, TypeError, WorkspaceProblem, WorkflowFailure):
                return True
        if job.plan.get("planner_version") != PLANNER_VERSION:
            return False
        try:
            if job.result.get("passes") and job.result.get("retrieval_source_fingerprint") != self.workspace.source_availability_fingerprint(matter.matter_id, job.source_set_id):
                return True
            return any(self._current_workflow_citation(matter, self._workflow_citation(value), store) is None
                       for value in job.result.get("evidence", []))
        except (KeyError, TypeError, ValueError):
            return True

    def _validated_research_citations(
        self,
        matter: MatterRecord,
        citations: Sequence[WorkbenchCitation],
    ) -> list[WorkbenchCitation]:
        """Fail a bounded run rather than save evidence whose source changed."""

        current: list[WorkbenchCitation] = []
        for citation in citations:
            resolved = self._current_workflow_citation(matter, citation)
            if resolved is None:
                raise WorkflowFailure(
                    "A source changed while this investigation was working. Retry it to use the current source."
                )
            current.append(resolved)
        return current

    def _finish_research_job(
        self, job: ResearchJobRecord, result: Mapping[str, object]
    ) -> ResearchJobRecord:
        matter = self._matter_by_id(job.matter_id)
        if is_full_text_synthesis(job.plan, job.result) or is_full_text_synthesis(job.plan, result):
            service = self._full_text_synthesis_service(matter)
            validate_research_basis(matter, replace(job, result=result))
            with self.source_store(matter).mutation_guard():
                self._validate_full_text_synthesis_sources(matter, result)
                return self.workspace.finish_research_job(job.job_id, result, expected_job=job,
                    validate_locked=lambda: service.repository.validate_locked(
                        job.matter_id, job.actor_id, result["full_text_synthesis_input"]))
        if result.get("hierarchical_synthesis") is not None:
            current_job = self.workspace.research_job(job.matter_id, job.actor_id, job.job_id)
            try:
                validate_research_basis(matter, replace(current_job, result=result))
            except ExportProblem as exc:
                raise WorkflowFailure("The synthesis checkpoint could not be verified.") from exc
        with self.source_store(matter).mutation_guard():
            raw_evidence = result.get("evidence")
            if not isinstance(raw_evidence, list):
                raise WorkflowFailure(
                    "The investigation evidence ledger could not be verified. Retry the investigation."
                )
            try:
                exact_citations = tuple(
                    self._workflow_citation(value)
                    for value in raw_evidence
                    if isinstance(value, Mapping)
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise WorkflowFailure(
                    "The investigation evidence ledger could not be verified. Retry the investigation."
                ) from exc
            if len(exact_citations) != len(raw_evidence) or any(
                self._current_workflow_citation(matter, citation) is None
                for citation in exact_citations
            ):
                raise WorkflowFailure(
                    "A source changed before this investigation could be saved. Retry it to use the current source."
                )
            self._assert_current_payload_support(
                matter,
                result,
                failure=WorkflowFailure,
                message=(
                    "A source changed before this investigation could be saved. "
                    "Retry it to use the current source."
                ),
            )
            with self.workspace._lock:
                prepared = dict(result)
                fingerprint = prepared.pop("_retrieval_source_fingerprint", None)
                if isinstance(fingerprint, str):
                    _, coverage = self._completion_source_coverage(matter,
                        {"source_fingerprint": fingerprint, "source_set_id": job.source_set_id or ""})
                    coverage["notice"] = " ".join(part for part in (
                        str(coverage["notice"]), RESEARCH_COVERAGE_NOTICE,
                    ) if part)
                    prepared["coverage"] = {**dict(prepared.get("coverage", {})), **coverage}
                return self.workspace.finish_research_job(job.job_id, prepared)

    @staticmethod
    def _research_plan(question: str, title: str) -> dict[str, object]:
        """Create a bounded, explainable search plan without inventing case facts."""

        base = " ".join(question.split())
        return {
            "title": title,
            "objective": base,
            "queries": [initial_query(question)],
            "planner_version": PLANNER_VERSION,
            "synthesis_version": 1,
            "method": "Source-backed identifier, date, name, and phrase follow-ups; bounded retrieval and generated synthesis for source review.",
        }

    def _process_research_job(
        self, job: ResearchJobRecord, cancelled: Callable[[], bool]
    ) -> Mapping[str, object]:
        if is_full_text_synthesis(job.plan, job.result):
            return self._process_full_text_synthesis(job, cancelled)
        try:
            self.workspace.membership(job.matter_id, job.actor_id)
        except KeyError as exc:
            raise WorkflowFailure(
                "Access to this matter was removed before research began."
            ) from exc
        matter = self._matter_by_id(job.matter_id)
        readiness = self.workspace.matter_readiness(matter.matter_id)
        if not readiness.can_query:
            raise WorkflowFailure(
                "No source is searchable for this research run. Finish active preparation or resolve a source issue, then retry."
            )
        scoped_document_ids, retrieval_boundary = self._answer_source_boundary(matter, job.source_set_id)
        if scoped_document_ids is not None and not scoped_document_ids:
            raise WorkflowFailure("The selected source set is empty.")

        plan = dict(job.plan) if job.plan else self._research_plan(job.question, job.title)
        raw_queries = plan.get("queries")
        queries = tuple(
            str(item).strip()[:MAX_SEARCH_CHARS]
            for item in raw_queries
            if isinstance(item, str) and item.strip()
        ) if isinstance(raw_queries, list) else ()
        if not queries:
            plan = self._research_plan(job.question, job.title)
            queries = tuple(str(item) for item in plan["queries"])
        adaptive = plan.get("planner_version") == PLANNER_VERSION
        budget = ReviewBudget(**plan["budget"]["effective"]) if adaptive and "budget" in plan else DEFAULT_REVIEW_BUDGET
        if len(queries) > budget.passes:
            raise WorkflowFailure("This investigation exceeds its approved review budget. Start a new investigation.")
        plan["budget"] = budget.metadata()
        total_steps = budget.passes + 2 if adaptive else len(queries) + 2
        if not job.plan or job.total_steps != total_steps or "budget" not in job.plan:
            self.workspace.set_research_plan(job.job_id, plan, total_steps)

        checkpoint = dict(job.result) if job.result else {}
        saved_synthesis = checkpoint.get("hierarchical_synthesis")
        passes = [dict(item) for item in checkpoint.get("passes", []) if isinstance(item, dict)]
        discarded_passes = int(checkpoint.get("discarded_passes", 0)) if adaptive else 0
        elapsed_before = float(checkpoint.get("search_elapsed_seconds", 0))
        evidence_values = [
            dict(item) for item in checkpoint.get("evidence", []) if isinstance(item, dict)
        ]
        citations: list[WorkbenchCitation] = []
        seen_tokens: set[str] = set()
        checkpoint_stale = bool(passes and "budget" not in checkpoint)
        for value in evidence_values:
            try:
                checkpoint_citation = self._workflow_citation(value)
            except (KeyError, TypeError, ValueError):
                checkpoint_stale = True
                continue
            citation = self._current_workflow_citation(matter, checkpoint_citation)
            if citation is None:
                checkpoint_stale = True
                continue
            if citation.support_token in seen_tokens:
                checkpoint_stale = True
                continue
            citations.append(citation)
            seen_tokens.add(citation.support_token)
        candidate_count = int(checkpoint.get("candidate_count", 0) or 0)
        lifetime_candidates = int(checkpoint.get("lifetime_candidate_count", candidate_count))
        lifetime_documents = set(checkpoint.get("lifetime_candidate_document_ids", checkpoint.get("candidate_document_ids", [])))
        analyzed_units = int(checkpoint.get("lifetime_analyzed_units", sum(int(item.get("analyzed_units", 0)) for item in passes)))
        truncated_chars = int((checkpoint.get("budget") or {}).get("counts", {}).get("truncated_chars", 0))
        saved_fingerprint = checkpoint.get("retrieval_source_fingerprint")
        current_fingerprint = retrieval_boundary.get("source_fingerprint") or self.workspace.source_availability_fingerprint(matter.matter_id)
        if passes and saved_fingerprint != current_fingerprint:
            checkpoint_stale = True
        if passes and not checkpoint_stale:
            retrieval_boundary["source_fingerprint"] = str(saved_fingerprint)
        if checkpoint_stale:
            if not adaptive and not self.workspace.restart_research_checkpoint(job.job_id):
                raise WorkflowFailure("Research cancelled.")
            # A changed source invalidates both its citation and any saved
            # finding derived from that checkpoint. Restart the bounded search
            # plan rather than synthesizing or displaying stale source text.
            if adaptive:
                discarded_passes += len(passes)
            passes = []
            citations = []
            seen_tokens = set()
            candidate_count = 0
        if checkpoint_stale and adaptive:
            checkpoint = {}
        pending = list(checkpoint.get("pending_searches", [])) if adaptive else []
        if adaptive:
            source_text = {item.support_token: item.excerpt for item in citations}
            pending = [proposal for item in pending[:40] if (proposal := validate_proposal(item, source_text))]
            searched = {query_key(str(item.get("query", ""))) for item in passes}
            deduplicated = []
            for proposal in pending:
                key = query_key(proposal["query"])
                if key not in searched:
                    deduplicated.append(proposal)
                    searched.add(key)
            pending = deduplicated
        started_search = monotonic()
        stop_reason = checkpoint.get("search_stop_reason") or "completed_bounded_plan"
        seed_search_pending = bool(checkpoint.get("seed_search_pending", not passes)) if adaptive else False
        no_new_count = int(checkpoint.get("no_new_count", 0)) if not checkpoint_stale else 0
        candidate_documents = set(checkpoint.get("candidate_document_ids", [])) if not checkpoint_stale else set()

        def packet_for(evidence):
            nonlocal truncated_chars
            excerpts, omitted = budget.bound_excerpts(tuple(citation.excerpt for citation in evidence.values()))
            truncated_chars += omitted
            return tuple(
                EvidenceItem(identifier, citation.source_name, citation.location,
                             excerpt, citation.evidence_kind, document_id=citation.document_id)
                for (identifier, citation), excerpt in zip(evidence.items(), excerpts)
                if excerpt
            )

        def accounting(synthesis_inputs=0, stop_reason="running"):
            value = budget.metadata(completed_passes=len(passes) + discarded_passes,
                                    discarded_passes=discarded_passes, candidate_occurrences=lifetime_candidates,
                                    unique_evidence=len(citations), synthesis_inputs=synthesis_inputs,
                                    truncated_chars=truncated_chars,
                                    candidate_sources=len(lifetime_documents), analyzed_unit_occurrences=analyzed_units,
                                    unavailable_sources=max(0, readiness.total_count - readiness.searchable_count))
            value["stop_reason"] = stop_reason
            return value

        intent = classify_question(job.question)

        if checkpoint_stale and adaptive:
            # Losing stale findings must not refund the resources already spent,
            # even if recovery is interrupted before its next search completes.
            checkpoint = {
                **({"hierarchical_synthesis": saved_synthesis} if saved_synthesis else {}),
                "budget": accounting(), "passes": [], "evidence": [],
                "pending_searches": [], "discarded_passes": discarded_passes,
                "search_elapsed_seconds": elapsed_before,
                "lifetime_candidate_count": lifetime_candidates,
                "lifetime_candidate_document_ids": sorted(lifetime_documents),
                "lifetime_analyzed_units": analyzed_units,
                "seed_search_pending": True,
                "retrieval_source_fingerprint": current_fingerprint,
            }
            if not self.workspace.restart_research_checkpoint(job.job_id, checkpoint=checkpoint):
                raise WorkflowFailure("Research cancelled.")

        while (stop_reason == "completed_bounded_plan"
               and len(passes) + discarded_passes < (budget.passes if adaptive else len(queries))):
            if cancelled():
                raise WorkflowFailure("Research cancelled.")
            # Recheck membership at every tool boundary; the planner cannot expand scope.
            try:
                self.workspace.membership(job.matter_id, job.actor_id)
            except KeyError as exc:
                raise WorkflowFailure("Access to this matter was removed during research.") from exc
            if elapsed_before + monotonic() - started_search >= budget.search_seconds:
                stop_reason = "time_budget"
                break
            if adaptive and no_new_count >= 2:
                stop_reason = "no_new_evidence"
                break
            if adaptive and len(citations) >= budget.unique_evidence:
                stop_reason = "evidence_budget"
                break
            if adaptive:
                citations = self._validated_research_citations(matter, citations)
            proposal = None
            if not adaptive:
                query = queries[len(passes)]
            elif seed_search_pending:
                query = queries[0]
            elif pending:
                proposal = pending.pop(0)
                query = proposal["query"]
            else:
                stop_reason = "queue_exhausted"
                break
            index = len(passes) + 1
            retrieval_available = True
            try:
                found = self._answer_search(
                    matter,
                    job.question,
                    query,
                    document_ids=scoped_document_ids,
                    expand_broad_summary=False,
                    primary_limit=budget.primary_candidates,
                    retrieval_boundary=retrieval_boundary,
                )
            except RetrievalUnavailable:
                retrieval_available = False
                found = ()
            if adaptive and seed_search_pending and retrieval_available:
                seed_search_pending = False
            # Answer retrieval has already deduplicated primary and modality
            # results. Selection may also inspect neighboring transcript units;
            # account for those exact candidates before admission or truncation.
            candidates = list(found)

            def record_candidate(candidate: WorkbenchCitation) -> None:
                if all(item.support_token != candidate.support_token for item in candidates):
                    candidates.append(candidate)

            selectable = tuple(item for item in found if item.support_token not in seen_tokens) if adaptive else found
            selected = self._answer_evidence_citations(
                matter,
                selectable,
                maximum=budget.selected_per_pass,
                required_kinds=intent.required_evidence_kinds,
                candidate_callback=record_candidate,
            )
            found = tuple(candidates)
            candidate_count += len(found)
            lifetime_candidates += len(found)
            candidate_documents.update(item.document_id for item in found)
            lifetime_documents.update(item.document_id for item in found)
            new_selected = [item for item in selected if item.support_token not in seen_tokens]
            if adaptive:
                selected = new_selected[:max(0, budget.unique_evidence - len(citations))]
            new_selected = []
            for citation in selected:
                if citation.support_token not in seen_tokens and len(citations) < budget.unique_evidence:
                    citations.append(citation)
                    seen_tokens.add(citation.support_token)
                    new_selected.append(citation)
            evidence = {f"S{ordinal}": citation for ordinal, citation in enumerate(selected, 1)}
            packet = packet_for(evidence)
            if packet:
                try:
                    answer = self.generator.answer(
                        research_step_question(job.question, query),
                        packet,
                    )
                    payload = self._answer_payload(answer, evidence)
                    pass_shape = modality_coverage(
                        job.question,
                        evidence,
                        answer.used_evidence_ids,
                    )
                    if pass_shape:
                        payload["modality_coverage"] = pass_shape
                    pass_result = {
                        "query": query,
                        "status": "supported" if answer.answerable else "gap",
                        "text": answer.text,
                        "answer": payload,
                        "candidate_passages": len(found),
                        "candidate_sources": len({item.document_id for item in found}),
                    }
                except GenerationGroundingRejected:
                    rejected_answer = self._verification_abstention()
                    pass_result = {
                        "query": query,
                        "status": "needs_review",
                        "text": rejected_answer.text,
                        "answer": self._answer_payload(rejected_answer, evidence),
                        "candidate_passages": len(found),
                        "candidate_sources": len({item.document_id for item in found}),
                    }
            else:
                pass_result = {
                    "query": query,
                    "status": "duplicate" if found else "gap",
                    "text": "No new passage was selected from this search." if found else "No searchable passage matched this part of the research plan.",
                    "candidate_passages": len(found),
                    "candidate_sources": len({item.document_id for item in found}),
                }
                if not retrieval_available:
                    pass_result.update(
                        status="retrieval_unavailable",
                        text="Retrieval was unavailable; this search does not establish zero hits.",
                    )
            analyzed_units += len(packet)
            pass_result["hit_count"] = len(found) if retrieval_available else None
            pass_result["retrieval_outcome"] = (
                "unavailable" if not retrieval_available else
                "zero_hits" if not found else
                "no_new_evidence" if not new_selected else "new_evidence"
            )
            pass_result["selected_passages"] = len(selected)
            pass_result["analyzed_units"] = len(packet)
            pass_result["reason"] = proposal["reason"] if proposal else "Initial reviewer question."
            pass_result["motivating_support_token"] = proposal["support_token"] if proposal else ""
            pass_result["anchor"] = proposal["anchor"] if proposal else ""
            pass_result["new_evidence"] = len(new_selected)
            passes.append(pass_result)
            if adaptive:
                pending.extend(propose_searches(selected, [item["query"] for item in passes], pending))
                if retrieval_available:
                    no_new_count = no_new_count + 1 if not new_selected else 0
            checkpoint = {
                **({"hierarchical_synthesis": saved_synthesis} if saved_synthesis else {}),
                "pending_searches": pending,
                "seed_search_pending": seed_search_pending,
                "lifetime_candidate_count": lifetime_candidates,
                "lifetime_candidate_document_ids": sorted(lifetime_documents),
                "lifetime_analyzed_units": analyzed_units,
                "no_new_count": no_new_count,
                "discarded_passes": discarded_passes,
                "search_elapsed_seconds": elapsed_before + monotonic() - started_search,
                "candidate_document_ids": sorted(candidate_documents),
                "budget": accounting(),
                "passes": passes,
                "evidence": [self._workflow_citation_payload(item) for item in citations],
                "candidate_count": candidate_count,
                "retrieval_source_fingerprint": retrieval_boundary.get("source_fingerprint", ""),
            }
            self.workspace.checkpoint_research_job(job.job_id, checkpoint)
            if not self.workspace.update_research_progress(
                job.job_id,
                stage="searching",
                message=f"Completed evidence pass {index:,} of {budget.passes if adaptive else len(queries):,}; {candidate_count:,} candidate occurrences, {len(citations):,} unique selected passages.",
                completed_steps=index + discarded_passes,
                candidate_count=candidate_count,
                evidence_count=len(citations),
            ):
                raise WorkflowFailure("Research cancelled.")

            if adaptive and no_new_count >= 2:
                stop_reason = "no_new_evidence"
                break
        if adaptive and stop_reason == "completed_bounded_plan":
            stop_reason = "pass_budget" if len(passes) + discarded_passes >= budget.passes else "queue_exhausted"

        if adaptive and seed_search_pending:
            raise WorkflowFailure("Initial retrieval remains unavailable. Resume investigation, or add search budget if its passes are exhausted.")
        if adaptive:
            checkpoint = {**checkpoint, "search_stop_reason": stop_reason,
                          "budget": accounting(stop_reason=stop_reason)}
            self.workspace.checkpoint_research_job(job.job_id, checkpoint)
        if cancelled():
            raise WorkflowFailure("Research cancelled.")
        citations = self._validated_research_citations(matter, citations)
        hierarchy = None
        if plan.get("synthesis_version") == 1:
            from .hierarchical_synthesis import run_synthesis, synthesis_answer

            def synthesis_boundary():
                if cancelled():
                    raise WorkflowFailure("Research cancelled.")
                self.workspace.membership(job.matter_id, job.actor_id)
                self._validated_research_citations(matter, citations)
                if job.source_set_id:
                    current_scope = self.workspace.source_set_document_ids(matter.matter_id, job.source_set_id)
                    if any(citation.document_id not in current_scope for citation in citations):
                        raise WorkflowFailure("Cited sources left the selected set during synthesis. Rebuild the investigation.")
                # Late additions do not invalidate already verified originals.
                # The existing completion coverage receipt discloses availability
                # changes; newly added material is not silently synthesized.

            def save_synthesis(value):
                nonlocal checkpoint
                synthesis_boundary()
                checkpoint = {**checkpoint, "hierarchical_synthesis": value}
                self.workspace.checkpoint_research_job(job.job_id, checkpoint)
                self.workspace.update_research_progress(
                    job.job_id, stage="synthesizing",
                    message=f"Saved {len(value['issue'])} issue groups and {len(value['matter'])} matter sections; {value['requests_spent']} generation requests charged.",
                    completed_steps=total_steps - 1, candidate_count=candidate_count,
                    evidence_count=len(citations))

            hierarchy = run_synthesis(job.question, passes,
                [self._workflow_citation_payload(item) for item in citations],
                self.generator, save_synthesis, synthesis_boundary, saved_synthesis)
            final_citations = citations
            final_evidence = {f"S{ordinal}": citation for ordinal, citation in enumerate(citations, 1)}
            final_packet = tuple(dict.fromkeys(token for node in hierarchy["issue"]
                                               for token in node["support_tokens"]))  # Committed group inputs.
            final_answer = synthesis_answer(hierarchy, [self._workflow_citation_payload(item) for item in citations])
        else:
            final_citations = self._answer_evidence_citations(
                matter,
                citations,
                maximum=budget.synthesis_inputs,
                required_kinds=intent.required_evidence_kinds,
            )
            self.workspace.update_research_progress(
                job.job_id,
                stage="synthesizing",
                message="Synthesizing findings from the cross-source evidence ledger.",
                completed_steps=total_steps - 1,
                candidate_count=candidate_count,
                evidence_count=len(citations),
            )
            final_evidence = {
                f"S{ordinal}": citation for ordinal, citation in enumerate(final_citations, 1)
            }
            final_packet = packet_for(final_evidence)
            if adaptive:
                checkpoint = {**checkpoint, "budget": accounting(len(final_packet), stop_reason)}
                self.workspace.checkpoint_research_job(job.job_id, checkpoint)
            try:
                final_answer = self.generator.answer(
                    research_synthesis_question(job.question),
                    final_packet,
                )
            except GenerationGroundingRejected:
                final_answer = self._verification_abstention()
        if cancelled():
            raise WorkflowFailure("Research cancelled.")
        # Generation can take long enough for a source to be replaced. Resolve
        # every saved locator again immediately before the result is returned
        # to the coordinator for transactional completion.
        citations = self._validated_research_citations(matter, citations)
        final_citations = self._validated_research_citations(
            matter, final_citations
        )
        final_evidence = {
            f"S{ordinal}": citation
            for ordinal, citation in enumerate(final_citations, 1)
        }
        self.workspace.update_research_progress(
            job.job_id,
            stage="verifying",
            message="Citation references checked. Saving the evidence and coverage ledger; generated meaning still needs human review.",
            completed_steps=total_steps,
            candidate_count=candidate_count,
            evidence_count=len(citations),
        )
        gaps = [
            {"query": item.get("query", ""), "note": item.get("text", "")}
            for item in passes
            if item.get("status") != "supported"
        ]
        final_answer_payload = self._answer_payload(final_answer, final_evidence)
        evidence_shape = modality_coverage(
            job.question,
            final_evidence,
            final_answer.used_evidence_ids,
        )
        if evidence_shape:
            final_answer_payload["modality_coverage"] = evidence_shape
        # Retrieval passes use the live index, which can gain sources while
        # an investigation runs. Describe current availability and disclose
        # availability changes since the first retrieval pass separately.
        readiness, coverage = self._completion_source_coverage(matter, retrieval_boundary)
        coverage["notice"] = " ".join(part for part in (
            str(coverage["notice"]),
            RESEARCH_COVERAGE_NOTICE,
        ) if part)
        return {
            "_retrieval_source_fingerprint": retrieval_boundary.get("source_fingerprint", ""),
            "budget": accounting(len(final_packet), stop_reason),
            "stop_reason": stop_reason,
            "pending_searches": pending,
            "seed_search_pending": seed_search_pending,
            "search_stop_reason": stop_reason,
            "lifetime_candidate_count": lifetime_candidates,
            "lifetime_candidate_document_ids": sorted(lifetime_documents),
            "lifetime_analyzed_units": analyzed_units,
            "no_new_count": no_new_count,
            "discarded_passes": discarded_passes,
            "search_elapsed_seconds": checkpoint.get("search_elapsed_seconds", elapsed_before),
            "candidate_count": candidate_count,
            "candidate_document_ids": sorted(candidate_documents),
            "retrieval_source_fingerprint": retrieval_boundary.get("source_fingerprint", ""),
            **({"hierarchical_synthesis": hierarchy} if hierarchy is not None else {}),
            "summary": final_answer.text,
            "answer": final_answer_payload,
            "passes": passes,
            "evidence": [self._workflow_citation_payload(item) for item in citations],
            "gaps": gaps,
            "coverage": {
                **coverage,
                "search_pass_count": len(passes),
                "candidate_passage_count": candidate_count,
                "evidence_passage_count": len(citations),
                "evidence_source_count": len({item.document_id for item in citations}),
                "scope": "source_set" if job.source_set_id else "all_searchable_sources",
            },
        }

    def _process_review_decision(
        self,
        run: ReviewRunRecord,
        decision: ReviewDecisionRecord,
        cancelled: Callable[[], bool],
    ) -> ReviewDecisionResult:
        try:
            self.workspace.membership(run.matter_id, run.actor_id)
        except KeyError as exc:
            raise WorkflowFailure("Access to this matter was removed during the every-source check.") from exc
        if cancelled():
            raise WorkflowFailure("Review cancelled.")
        from .full_text_review import FullTextReviewLedger, process_text_source
        if FullTextReviewLedger(self.workspace).enabled(run.run_id):
            return process_text_source(self, run, decision, cancelled)
        matter = self._matter_by_id(run.matter_id)
        try:
            frozen_document = self.source_store(matter).get(decision.document_id)
        except KeyError:
            frozen_document = None
        if (
            frozen_document is None
            or frozen_document.state != "ready"
            or frozen_document.version_id != decision.source_version_id
            or not decision.source_basis_digest
            or self._document_content_basis(frozen_document)
            != decision.source_basis_digest
        ):
            return ReviewDecisionResult(
                "needs_attention",
                "This source changed after the source list was frozen, so its replacement was not reviewed.",
                (),
                "Run a new source check to evaluate the current source version.",
            )
        criterion = self.workspace.review_criterion(matter.matter_id, run.criterion_id)
        version = self.workspace.review_criterion_version(
            matter.matter_id, run.criterion_version_id
        )
        retrieval_query = " ".join(
            value for value in (
                criterion.title,
                version.instructions,
                version.include_guidance,
            ) if value
        )[:MAX_SEARCH_CHARS]
        citations = self.search(
            matter,
            retrieval_query,
            limit=12,
            document_ids=frozenset({decision.document_id}),
        )
        try:
            citations = tuple(
                self._current_workflow_citation(matter, citation)
                for citation in citations
            )
        except KeyError:
            citations = ()
        if (
            any(citation is None for citation in citations)
            or any(
                citation.document_id != decision.document_id
                or citation.source_version_id != decision.source_version_id
                for citation in citations
                if citation is not None
            )
        ):
            return ReviewDecisionResult(
                "needs_attention",
                "This source changed while it was being checked, so no decision was saved from the replacement.",
                (),
                "Run a new source check to evaluate the current source version.",
            )
        citations = tuple(citation for citation in citations if citation is not None)
        try:
            current_document = self.source_store(matter).get(decision.document_id)
        except KeyError:
            current_document = None
        if (
            current_document is None
            or current_document.state != "ready"
            or current_document.version_id != decision.source_version_id
            or not decision.source_basis_digest
            or self._document_content_basis(current_document)
            != decision.source_basis_digest
        ):
            return ReviewDecisionResult(
                "needs_attention",
                "This source changed while it was being checked, so no decision was saved from the replacement.",
                (),
                "Run a new source check to evaluate the current source version.",
            )
        if not citations:
            return ReviewDecisionResult(
                "needs_attention",
                "No matching passage was found by this search, so the source was not "
                "classified as included or excluded.",
                (),
                "Review this source directly or refine the saved criterion, then run a "
                "new source check.",
            )
        evidence = {
            f"S{ordinal}": citation for ordinal, citation in enumerate(citations[:12], 1)
        }
        packet = tuple(
            EvidenceItem(
                identifier,
                citation.source_name,
                citation.location,
                citation.excerpt[:6_000],
                citation.evidence_kind,
                document_id=citation.document_id,
            )
            for identifier, citation in evidence.items()
        )
        try:
            classification = self.generator.classify_source(
                criterion=version.instructions,
                include_guidance=version.include_guidance,
                exclude_guidance=version.exclude_guidance,
                evidence=packet,
            )
        except (GenerationGroundingRejected, GenerationRejected):
            validated = tuple(
                self._current_workflow_citation(matter, citation)
                for citation in citations
            )
            if any(citation is None for citation in validated):
                return ReviewDecisionResult(
                    "needs_attention",
                    "This source changed while it was being checked, so no decision was saved from the replacement.",
                    (),
                    "Run a new source check to evaluate the current source version.",
                )
            return ReviewDecisionResult(
                "needs_attention",
                "Potentially relevant passages were found, but no inclusion decision was retained after automated checks.",
                [
                    self._workflow_citation_payload(item)
                    for item in validated[:12]
                    if item is not None
                ],
                "Automated checks did not retain an inclusion decision. Review the source directly.",
            )
        if cancelled():
            raise WorkflowFailure("Review cancelled.")
        validated = tuple(
            self._current_workflow_citation(matter, citation)
            for citation in citations
        )
        if any(citation is None for citation in validated):
            return ReviewDecisionResult(
                "needs_attention",
                "This source changed while it was being checked, so no decision was saved from the replacement.",
                (),
                "Run a new source check to evaluate the current source version.",
            )
        citations = tuple(citation for citation in validated if citation is not None)
        evidence = {
            f"S{ordinal}": citation
            for ordinal, citation in enumerate(citations, 1)
        }
        try:
            self.workspace.membership(run.matter_id, run.actor_id)
        except KeyError as exc:
            raise WorkflowFailure(
                "Access to this matter was removed during the every-source check."
            ) from exc
        used = [
            evidence[identifier]
            for identifier in classification.used_evidence_ids
            if identifier in evidence
        ]
        if classification.decision == "include":
            return ReviewDecisionResult(
                "included",
                classification.rationale[:4_000],
                [self._workflow_citation_payload(item) for item in used],
            )
        return ReviewDecisionResult(
            "excluded",
            classification.rationale,
            [self._workflow_citation_payload(item) for item in citations[:4]],
        )

    def _record_review_decision(
        self,
        run: ReviewRunRecord,
        decision: ReviewDecisionRecord,
        result: ReviewDecisionResult,
    ) -> ReviewRunRecord:
        matter = self._matter_by_id(run.matter_id)
        with self.source_store(matter).mutation_guard():
            try:
                if FullTextReviewLedger(self.workspace).enabled(run.run_id):
                    from .full_text_review import validate_text_citations
                    validate_text_citations(self, matter, result.citations,
                        source_basis_digests={decision.document_id: decision.source_basis_digest},
                        source_versions={decision.document_id: decision.source_version_id})
                else:
                    exact_citations = tuple(
                        self._workflow_citation(citation)
                        for citation in result.citations
                    )
                    if any(
                        self._current_workflow_citation(matter, citation) is None
                        for citation in exact_citations
                    ):
                        raise WorkflowFailure(
                            "The frozen source changed before its decision could be saved."
                        )
                    self._assert_current_payload_support(
                        matter,
                        result.citations,
                        failure=WorkflowFailure,
                        message="The frozen source changed before its decision could be saved.",
                    )
            except (WorkflowFailure, WorkspaceProblem, KeyError, TypeError, ValueError):
                result = ReviewDecisionResult(
                    "needs_attention",
                    "This source changed while it was being checked, so no decision was saved from the replacement.",
                    (),
                    "Run a new source check to evaluate the current source version.",
                )
            return self.workspace.record_review_decision(
                run.run_id,
                decision.document_id,
                decision=result.decision,
                rationale=result.rationale,
                citations=result.citations,
                error_message=result.error_message,
                expected_attempt=run.attempts,
            )

    def _finish_review_run(self, run: ReviewRunRecord) -> ReviewRunRecord:
        matter = self._matter_by_id(run.matter_id)
        text_mode = FullTextReviewLedger(self.workspace).enabled(run.run_id)
        with self.source_store(matter).mutation_guard():
            after_ordinal = 0
            while True:
                decisions = self.workspace.review_decisions_for_export(
                    run.matter_id,
                    run.actor_id,
                    run.run_id,
                    limit=1_000,
                    after_ordinal=after_ordinal,
                )
                if not decisions:
                    break
                for decision in decisions:
                    source_current = False
                    try:
                        document = self.source_store(matter).get(decision.document_id)
                        source_current = bool(
                            document.state == "ready"
                            and document.version_id == decision.source_version_id
                            and bool(decision.source_basis_digest)
                            and (text_mode or self._document_content_basis(document)
                            == decision.source_basis_digest)
                        )
                    except KeyError:
                        pass
                    citations_current = source_current
                    if citations_current:
                        try:
                            if text_mode:
                                from .full_text_review import validate_text_citations
                                validate_text_citations(self, matter, decision.citations,
                                    source_basis_digests={decision.document_id: decision.source_basis_digest},
                                    source_versions={decision.document_id: decision.source_version_id})
                            else:
                                citations_current = all(
                                    self._current_workflow_citation(
                                        matter, self._workflow_citation(value)
                                    )
                                    is not None
                                    for value in decision.citations
                                )
                        except (KeyError, TypeError, ValueError, WorkflowFailure, WorkspaceProblem):
                            citations_current = False
                    if not citations_current:
                        text_ledger = FullTextReviewLedger(self.workspace)
                        if text_ledger.enabled(run.run_id):
                            text_ledger.invalidate(run, decision.document_id)
                        self.workspace.mark_review_decision_source_changed(
                            run.run_id, decision.document_id
                        )
                after_ordinal = decisions[-1].ordinal
            return self.workspace.finish_review_run(run.run_id, expected_attempt=run.attempts)

    def record_answer_error(
        self,
        matter: MatterRecord,
        conversation: ConversationRecord,
        message: str,
    ) -> MessageRecord:
        return self.workspace.append_message(
            matter.matter_id,
            conversation.conversation_id,
            "assistant",
            message,
            {"kind": "error"},
        )

    def capabilities(self) -> dict[str, str]:
        storage = self.storage_capacity_projection(include_managed_usage=False)
        malware = scanner_status(self.malware_scanner)
        return {
            "search": "word + meaning" if self.learned_retrieval else "word search only",
            "answering": "ready" if self.generator.available else "temporarily unavailable",
            "source_review": "ready",
            "ingestion": "background queue" if self.background_ingestion else "request-bound",
            "ocr": os.getenv("CASE_INTELLIGENCE_OCR_MODE", "off") or "off",
            "malware_scan": (
                "ready"
                if malware.ready
                else (
                    "not required"
                    if self.malware_scan_mode == "disabled"
                    else "administrator setup required"
                )
            ),
            "transcription": (
                "local WhisperX v2"
                if self.media is not None and self.media.available
                else "temporarily unavailable"
            ),
            "storage": "ready" if storage["ready"] else "capacity attention required",
        }


_READER_STATE_KEYS = frozenset(
    {"unit", "start_ms", "segment", "q", "speaker", "speaker_review", "flag", "page"})


def _query_url(path: str, **values: str) -> str:
    filtered = {key: value for key, value in values.items() if value}
    return path + (("?" + urlencode(filtered)) if filtered else "")


def _source_browse_href(href: str, browse: str) -> str:
    if not href:
        return href
    parsed = urlparse(href)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["browse"] = [browse]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()


def _source_find_href(href: str, find_query: str = "") -> str:
    """Keep find-in-this-file active across section navigation."""
    if not href or not find_query:
        return href
    parsed = urlparse(href)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["q"] = [find_query]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()


def _entity_context_href(href: str, origin: str = "") -> str:
    """Carry source-review origin through existing document/section links."""
    if not origin:
        return href
    parsed = urlparse(href)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["entity_return_to"] = [origin]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()


def _source_review_return_href(slug: str, origin: str = "") -> str:
    """Expose a direct return only to this matter's local review context."""
    from .review_navigation import matter_return_path
    return matter_return_path(slug, origin)


def _workspace_citation_href(
    citation: object, matter_slug: str, conversation_id: str = "", *, entity_return_to: str = ""
) -> str:
    """Normalize current and historical citations into the in-workspace evidence pane."""

    def citation_value(name: str) -> str:
        if isinstance(citation, Mapping):
            return str(citation.get(name) or "")
        return str(getattr(citation, name, "") or "")

    token = citation_value("support_token")
    raw_href = citation_value("href")
    if not re.fullmatch(r"[0-9a-f]{40}", token):
        try:
            parsed = urlparse(raw_href)
            token = (parse_qs(parsed.query).get("support") or [""])[0]
        except (TypeError, ValueError):
            token = ""
    if re.fullmatch(r"[0-9a-f]{40}", token):
        return _query_url(
            f"/matters/{matter_slug}",
            support=token,
            conversation=conversation_id,
            play="1" if citation_value("evidence_kind") == "transcript" else "",
            entity_return_to=entity_return_to,
        ) + "#support-pane"
    if raw_href.startswith(f"/matters/{matter_slug}"):
        return raw_href
    return _query_url(f"/matters/{matter_slug}", conversation=conversation_id)


class _MatterResponseSendLeaseMiddleware:
    """Release admitted downloads after the outer response send exits."""

    def __init__(self, app, *, release: Callable[[str, str], None],
                 release_clip: Callable[[str, Path], bool]):
        self.app = app
        self.release = release
        self.release_clip = release_clip

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        finally:
            try:
                state = scope.get("state", {}).get("matter_response_lease")
                if isinstance(state, dict) and state.get("send_owned"):
                    # BaseHTTPMiddleware buffers an inner Response. Its background
                    # task may run before the outer body's send completes, and may
                    # be skipped on failure. This boundary also covers disconnects.
                    self.release(state["matter"].matter_id, state["lease_id"])
            finally:
                clip = scope.get("state", {}).get("media_clip_response_lease")
                if clip is not None:
                    # Ownership begins at render admission, so construction,
                    # authorization, rendering and send failures all clean up.
                    self.release_clip(*clip)


def create_workbench_app(
    runtime_dir: Path | None = None,
    *,
    generator: GeneratorClient | None = None,
    postgres_connection=None,
    embedding=None,
    reranker=None,
    learned_retrieval: bool | None = None,
    source_registry: SourceLocationRegistry | None = None,
    background_ingestion: bool | None = None,
    ingestion_workers: int | None = None,
    answer_workers: int | None = None,
    media_processor: MediaProcessor | None = None,
    media_poll_seconds: float = 2.0,
    auth_mode: str | None = None,
    secure_cookie: bool | None = None,
    local_settings: LocalAccountSettings | None = None,
    oidc_settings: OidcSettings | None = None,
    oidc_client: OidcProviderClient | None = None,
    kerberos_settings: KerberosSettings | None = None,
    kerberos_profile_resolver: Callable[[str], tuple[str, str]] | None = None,
    kerberos_group_resolver: Callable[[str], Iterable[str]] | None = None,
    managed_storage_root: Path | None = None,
    storage_policy: StoragePolicy | None = None,
    malware_scanner: MalwareScanner | None = None,
    malware_scan_mode: str | None = None,
) -> FastAPI:
    selected_auth_mode = resolve_auth_mode(auth_mode)
    if background_ingestion is None:
        background_ingestion = os.getenv("CASE_INTELLIGENCE_BACKGROUND_INGESTION", "0") == "1"
    if ingestion_workers is None:
        try:
            ingestion_workers = int(os.getenv("CASE_INTELLIGENCE_INGESTION_WORKERS", "2"))
        except ValueError:
            ingestion_workers = 2
    if answer_workers is None:
        try:
            answer_workers = int(os.getenv("CASE_INTELLIGENCE_ANSWER_WORKERS", "2"))
        except ValueError:
            answer_workers = 2
    # Recovery coordinators start in the workbench constructor. Install the
    # live account boundary before they can inspect any restored local jobs.
    if selected_auth_mode == "local":
        local_settings = local_settings or LocalAccountSettings.from_env()
    bench = CaseIntelligenceWorkbench(
        runtime_dir or DEFAULT_RUNTIME,
        generator=generator,
        postgres_connection=postgres_connection,
        embedding=embedding,
        reranker=reranker,
        learned_retrieval=learned_retrieval,
        source_registry=source_registry,
        background_ingestion=background_ingestion,
        ingestion_workers=ingestion_workers,
        answer_workers=answer_workers,
        media_processor=media_processor,
        media_poll_seconds=media_poll_seconds,
        managed_storage_root=managed_storage_root,
        storage_policy=storage_policy,
        malware_scanner=malware_scanner,
        malware_scan_mode=malware_scan_mode,
        principal_enabled=lambda provider, subject: local_principal_enabled(local_settings, provider, subject),
    )

    recording_decision_limit = 2
    recording_decision_capacity = anyio.CapacityLimiter(recording_decision_limit)
    recording_decisions_pending: set[str] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        bench.close()

    app = FastAPI(title=PRODUCT_NAME, description=PRODUCT_DESCRIPTION, lifespan=lifespan)
    app.add_middleware(RawUploadLimitMiddleware)
    app.state.workbench = bench
    try:
        identity = IdentityService(
            bench.workspace,
            bench.runtime_dir,
            auth_mode=selected_auth_mode,
            secure_cookie=secure_cookie,
            local_settings=local_settings,
            oidc_settings=oidc_settings,
            oidc_client=oidc_client,
            kerberos_settings=kerberos_settings,
            kerberos_profile_resolver=kerberos_profile_resolver,
            kerberos_group_resolver=kerberos_group_resolver,
        )
    except Exception:
        bench.close()
        raise
    app.state.identity = identity
    from .app_context import AppContext, auth_context, present_value, requested_path

    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))
    templates.env.finalize = present_value
    templates.env.globals.update(
        answer_introduction=answer_introduction,
        research_content=research_content,
        rejected_answer_notice=rejected_answer_notice,
        review_rejection_notice=review_rejection_notice,
        answer_limitation=answer_limitation,
        modality_coverage_notice=modality_coverage_notice,
        generated_review_notice=GENERATED_REVIEW_NOTICE,
        product_name=PRODUCT_NAME,
        product_tagline=PRODUCT_TAGLINE,
        citation_href=_workspace_citation_href,
        entity_context_href=_entity_context_href,
        source_browse_href=_source_browse_href,
        source_find_href=_source_find_href,
        source_review_return_href=_source_review_return_href,
        asset_url=asset_url,
        highlight_find=highlight_find,
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")

    def audit(
        request: Request,
        action: str,
        outcome: str,
        *,
        context: AuthContext | None = None,
        matter: MatterRecord | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        current = context or getattr(request.state, "auth", None)
        bench.workspace.append_audit_event(
            actor_principal_id=(current.principal_id if isinstance(current, AuthContext) else None),
            session_id=(
                current.session.session_id
                if isinstance(current, AuthContext) and current.session is not None
                else None
            ),
            matter_id=matter.matter_id if matter is not None else None,
            request_id=getattr(request.state, "request_id", f"request-{uuid.uuid4().hex}"),
            action=action,
            outcome=outcome,
            object_type=object_type,
            object_id=object_id,
            details=details,
        )

    @app.middleware("http")
    async def identity_boundary(request: Request, call_next):
        if any(header_conflicts(request.headers, name) for name in (KERBEROS_USER_HEADER, KERBEROS_SECRET_HEADER)):
            return Response(status_code=401, headers={"Cache-Control": "no-store"})
        if request.url.path == "/internal/auth-mode":
            if (
                request.method != "GET"
                or request.client is None
                or request.client.host not in {"127.0.0.1", "::1"}
                or not identity.auth_diagnostic_authorized(header(request.headers, "X-RecordBench-Auth-Diagnostic"))
            ):
                return Response(status_code=404, headers={"Cache-Control": "no-store"})
            return JSONResponse({"auth_mode": identity.auth_mode}, headers={"Cache-Control": "no-store"})
        if identity.auth_mode in {"preview", "test"} and (
            request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}
        ):
            return PlainTextResponse("Development authentication is loopback-only.", status_code=403)
        request.state.request_id = f"request-{uuid.uuid4().hex}"
        public = (
            request.url.path == "/health"
            or request.url.path == "/favicon.ico"
            or request.url.path.startswith("/static/")
            or request.url.path.startswith("/auth/login")
            or request.url.path == "/auth/local"
            or request.url.path.startswith("/auth/oidc/")
            or request.url.path == "/auth/signed-out"
        )
        if not public:
            context = identity.resolve(request.cookies.get(SESSION_COOKIE))
            if context is not None and identity.auth_mode == "kerberos":
                bound_context = identity.bind_kerberos_request(
                    context,
                    *kerberos_headers(request.headers),
                )
                if bound_context is None:
                    audit(
                        request,
                        "auth.session_binding",
                        "denied",
                        context=context,
                        object_type="session",
                        object_id=(
                            context.session.session_id
                            if context.session is not None
                            else None
                        ),
                        details={"auth_method": "spnego"},
                    )
                    if identity.kerberos_session_identity_matches(
                        context,
                        *kerberos_headers(request.headers),
                    ):
                        identity.logout(context)
                    context = None
                else:
                    context = bound_context
            if context is None:
                wants_json = "application/json" in request.headers.get("accept", "")
                if wants_json:
                    return JSONResponse({"message": "Your session has ended. Sign in again."}, status_code=401)
                destination = _query_url("/auth/login", next=requested_path(request))
                return RedirectResponse(destination, status_code=303)
            request.state.auth = context
        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", static_cache_control(
                request.url.path, request.query_params.get("v")))
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Register outside the identity middleware's response buffer so an opted-in
    # lease outlives the final body send, including send errors/cancellation.
    app.add_middleware(_MatterResponseSendLeaseMiddleware, release=bench.finish_matter_response,
                       release_clip=bench.finish_media_clip_export)

    async def require_csrf(
        request: Request,
        csrf_token: str | None = Form(None),
        x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
    ) -> None:
        context = auth_context(request)
        submitted = x_csrf_token or csrf_token
        if not identity.csrf_valid(context, submitted):
            audit(
                request,
                "security.csrf",
                "denied",
                context=context,
                details={"auth_method": context.auth_method},
            )
            raise HTTPException(403, "This form expired. Refresh the page and try again.")

    async def require_csrf_header(
        request: Request,
        x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
    ) -> None:
        context = auth_context(request)
        if not identity.csrf_valid(context, x_csrf_token):
            audit(
                request,
                "security.csrf",
                "denied",
                context=context,
                details={"auth_method": context.auth_method},
            )
            raise HTTPException(403, "This page expired. Refresh it and try again.")

    def authorized_matter(
        request: Request,
        slug: str,
        *,
        allow_administrator_write: bool = False,
    ) -> MatterRecord:
        context = auth_context(request)
        try:
            return bench.matter(slug, context.principal_id)
        except KeyError as exc:
            if not context.is_administrator:
                audit(request, "matter.access", "denied", context=context)
                raise HTTPException(404, "Matter not found") from exc
        try:
            matter = bench.matter(
                slug,
                context.principal_id,
                administrator=True,
            )
        except KeyError as exc:
            audit(request, "matter.access", "denied", context=context)
            raise HTTPException(404, "Matter not found") from exc
        if request.method not in {"GET", "HEAD"} and not allow_administrator_write:
            audit(
                request,
                "matter.admin_write",
                "denied",
                context=context,
                matter=matter,
                details={"role": "administrator"},
            )
            raise HTTPException(
                403,
                "Administrator review is read-only until you join the case team.",
            )
        audit(
            request,
            "matter.admin_access",
            "success",
            context=context,
            matter=matter,
            details={"role": "administrator"},
        )
        request.state.administrator_matter_override = matter.matter_id
        return matter

    def _matter_response_dependency(
        request: Request,
        matter: MatterRecord,
        *,
        allowed_states: frozenset[str],
    ) -> Iterator[None]:
        try:
            lease_id = bench.begin_matter_response(
                matter, allowed_states=allowed_states
            )
        except WorkspaceProblem as exc:
            raise HTTPException(409, str(exc)) from exc
        state = {
            "matter": matter,
            "lease_id": lease_id,
            "transferred": False,
        }
        request.state.matter_response_lease = state
        try:
            yield
        finally:
            # FastAPI 0.116 finalizes yield dependencies before a streaming
            # response is sent. Successful routes explicitly transfer their
            # lease to the response background task; errors release it here.
            if not state["transferred"]:
                bench.finish_matter_response(matter.matter_id, lease_id)

    def require_matter_response_lease(
        request: Request, slug: str
    ) -> Iterator[None]:
        """Admit an active source-bearing response."""

        matter = authorized_matter(request, slug)
        yield from _matter_response_dependency(
            request, matter, allowed_states=frozenset({"active"})
        )

    def require_matter_bundle_response_lease(
        request: Request, slug: str
    ) -> Iterator[None]:
        """Retain the final bundle opportunity after a failed purge attempt."""

        context = auth_context(request)
        administrator_override = False
        try:
            matter = bench.matter(slug, context.principal_id)
        except KeyError:
            try:
                matter, lifecycle = bench.matter_for_closure(
                    slug,
                    context.principal_id,
                    administrator_override=context.is_administrator,
                )
            except KeyError as exc:
                audit(request, "matter.access", "denied", context=context)
                raise HTTPException(404, "Matter not found") from exc
            if lifecycle.state not in {"active", "purge_failed"}:
                audit(request, "matter.access", "denied", context=context)
                raise HTTPException(404, "Matter not found")
            administrator_override = bool(
                context.is_administrator
                and matter.owner_id != context.principal_id
            )
            if administrator_override:
                audit(
                    request,
                    "matter.admin_access",
                    "success",
                    context=context,
                    matter=matter,
                    details={"role": "administrator"},
                )
                request.state.administrator_matter_override = matter.matter_id
        yield from _matter_response_dependency(
            request,
            matter,
            allowed_states=frozenset({"active", "purge_failed"}),
        )

    def response_lease_matter(request: Request, slug: str) -> MatterRecord:
        state = getattr(request.state, "matter_response_lease", None)
        matter = state.get("matter") if isinstance(state, dict) else None
        if not isinstance(matter, MatterRecord) or matter.slug != slug:
            raise HTTPException(409, "The download admission could not be verified.")
        return matter

    def transfer_matter_response_lease(
        request: Request, response: Response, *, through_send: bool = False,
    ) -> Response:
        """Keep a deletion lease through the final response body byte."""

        state = getattr(request.state, "matter_response_lease", None)
        if not isinstance(state, dict) or state.get("transferred"):
            raise RuntimeError("matter response lease is unavailable")
        matter = state.get("matter")
        lease_id = state.get("lease_id")
        if not isinstance(matter, MatterRecord) or not isinstance(lease_id, str):
            raise RuntimeError("matter response lease is invalid")
        if through_send:
            state["send_owned"] = True
            state["transferred"] = True
            return response
        cleanup = BackgroundTask(
            bench.finish_matter_response, matter.matter_id, lease_id
        )
        if response.background is None:
            response.background = cleanup
        else:
            response.background = BackgroundTasks((response.background, cleanup))
        state["transferred"] = True
        return response

    def matter_readiness_projection(
        matter: MatterRecord,
        record: MatterReadinessRecord | None = None,
    ) -> dict[str, object]:
        readiness = record or bench.workspace.matter_readiness(matter.matter_id)
        recording_hold_only = (
            readiness.state == "attention" and readiness.attention_count > 0 and
            readiness.playback_only_count + readiness.recording_review_count == readiness.attention_count
        )
        if readiness.state == "empty":
            headline = "Add sources to begin"
            summary = "Upload the records you want Exculpata to prepare for review."
            guidance = "Add sources before searching or asking questions."
            action_label = "Add sources"
            action_url = f"/matters/{matter.slug}/setup"
        elif readiness.state == "preparing":
            headline = "Preparing your matter"
            summary = (
                f"{readiness.searchable_count:,} of {readiness.total_count:,} sources "
                f"searchable · {readiness.processing_count:,} still processing"
            )
            if readiness.attention_count:
                summary += f" · {readiness.attention_count:,} need attention"
            guidance = (
                "Wait until preparation is complete before asking questions "
                "across the full record."
            )
            action_label = "View processing details"
            action_url = ""
        elif recording_hold_only:
            headline = "Sources available for review"
            summary = (
                f"{readiness.searchable_count:,} of {readiness.total_count:,} sources searchable · "
                f"{readiness.playback_only_count:,} playback only · "
                f"{readiness.recording_review_count:,} awaiting a recording decision"
            )
            guidance = "You can play and review recordings. Search and answers require a transcript."
            action_label = "Review recordings" if readiness.recording_review_count else "Open recordings"
            action_url = f"/matters/{matter.slug}/setup?view=list&status=attention#source-library"
        elif readiness.state == "attention":
            if readiness.can_query:
                headline = "Review available with exclusions"
                summary = (
                    f"{readiness.searchable_count:,} of {readiness.total_count:,} sources "
                    f"searchable · {readiness.attention_count:,} excluded"
                )
                guidance = str(_source_coverage(readiness)["notice"])
            else:
                headline = "Preparation needs attention"
                summary = (
                    f"0 of {readiness.total_count:,} sources searchable · "
                    f"{readiness.attention_count:,} need attention"
                )
                guidance = (
                    "No source is searchable yet. Retry or remove an affected "
                    "source to begin search and conversation."
                )
            action_label = "Review issues"
            action_url = _query_url(
                f"/matters/{matter.slug}/setup", view="list", status="attention"
            ) + "#source-library"
        else:
            headline = "Ready for questions"
            summary = f"All {readiness.total_count:,} sources are searchable."
            guidance = "You can now ask questions across the full record."
            action_label = "View sources"
            action_url = f"/matters/{matter.slug}/setup?view=list#source-library"

        if (readiness.email_count or readiness.pdf_count) and readiness.state == "ready":
            summary = (
                "1 source has searchable text." if readiness.searchable_count == 1
                else f"{readiness.searchable_count:,} sources have searchable text."
            )
            guidance = str(_source_coverage(readiness)["notice"])

        active_work = tuple(
            item
            for item in (
                {
                    "key": "uploading",
                    "label": "Uploading",
                    "count": readiness.uploading_count,
                },
                {
                    "key": "extracting",
                    "label": "Reading & OCR",
                    "count": readiness.extracting_count,
                },
                {
                    "key": "transcribing",
                    "label": "Transcribing",
                    "count": readiness.transcribing_count,
                },
                {
                    "key": "indexing",
                    "label": "Preparing source search",
                    "count": readiness.indexing_count,
                },
            )
            if int(item["count"])
        )

        def stage(
            key: str,
            label: str,
            description: str,
            completed: int,
            *,
            working: int = 0,
        ) -> dict[str, object]:
            if readiness.total_count == 0:
                state = "waiting"
            elif completed >= readiness.total_count:
                state = "complete"
            elif working:
                state = "working"
            elif recording_hold_only:
                state = "review"
            elif readiness.attention_count:
                state = "attention"
            elif completed:
                # A downstream stage can already contain completed sources
                # while the rest of a large intake is still arriving.
                state = "working"
            else:
                state = "waiting"
            return {
                "key": key,
                "label": label,
                "description": description,
                "completed": completed,
                "total": readiness.total_count,
                "count_label": f"{completed:,} of {readiness.total_count:,}",
                "state": state,
                "state_label": {
                    "waiting": "Waiting",
                    "working": "Working",
                    "complete": "Complete",
                    "attention": "Needs attention",
                    "review": "Playback only" if not readiness.recording_review_count else "Awaiting review",
                }[state],
            }

        stages = (
            stage(
                "saved",
                "Saved",
                "Securely received by this matter.",
                readiness.saved_count,
                working=readiness.uploading_count,
            ),
            stage(
                "text",
                "Text, OCR & transcripts",
                "Reading documents and transcribing recordings.",
                readiness.extracted_count,
                working=readiness.extracting_count + readiness.transcribing_count,
            ),
            stage(
                "index",
                "Source search",
                "Building word and meaning search.",
                readiness.searchable_count,
                working=readiness.indexing_count,
            ),
            stage(
                "ready",
                "Ready for questions",
                "Available to grounded search and conversation.",
                readiness.searchable_count,
                working=readiness.processing_count,
            ),
        )
        overview_note = ""
        if readiness.overview_processing_count:
            overview_note = (
                f"{readiness.overview_processing_count:,} media overview"
                f"{'s are' if readiness.overview_processing_count != 1 else ' is'} still "
                "being prepared; searchable transcripts remain available."
            )
        elif readiness.overview_attention_count:
            overview_note = (
                f"{readiness.overview_attention_count:,} optional media overview"
                f"{'s need' if readiness.overview_attention_count != 1 else ' needs'} attention; "
                "this does not remove searchable transcripts."
            )
        coverage = _source_coverage(readiness)
        discovery = {"label": "", "href": "", "working": False}
        if readiness.total_count:
            progress = bench.automatic_discovery_progress(matter)
            attention = progress["sources_attention"] if progress else 0
            # Sources needing attention are counted independently of pending
            # work, so incomplete coverage stays visible even while work remains.
            if progress and progress["budget_reached"]:
                # Nothing more can commit until space is freed; never show this as active work.
                discovery = {
                    "label": "Finding people and dates paused · suggestion storage limit reached",
                    "href": f"/matters/{matter.slug}/entities",
                    "working": False,
                }
            elif progress and progress["sources_complete"] + attention < progress["sources_ready"]:
                discovery = {
                    "label": (
                        f"Finding people and dates · {progress['sources_complete']:,} of "
                        f"{progress['sources_ready']:,} sources"
                        + (f" · {attention:,} need{'s' if attention == 1 else ''} attention" if attention else "")
                    ),
                    "href": f"/matters/{matter.slug}/entities",
                    "working": True,
                }
            elif attention:
                discovery = {
                    "label": (
                        f"Finding people and dates needs attention · {attention:,} "
                        f"source{'' if attention == 1 else 's'}"
                    ),
                    "href": f"/matters/{matter.slug}/entities",
                    "working": False,
                }
            elif progress and progress["suggested"]:
                discovery = {
                    "label": (
                        f"{progress['suggested']:,} suggested "
                        f"{'person, thing or date' if progress['suggested'] == 1 else 'people, things and dates'}"
                        " to review"
                    ),
                    "href": f"/matters/{matter.slug}/entities",
                    "working": False,
                }
        return {
            "discovery": discovery,
            "state": "review" if recording_hold_only else readiness.state,
            "recording_review_count": readiness.recording_review_count,
            "playback_only_count": readiness.playback_only_count,
            "can_query": readiness.can_query,
            "partial_query": readiness.partial_query,
            "excluded_count": int(coverage["excluded_count"]),
            "coverage_notice": str(coverage["notice"]),
            "pdf_presentation_notice": PDF_SAVED_RESULT_NOTICE,
            "headline": headline,
            "summary": summary,
            "guidance": guidance,
            "action_label": action_label,
            "action_url": action_url,
            "total_count": readiness.total_count,
            "saved_count": readiness.saved_count,
            "extracted_count": readiness.extracted_count,
            "searchable_count": readiness.searchable_count,
            "processing_count": readiness.processing_count,
            "attention_count": readiness.attention_count,
            "progress_percent": readiness.progress_percent,
            "active_work": active_work,
            "stages": stages,
            "overview_processing_count": readiness.overview_processing_count,
            "overview_attention_count": readiness.overview_attention_count,
            "overview_note": overview_note,
            "updated_at": readiness.updated_at,
            "status_url": f"/matters/{matter.slug}/processing-status",
            "sources_url": f"/matters/{matter.slug}/setup?view=list#source-library",
            "poll_after_ms": 2_000 if readiness.state == "preparing" else 8_000,
        }

    DEFAULT_ASSISTANT_SUGGESTIONS = {
        "heading": "Suggested questions",
        "scope": "",
        "hint": "Fills the question box; nothing is sent until you choose Send. "
                "Searches all searchable sources unless you choose a scope below.",
        "questions": (
            ("Summarize the records", "Summarize the records in this matter."),
            ("Find people and organizations", "Which people and organizations are mentioned in these records?"),
            ("Review dates and events", "What dates and events should I review first?"),
        ),
    }
    SOURCE_ASSISTANT_SUGGESTIONS = {
        "heading": "Suggested for this source",
        "scope": "source",
        "hint": "Fills the question box and limits your next question to this source; "
                "nothing is sent until you choose Send.",
        "questions": (
            ("Summarize this source", "Summarize this source."),
            ("Who and what appears here", "Which people, organizations and things appear in this source?"),
            ("What dates appear here", "Which dates appear in this source, and what happened on each?"),
        ),
    }

    def assistant_suggestions(matter: MatterRecord, page: str) -> dict[str, object]:
        """Suggested questions for the page the reviewer has open.

        Suggestions only fill the question box. On an open, ready source they
        ask about that source, and choosing one first limits the next question
        to it through the existing single-source scope.
        """
        prefix = f"/matters/{matter.slug}/sources/"
        path = urlparse(page).path
        if path.startswith(prefix):
            token = path[len(prefix):].split("/", 1)[0]
            try:
                document = bench.source_store(matter).get_by_action_token(token)
            except (KeyError, OSError, RuntimeError):
                document = None
            if document is not None and document.state == "ready":
                return SOURCE_ASSISTANT_SUGGESTIONS
        return DEFAULT_ASSISTANT_SUGGESTIONS

    def assistant_context(
        request: Request,
        matter: MatterRecord,
        context: AuthContext,
        *,
        membership_present: bool,
        readiness: Mapping[str, object],
    ) -> dict[str, object] | None:
        if not membership_present:
            return None
        conversations = bench.workspace.conversations(
            matter.matter_id, include_archived=False
        )
        if not conversations:
            return None
        conversation_summaries = tuple(
            item
            for item in bench.workspace.conversation_summaries(matter.matter_id)
            if item.state == "active"
        )
        requested_conversation = request.query_params.get("conversation", "")
        conversation = next(
            (
                item
                for item in conversations
                if item.conversation_id == requested_conversation
            ),
            conversations[0],
        )
        conversation_choices = conversation_summaries[:20]
        if not any(
            item.conversation_id == conversation.conversation_id
            for item in conversation_choices
        ):
            selected_summary = next(
                item
                for item in conversation_summaries
                if item.conversation_id == conversation.conversation_id
            )
            conversation_choices = (*conversation_choices, selected_summary)
        latest_job = bench.workspace.latest_answer_job(
            matter.matter_id,
            conversation.conversation_id,
            context.principal_id,
        )
        projected_job = (
            answer_projection(request, matter, latest_job)
            if latest_job is not None and latest_job.state != "succeeded"
            else None
        )
        requested_presentation = request.query_params.get(
            "assistant_presentation", ""
        )
        if requested_presentation in {"matter", "quick"}:
            presentation = requested_presentation
        elif request.url.path.endswith(("/research", "/full-review")):
            presentation = "quick"
        else:
            presentation = "matter"
        from .matter_context_repository import MatterContextRepository
        from .review_navigation import matter_return_path
        with bench.workspace._lock:
            saved_context = MatterContextRepository(bench.workspace.assertion_repository()).selection(matter.matter_id, context.principal_id)
        # The dock refreshes from its own fragment route, so it carries the page it
        # is shown on; only a path inside this matter is accepted.
        if request.url.path == f"/matters/{matter.slug}/assistant":
            page = request.query_params.get("assistant_from", "")
        else:
            page = request.url.path + ("?" + request.url.query if request.url.query else "")
        page = matter_return_path(matter.slug, page)
        return {
            "saved_context": saved_context,
            "suggestions": assistant_suggestions(matter, page),
            "matter": matter,
            "conversation": conversation,
            "conversation_choices": conversation_choices,
            "conversation_total": len(conversation_summaries),
            "messages": bench.workspace.messages(
                matter.matter_id, conversation.conversation_id
            )[-8:],
            "active_answer": projected_job,
            "source_sets": bench.workspace.source_sets(matter.matter_id),
            "searchable_source_count": int(readiness["searchable_count"]),
            "matter_ready": bool(readiness["can_query"]),
            "readiness": readiness,
            "request_key": f"answer-request-{uuid.uuid4().hex}",
            "fragment_url": _query_url(
                f"/matters/{matter.slug}/assistant",
                conversation=conversation.conversation_id,
                assistant_presentation=presentation,
                assistant_from=page,
            ),
            "open_url": _query_url(
                f"/matters/{matter.slug}",
                conversation=conversation.conversation_id,
            )
            + "#evidence-conversation",
            "new_conversation_url": f"/matters/{matter.slug}/conversations",
            "presentation": presentation,
            "default_state": "collapsed" if presentation == "quick" else "open",
            "display_label": "Quick question" if presentation == "quick" else "Matter assistant",
        }

    def activity_center_projection(
        context: AuthContext, *, active_slug: str = ""
    ) -> dict[str, object]:
        """Project content-minimized work status across a member's matters."""

        active_states = {"queued", "running"}
        attention_states = {"failed", "needs_review"}
        items: list[dict[str, object]] = []
        active_count = 0
        attention_count = 0
        matters = bench.matters(context.principal_id, administrator=False)
        answers_by_matter: dict[str, list[AnswerJobRecord]] = {}
        research_by_matter: dict[str, list[ResearchJobRecord]] = {}
        reviews_by_matter: dict[str, list[ReviewRunRecord]] = {}
        for job in bench.workspace.answer_jobs_for_actor(
            context.principal_id, limit=150
        ):
            answers_by_matter.setdefault(job.matter_id, []).append(job)
        for job in bench.workspace.research_jobs_for_actor(
            context.principal_id, limit=150
        ):
            research_by_matter.setdefault(job.matter_id, []).append(job)
        for run in bench.workspace.review_runs_for_actor(
            context.principal_id, limit=150
        ):
            reviews_by_matter.setdefault(run.matter_id, []).append(run)

        def append_item(
            *,
            matter: MatterRecord,
            kind: str,
            activity_key: str,
            title: str,
            detail: str,
            state: str,
            href: str,
            updated_at: str,
        ) -> None:
            nonlocal active_count, attention_count
            if state in active_states:
                tone = "working"
                state_label = "Working" if state == "running" else "Queued"
                active_count += 1
            elif state in attention_states:
                tone = "attention"
                state_label = "Needs review" if state == "needs_review" else "Needs attention"
                attention_count += 1
            elif state == "cancelled":
                tone = "quiet"
                state_label = "Cancelled"
            elif state in {"ready", "succeeded"}:
                tone = "complete"
                state_label = "Complete" if state == "succeeded" else "Ready"
            else:
                tone = "quiet"
                state_label = state.replace("_", " ").title() or "Saved"
            items.append(
                {
                    "matter_name": matter.display_name,
                    "matter_slug": matter.slug,
                    "kind": kind,
                    "activity_key": activity_key,
                    "title": title,
                    "detail": detail,
                    "state": state,
                    "state_label": state_label,
                    "tone": tone,
                    "href": href,
                    "updated_at": updated_at,
                }
            )

        for matter in matters[:50]:
            readiness = matter_readiness_projection(matter)
            if matter.slug == active_slug or readiness["state"] in {
                "preparing",
                "attention",
            } or (readiness["state"] == "review" and readiness["recording_review_count"]):
                readiness_state = str(readiness["state"])
                append_item(
                    matter=matter,
                    kind="Source preparation",
                    activity_key=f"sources:{matter.matter_id}",
                    title=str(readiness["headline"]),
                    detail=str(readiness["summary"]),
                    state=(
                        "running"
                        if readiness_state == "preparing"
                        else "failed"
                        if readiness_state == "attention"
                        else "needs_review"
                        if readiness_state == "review" and readiness["recording_review_count"]
                        else "playback_only"
                        if readiness_state == "review"
                        else "ready"
                        if readiness_state == "ready"
                        else "saved"
                    ),
                    href=str(readiness["action_url"] or readiness["sources_url"]),
                    updated_at=str(readiness["updated_at"]),
                )

            recent_answers = answers_by_matter.get(matter.matter_id, [])[:3]
            for index, job in enumerate(recent_answers):
                if not (
                    job.state in active_states | attention_states
                    or (matter.slug == active_slug and index == 0)
                ):
                    continue
                append_item(
                    matter=matter,
                    kind="Focused answer",
                    activity_key=f"answer:{job.job_id}",
                    title="Focused answer",
                    detail=answer_failure_notice(job.message),
                    state=job.state,
                    href=_query_url(
                        f"/matters/{matter.slug}",
                        conversation=job.conversation_id,
                    )
                    + "#latest",
                    updated_at=job.updated_at,
                )

            for index, job in enumerate(
                research_by_matter.get(matter.matter_id, [])[:3]
            ):
                if not (
                    job.state in active_states | attention_states
                    or (matter.slug == active_slug and index == 0)
                ):
                    continue
                append_item(
                    matter=matter,
                    kind="Investigation",
                    activity_key=f"research:{job.job_id}",
                    title=job.title,
                    detail=job.message,
                    state=job.state,
                    href=_query_url(
                        f"/matters/{matter.slug}/research", job=job.job_id
                    ),
                    updated_at=job.updated_at,
                )

            for index, run in enumerate(
                reviews_by_matter.get(matter.matter_id, [])[:3]
            ):
                if not (
                    run.state in active_states | attention_states
                    or (matter.slug == active_slug and index == 0)
                ):
                    continue
                append_item(
                    matter=matter,
                    kind="Every-source check",
                    activity_key=f"review:{run.run_id}",
                    title=(
                        f"Every-source check · {run.snapshot_count:,} source"
                        f"{'s' if run.snapshot_count != 1 else ''}"
                    ),
                    detail=run.message,
                    state=run.state,
                    href=_query_url(
                        f"/matters/{matter.slug}/full-review",
                        criterion=run.criterion_id,
                        run=run.run_id,
                    ),
                    updated_at=run.updated_at,
                )

        items.sort(
            key=lambda item: (
                str(item["state"]) in attention_states,
                str(item["state"]) in active_states,
                str(item["updated_at"]),
            ),
            reverse=True,
        )
        visible = tuple(items[:16])
        if active_count:
            summary = (
                f"{active_count:,} background task"
                f"{'s are' if active_count != 1 else ' is'} working"
            )
            if attention_count:
                summary += f" · {attention_count:,} need attention"
        elif attention_count:
            summary = (
                f"{attention_count:,} item"
                f"{'s need' if attention_count != 1 else ' needs'} attention"
            )
        else:
            summary = "No background work is running"
        return {
            "items": visible,
            "active_count": active_count,
            "attention_count": attention_count,
            "badge_count": active_count + attention_count,
            "summary": summary,
            # Refresh quickly while work is moving, then back off. Opening the
            # drawer and readiness events still request an immediate refresh.
            "poll_after_ms": 3_000 if active_count else 30_000,
            "active_slug": active_slug,
        }

    def base_context(
        request: Request,
        active: MatterRecord | None = None,
        *,
        readiness: dict[str, object] | None = None,
        include_assistant: bool = True,
    ) -> dict[str, object]:
        context = auth_context(request)
        membership = None
        if active is not None:
            try:
                membership = bench.workspace.membership(
                    active.matter_id, context.principal_id
                )
            except KeyError:
                membership = None
        is_matter_owner = membership is not None and membership.role == "owner"
        administrator_view = bool(
            active is not None
            and membership is None
            and context.is_administrator
        )
        matters = bench.matters(
            context.principal_id,
            administrator=context.is_administrator,
        )
        matter_navigation = tuple(
            {"matter": item, "retention": bench.retention_projection(item)}
            for item in matters
        )
        active_retention = (
            bench.retention_projection(active) if active is not None else None
        )
        active_lifecycle = (
            bench.workspace.matter_lifecycle(active.matter_id)
            if active is not None
            else None
        )
        active_readiness = (
            readiness
            if readiness is not None
            else matter_readiness_projection(active)
            if active is not None
            and active_lifecycle is not None
            and active_lifecycle.state == "active"
            else None
        )
        assistant = (
            assistant_context(
                request,
                active,
                context,
                membership_present=membership is not None,
                readiness=active_readiness,
            )
            if include_assistant and active is not None and active_readiness is not None
            else None
        )
        preference = bench.workspace.principal_preference(context.principal_id)
        initial_activity_count = 0
        if active_readiness is not None and active_readiness["state"] in {
            "preparing",
            "attention",
            "review",
        } and (active_readiness["state"] != "review" or active_readiness["recording_review_count"]):
            initial_activity_count += 1
        if assistant and assistant["active_answer"]:
            initial_activity_count += 1
        return {
            "matters": matters,
            "matter_navigation": matter_navigation,
            "active_matter": active,
            "retention": active_retention,
            # Closing or recovering matters keep their own pages without matter navigation.
            "matter_navigation_available": (
                active_lifecycle is not None and active_lifecycle.state == "active"
            ),
            "assistant": assistant,
            "assistant_default": (
                str(assistant["default_state"]) if assistant else "open"
            ),
            "matter_readiness": active_readiness,
            "show_assistant_dock": bool(
                assistant is not None
                and active is not None
                and request.url.path != f"/matters/{active.slug}"
            ),
            "actor_name": context.display_name,
            "actor_initials": context.initials,
            "csrf_token": context.csrf_token,
            "theme": preference.theme,
            "current_path": requested_path(request),
            "auth_mode": identity.auth_mode,
            "membership": membership,
            "is_administrator": context.is_administrator,
            "administrator_view": administrator_view,
            "can_manage_members": is_matter_owner or context.is_administrator,
            "can_close_matter": is_matter_owner or context.is_administrator,
            "can_delete_conversations": is_matter_owner,
            "session_action_label": (
                "Close Exculpata" if identity.auth_mode == "kerberos" else "Sign out"
            ),
            "activity_url": _query_url(
                "/activity", matter=active.slug if active is not None else ""
            ),
            "activity_initial_count": initial_activity_count,
            "capabilities": bench.capabilities(),
        }

    def download_response(request: Request, artifact: ExportArtifact) -> Response:
        response = Response(
            content=artifact.body,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                **response_headers("Export", "work-product"),
            },
        )
        return transfer_matter_response_lease(request, response)

    def export_source_inventory(
        catalog: Sequence[SourceCatalogRecord],
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "name": source.display_name,
                "kind": source.kind,
                "state": source.state_label,
                "count_label": source.count_label,
            }
            for source in catalog
        )

    def export_media_work_product(
        matter: MatterRecord,
        catalog: Sequence[SourceCatalogRecord],
    ) -> tuple[dict[str, object], ...]:
        exports: list[dict[str, object]] = []
        prepared_bytes = 0
        for document in catalog:
            if not is_media_type(document.media_type):
                continue
            transcript = bench.workspace.media_transcript(
                matter.matter_id, document.document_id, document.version_id
            )
            if transcript is None:
                continue
            segments = bench.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            )
            clips = bench.workspace.media_clips(
                matter.matter_id, document.document_id, document.version_id
            )
            summary = bench.workspace.media_summary(
                matter.matter_id, document.document_id, document.version_id
            )
            summary_markdown = None
            summary_coverage: Mapping[str, object] | None = None
            if summary is not None and summary.state == "ready":
                try:
                    summary_markdown = export_media_summary(
                        document.display_name, summary, segments, "markdown"
                    ).body
                    coverage = summary.payload.get("coverage")
                    if isinstance(coverage, Mapping):
                        summary_coverage = dict(coverage)
                except ValueError:
                    summary_markdown = None
                    summary_coverage = None
            markdown = export_transcript(
                document.display_name, segments, "markdown", notices=transcript_processing_notices(transcript)
            ).body
            srt = export_transcript(document.display_name, segments, "srt").body
            json_body = export_transcript(
                document.display_name, segments, "json", notices=transcript_processing_notices(transcript)
            ).body
            projected_bytes = prepared_bytes + sum(
                len(body)
                for body in (markdown, srt, json_body, summary_markdown or b"")
            )
            if projected_bytes > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                raise ExportProblem(
                    "This bundle is too large to prepare at once. Export transcripts individually."
                )
            exports.append(
                {
                    "matter_id": matter.matter_id,
                    "source_name": document.display_name,
                    "source_kind": "Video" if document.has_video else "Audio",
                    "review_state": transcript.review_state,
                    "segment_count": len(segments),
                    "markdown": markdown,
                    "srt": srt,
                    "json": json_body,
                    "summary_markdown": summary_markdown,
                    "summary_coverage": summary_coverage,
                    "clips": [
                        {
                            "title": clip.title,
                            "start": format_timestamp(
                                clip.start_ms, include_millis=True
                            ).replace(",", "."),
                            "end": format_timestamp(
                                clip.end_ms, include_millis=True
                            ).replace(",", "."),
                            "created_at": clip.created_at,
                        }
                        for clip in clips
                    ],
                }
            )
            prepared_bytes = projected_bytes
        return tuple(exports)

    def notebook_export_entries(
        matter: MatterRecord,
        actor_id: str,
        *,
        item_id: str | None = None,
        include_dismissed: bool = True,
        administrator_override: bool = False,
    ) -> tuple[tuple[NotebookItemRecord, tuple[NotebookReferenceRecord, ...]], ...]:
        items = (
            (
                bench.workspace.notebook_item(
                    matter.matter_id,
                    actor_id,
                    item_id,
                    administrator_override=administrator_override,
                ),
            )
            if item_id is not None
            else bench.workspace.all_notebook_items(
                matter.matter_id,
                actor_id,
                include_dismissed=include_dismissed,
                administrator_override=administrator_override,
            )
        )
        return tuple(
            (
                item,
                bench.workspace.notebook_references(
                    matter.matter_id,
                    actor_id,
                    item.item_id,
                    administrator_override=administrator_override,
                ),
            )
            for item in items
        )

    def upload_projection(
        matter: MatterRecord,
        session,
        items: Sequence[UploadItemRecord],
    ) -> dict[str, object]:
        store = bench.source_store(matter)
        collection = bench.workspace.source_collection(
            matter.matter_id, session.collection_id
        )
        received_bytes = sum(item.received_size for item in items)
        queued = sum(item.state == "queued" for item in items)
        failed = sum(item.state in {"failed", "cancelled"} for item in items)
        projected_items: list[dict[str, object]] = []
        for item in items:
            work_state = item.state
            work_stage = item.message or "Waiting for upload"
            work_progress = (
                item.received_size / item.expected_size if item.expected_size else 0.0
            )
            review_url = ""
            media_source = is_media_type(item.media_type)
            if item.document_id:
                try:
                    document = store.get(item.document_id)
                    review_url = (
                        f"/matters/{matter.slug}/sources/{store.action_token(document)}"
                    )
                    if media_source:
                        job = bench.workspace.media_job(
                            matter.matter_id,
                            document.document_id,
                            document.version_id,
                        )
                        if job is not None:
                            work_state = (
                                "processing"
                                if job.state == "running"
                                else "queued"
                                if job.state == "queued"
                                else "ready"
                                if job.state in {"succeeded", "degraded"}
                                else "attention"
                            )
                            work_stage = job.stage
                            work_progress = job.progress
                    else:
                        job = bench.workspace.ingest_job(
                            matter.matter_id, document.document_id
                        )
                        if job is not None:
                            work_state = (
                                "processing"
                                if job.state == "running"
                                else "queued"
                                if job.state == "queued"
                                else "ready"
                                if job.state == "succeeded"
                                else "attention"
                            )
                            work_stage = job.stage
                            work_progress = (
                                job.completed_units / job.total_units
                                if job.total_units
                                else 0.0
                            )
                    if document.state == "ready":
                        work_state = "ready"
                        work_stage = document.processing_stage or "Searchable"
                        work_progress = 1.0
                    elif document.state == "playback_only":
                        work_state = "playback_only"
                        work_stage = "Available for playback"
                        work_progress = 1.0
                    elif document.state in {"failed", "needs_ocr"}:
                        work_state = "attention"
                        work_stage = document.processing_stage or "Needs attention"
                except KeyError:
                    work_state = "attention"
                    work_stage = "Source status unavailable"
            if item.state in {"failed", "cancelled"}:
                work_state = "attention"
                work_stage = item.message or "Upload needs attention"
            projected_items.append(
                {
                    "upload_item_id": item.upload_item_id,
                    "ordinal": item.ordinal,
                    "display_name": item.display_name,
                    "relative_path": item.relative_path,
                    "media_type": item.media_type,
                    "expected_size": item.expected_size,
                    "received_size": item.received_size,
                    "state": item.state,
                    "message": item.message,
                    "is_media": media_source,
                    "work_state": work_state,
                    "work_stage": work_stage,
                    "work_progress": max(0.0, min(float(work_progress), 1.0)),
                    "review_url": review_url,
                    "chunk_url": (
                        f"/matters/{matter.slug}/upload-sessions/"
                        f"{session.upload_session_id}/items/{item.upload_item_id}"
                    ),
                    "finalize_url": (
                        f"/matters/{matter.slug}/upload-sessions/"
                        f"{session.upload_session_id}/items/{item.upload_item_id}/finalize"
                    ),
                }
            )
        processing_count = sum(
            item["work_state"] in {"queued", "processing"}
            for item in projected_items
        )
        ready_count = sum(item["work_state"] == "ready" for item in projected_items)
        attention_count = sum(
            item["work_state"] == "attention" for item in projected_items
        )
        review_ready = bool(projected_items) and all(
            item["state"] in {"queued", "failed", "cancelled"}
            for item in projected_items
        )
        primary_review_url = (
            str(projected_items[0]["review_url"])
            if len(projected_items) == 1 and projected_items[0]["review_url"]
            else ""
        )
        return {
            "upload_session_id": session.upload_session_id,
            "state": session.state,
            "collection_id": session.collection_id,
            "collection_name": collection.name,
            "item_count": session.item_count,
            "total_bytes": session.total_bytes,
            "received_bytes": received_bytes,
            "queued_count": queued,
            "failed_count": failed,
            "processing_count": processing_count,
            "ready_count": ready_count,
            "attention_count": attention_count,
            "playback_only_count": sum(item["work_state"] == "playback_only" for item in projected_items),
            "work_complete": processing_count == 0 and session.state in {"complete", "partial"},
            "review_ready": review_ready,
            "contains_media": any(item["is_media"] for item in projected_items),
            "primary_review_url": primary_review_url,
            "chunk_bytes": MAX_UPLOAD_CHUNK_BYTES,
            "status_url": (
                f"/matters/{matter.slug}/upload-sessions/{session.upload_session_id}"
            ),
            "cancel_url": (
                f"/matters/{matter.slug}/upload-sessions/{session.upload_session_id}/cancel"
            ),
            "library_url": _query_url(
                f"/matters/{matter.slug}/setup",
                view="list",
                collection=session.collection_id,
            ),
            "items": projected_items,
        }

    def upload_delta_projection(
        matter: MatterRecord,
        session,
        item: UploadItemRecord,
    ) -> dict[str, object]:
        """Return one changed item plus database-native session aggregates."""

        projection = upload_projection(matter, session, (item,))
        summary = bench.workspace.upload_session_summary(
            matter.matter_id, session.actor_id, session.upload_session_id
        )
        projection.update(summary)
        projection["delta"] = True
        projection["primary_review_url"] = (
            str(projection["items"][0]["review_url"])
            if session.item_count == 1 and projection["items"][0]["review_url"]
            else ""
        )
        return projection

    def upload_compact_projection(
        matter: MatterRecord, session
    ) -> dict[str, object]:
        """Return bounded polling state regardless of upload-session size."""

        projection = upload_projection(matter, session, ())
        projection.update(
            bench.workspace.upload_session_summary(
                matter.matter_id, session.actor_id, session.upload_session_id
            )
        )
        projection["delta"] = True
        return projection

    def media_activity_projection(matter: MatterRecord) -> dict[str, object]:
        jobs = bench.workspace.media_jobs_for_matter(matter.matter_id)
        summaries = bench.workspace.media_summaries_for_matter(matter.matter_id)
        active_jobs = [job for job in jobs if job.state in {"queued", "running"}]
        attention_jobs = [job for job in jobs if job.display_state in {"failed", "needs_review"}]
        ready_jobs = [job for job in jobs if job.state in {"succeeded", "degraded"}]
        active_summaries = [
            summary
            for summary in summaries
            if summary.state in {"queued", "running", "stale"}
        ]
        attention_summaries = [
            summary for summary in summaries if summary.state == "failed"
        ]
        active_source_ids = {
            item.document_id for item in (*active_jobs, *active_summaries)
        }
        attention_source_ids = {
            item.document_id for item in (*attention_jobs, *attention_summaries)
        }
        projected: list[dict[str, object]] = []
        projected_ids: set[str] = set()

        for job in (*active_jobs, *attention_jobs):
            try:
                row = bench.workspace.source_catalog_record(
                    matter.matter_id, job.document_id
                )
                if row.version_id != job.source_version_id:
                    continue
            except KeyError:
                continue
            projected.append(
                {
                    "document_id": job.document_id,
                    "state": job.display_state,
                    "stage": job.stage,
                    "progress": job.progress,
                    "message": job.message,
                    "source_name": row.display_name,
                    "review_url": f"/matters/{matter.slug}/sources/{row.action_token}",
                    "updated_at": job.updated_at,
                }
            )
            projected_ids.add(job.document_id)
            if len(projected) >= 25:
                break

        for summary in (*active_summaries, *attention_summaries):
            if len(projected) >= 25 or summary.document_id in projected_ids:
                continue
            try:
                row = bench.workspace.source_catalog_record(
                    matter.matter_id, summary.document_id
                )
                if row.version_id != summary.source_version_id:
                    continue
            except KeyError:
                continue
            if summary.state == "failed":
                stage = "Transcript overview needs attention"
                message = summary.message
            elif summary.state == "stale":
                stage = "Refreshing transcript overview"
                message = "The reviewed transcript changed; a current overview is queued."
            else:
                stage = "Creating transcript overview"
                message = "The transcript is searchable while its orientation overview is created."
            projected.append(
                {
                    "document_id": summary.document_id,
                    "state": summary.state,
                    "stage": stage,
                    "progress": 0.99 if summary.state == "running" else 0.98,
                    "message": message,
                    "source_name": row.display_name,
                    "review_url": f"/matters/{matter.slug}/sources/{row.action_token}",
                    "updated_at": summary.updated_at,
                }
            )
            projected_ids.add(summary.document_id)

        if not projected and ready_jobs:
            job = max(ready_jobs, key=lambda item: item.updated_at)
            try:
                row = bench.workspace.source_catalog_record(
                    matter.matter_id, job.document_id
                )
                if row.version_id == job.source_version_id:
                    projected.append(
                        {
                            "document_id": job.document_id,
                            "state": job.state,
                            "stage": job.stage,
                            "progress": job.progress,
                            "message": job.message,
                            "source_name": row.display_name,
                            "review_url": f"/matters/{matter.slug}/sources/{row.action_token}",
                            "updated_at": job.updated_at,
                        }
                    )
            except KeyError:
                pass
        return {
            "active_count": len(active_source_ids),
            "attention_count": len(attention_source_ids),
            "ready_count": len(ready_jobs),
            "total_count": len(jobs),
            "items": projected,
            "status_url": f"/matters/{matter.slug}/media-activity",
            "library_url": _query_url(
                f"/matters/{matter.slug}/setup", view="list", kind="AUDIO"
            ),
        }

    def source_catalog_href(matter: MatterRecord, row, locator: int = 0) -> str:
        base = f"/matters/{matter.slug}/sources/{row.action_token}"
        if row.kind in {"AUDIO", "VIDEO"}:
            return _query_url(base, start_ms=str(locator) if locator else "")
        return _query_url(base, unit=str(max(locator, 1)) if locator else "")

    def source_catalog_projection(
        matter: MatterRecord, row, *, locator: int = 0
    ) -> dict[str, object]:
        if row.kind in {"AUDIO", "VIDEO"} and locator:
            position = format_timestamp(locator)
        elif locator:
            position = f"Section {locator}"
        else:
            position = row.count_label
        return {
            "object_id": row.document_id,
            "kind": row.kind,
            "title": row.display_name,
            "detail": f"{row.collection_name} · {position}",
            "state": row.state_label,
            "tone": row.tone,
            "review_state": row.review_state,
            "href": source_catalog_href(matter, row, locator),
            "action_token": row.action_token,
        }

    def source_browser_projection(matter, document_id: str, browse: str, origin: str = "",
                                  reader_state: Mapping[str, object] | None = None):
        origin = _source_review_return_href(matter.slug, origin)
        # Context is data, never a redirect URL; only library filters are accepted.
        try:
            parsed = parse_qs(browse, max_num_fields=20)
            values = {key: items[0] for key, items in parsed.items() if key in {
                "q", "status", "kind", "review", "collection", "source_set", "folder",
                "same_content", "matching_only", "sort", "page_size", "page", "folder_page"}}
            page = min(max(int(values.get("page", "1")), 1), 100_000)
            size = int(values.get("page_size", "50"))
            folder_page = min(max(int(values.get("folder_page", "1")), 1), 100_000)
        except ValueError as exc:
            raise HTTPException(400, "Invalid source browsing context") from exc
        options = dict(view="list", query=values.get("q", ""), status=values.get("status", ""),
            kind=values.get("kind", ""), review=values.get("review", ""),
            collection_id=values.get("collection", ""), source_set_id=values.get("source_set", ""),
            folder=values.get("folder", ""), same_content=values.get("same_content", ""),
            matching_only=values.get("matching_only", "").lower() in {"true", "1"},
            sort=values.get("sort", "oldest"), page_size=size)
        library = bench.source_library(matter, page=page, **options)
        position = bench.workspace.source_catalog_position(matter.matter_id, document_id,
            query_key=library.query.casefold(), tone=library.status, kind=library.kind,
            review_state=library.review, collection_id=library.collection_id,
            source_set_id=library.source_set_id, folder=library.folder,
            same_content=library.same_content, matching_only=library.matching_only, sort=library.sort)
        if position is not None and (position - 1) // library.page_size + 1 != library.page:
            library = bench.source_library(matter, page=(position - 1) // library.page_size + 1, **options)
        values = dict(view="list", page=str(library.page), page_size=str(library.page_size),
            folder_page=str(folder_page), sort=library.sort, q=library.query,
            status=library.status, kind=library.kind, review=library.review,
            collection=library.collection_id, source_set=library.source_set_id, folder=library.folder,
            same_content=library.same_content, matching_only="true" if library.matching_only else "")
        values = {key: value for key, value in values.items() if value}
        encoded = urlencode(values)
        def item(row, target_page):
            context = urlencode({**values, "page": str(target_page)})
            return dict(title=row.name, token=row.action_token, path=row.relative_path,
                href=_query_url(f"/matters/{matter.slug}/sources/{row.action_token}", browse=context, entity_return_to=origin))
        items = [item(row, library.page) for row in library.items]
        index = next((i for i, row in enumerate(library.items) if row.document_id == document_id), None)
        previous = following = None
        if index is not None:
            if index:
                previous = items[index - 1]
            elif library.page > 1:
                adjacent = bench.source_library(matter, page=library.page - 1, **options)
                if adjacent.items:
                    previous = item(adjacent.items[-1], adjacent.page)
            if index + 1 < len(items):
                following = items[index + 1]
            elif library.page < library.total_pages:
                adjacent = bench.source_library(matter, page=library.page + 1, **options)
                if adjacent.items:
                    following = item(adjacent.items[0], adjacent.page)
        scope_labels = [label for label in (library.status, library.kind, library.review) if label]
        if library.collection_id:
            scope_labels.insert(0, bench.workspace.source_collection(matter.matter_id, library.collection_id).name)
        if library.source_set_id:
            scope_labels.append(bench.workspace.source_set(matter.matter_id, library.source_set_id).name)
        # Folder steps keep the open source and re-scope only the queue, with the
        # library's own exact folder boundaries and filters.
        current_token = bench.source_store(matter).action_token(bench.source_store(matter).get(document_id))
        # The reader's own position, find and transcript state are kept as-is.
        kept = {key: str(value) for key, value in (reader_state or {}).items()
                if key in _READER_STATE_KEYS and value not in (None, "", 0)}
        def folder_href(folder):
            scoped = {key: value for key, value in {**values, "folder": folder}.items()
                      if value and key not in {"page", "folder_page"}}
            return _query_url(f"/matters/{matter.slug}/sources/{current_token}",
                **kept, browse=urlencode(scoped), entity_return_to=origin)
        child_folders = bench.workspace.source_catalog_folders(matter.matter_id,
            folder=library.folder, query_key=library.query.casefold(), tone=library.status,
            kind=library.kind, review_state=library.review, collection_id=library.collection_id,
            source_set_id=library.source_set_id, same_content=library.same_content,
            matching_only=library.matching_only, limit=20, offset=0)
        folders = dict(
            current=library.folder,
            parent_href=folder_href(library.folder.rpartition("/")[0]) if library.folder else None,
            all_href=folder_href("") if library.folder else None,
            items=[dict(name=folder.name, count=folder.source_count, href=folder_href(folder.path))
                   for folder in child_folders.items],
            more=child_folders.total > len(child_folders.items))
        return dict(items=items, previous=previous, next=following, library=library,
            scope_labels=scope_labels, folders=folders,
            context=encoded, position=position,
            library_href=_query_url(f"/matters/{matter.slug}/setup", **values) + "#source-library")

    def matter_resume_projection(
        matter: MatterRecord, context: AuthContext
    ) -> tuple[dict[str, object], ...]:
        """Resolve bounded, content-free activity records into current safe links."""

        try:
            activities = bench.workspace.recent_matter_activities(
                matter.matter_id, context.principal_id, limit=12
            )
        except KeyError:
            return ()
        conversations = {
            item.conversation_id: item
            for item in bench.workspace.conversation_summaries(matter.matter_id)
        }
        projected: list[dict[str, object]] = []
        for activity in activities:
            item: dict[str, object] | None = None
            if activity.activity_kind == "source":
                try:
                    source = bench.workspace.source_catalog_record(
                        matter.matter_id, activity.object_id
                    )
                except KeyError:
                    continue
                item = source_catalog_projection(
                    matter, source, locator=activity.locator
                )
                item["category"] = "Source review"
            elif activity.activity_kind == "conversation":
                conversation = conversations.get(activity.object_id)
                if conversation is None or conversation.state != "active":
                    continue
                item = {
                    "object_id": conversation.conversation_id,
                    "kind": "CONVERSATION",
                    "title": conversation.title,
                    "detail": (
                        f"{conversation.message_count} message"
                        f"{'s' if conversation.message_count != 1 else ''}"
                    ),
                    "href": _query_url(
                        f"/matters/{matter.slug}",
                        conversation=conversation.conversation_id,
                    )
                    + "#evidence-conversation",
                    "category": "Conversation",
                    "tone": "ready",
                }
            elif activity.activity_kind == "notebook":
                item = {
                    "object_id": "notebook",
                    "kind": "NOTE",
                    "title": "Case notes",
                    "detail": "Working observations and cited notes",
                    "href": f"/matters/{matter.slug}/notebook",
                    "category": "Work product",
                    "tone": "ready",
                }
            elif activity.activity_kind == "analysis":
                item = {
                    "object_id": "review-map",
                    "kind": "ANALYSIS",
                    "title": "Review map",
                    "detail": "People, places, dates, and comparisons",
                    "href": f"/matters/{matter.slug}/analysis",
                    "category": "Work product",
                    "tone": "ready",
                }
            elif activity.activity_kind == "report":
                if activity.object_id == "reports":
                    title = "Reports"
                    href = f"/matters/{matter.slug}/reports"
                else:
                    try:
                        report = bench.workspace.report(
                            matter.matter_id, activity.object_id
                        )
                    except KeyError:
                        continue
                    title = report.title
                    href = _query_url(
                        f"/matters/{matter.slug}/reports", report=report.report_id
                    )
                item = {
                    "object_id": activity.object_id,
                    "kind": "REPORT",
                    "title": title,
                    "detail": "Draft work product",
                    "href": href,
                    "category": "Work product",
                    "tone": "ready",
                }
            if item is not None:
                item["updated_at"] = activity.updated_at
                projected.append(item)
        return tuple(projected)

    def answer_projection(
        request: Request, matter: MatterRecord, job: AnswerJobRecord
    ) -> dict[str, object]:
        context = auth_context(request)
        job = bench.workspace.get_answer_job(
            matter.matter_id, context.principal_id, job.job_id
        )
        stage_label = ANSWER_STAGE_LABELS[job.stage]
        if job.state == "running" and job.stage == "queued":
            stage_label = "Starting your answer"
        workspace_url = _query_url(
            f"/matters/{matter.slug}", conversation=job.conversation_id
        ) + "#latest"
        conversation = bench.workspace.get_conversation(
            matter.matter_id, job.conversation_id
        )
        fragment_url = _query_url(
            f"/matters/{matter.slug}/assistant", conversation=job.conversation_id
        )
        projected = {
            "job_id": job.job_id,
            "conversation_id": job.conversation_id,
            "conversation_title": conversation.title,
            "state": job.state,
            "stage": job.stage,
            "stage_label": stage_label,
            "message": answer_failure_notice(job.message),
            "queue_position": bench.workspace.answer_queue_position(
                matter.matter_id, context.principal_id, job.job_id
            ),
            "attempts": job.attempts,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "can_cancel": job.state in {"queued", "running"},
            "can_retry": job.state in {"failed", "cancelled"},
            "status_url": f"/matters/{matter.slug}/answer-jobs/{job.job_id}",
            "result_url": f"/matters/{matter.slug}/answer-jobs/{job.job_id}/result",
            "cancel_url": f"/matters/{matter.slug}/answer-jobs/{job.job_id}/cancel",
            "retry_url": f"/matters/{matter.slug}/answer-jobs/{job.job_id}/retry",
            "workspace_url": workspace_url,
            "fragment_url": fragment_url,
            "open_url": _query_url(
                f"/matters/{matter.slug}", conversation=job.conversation_id
            )
            + "#evidence-conversation",
            "events": [
                {
                    "ordinal": event.ordinal,
                    "state": event.state,
                    "stage": event.stage,
                    "stage_label": ANSWER_STAGE_LABELS[event.stage],
                    "message": answer_failure_notice(event.message),
                    "created_at": event.created_at,
                }
                for event in bench.workspace.answer_events(
                    matter.matter_id, context.principal_id, job.job_id
                )
            ],
        }

        with bench.workspace._lock:
            has_context = bench.workspace.connection.execute('SELECT 1 FROM workbench_answer_context WHERE job_id=? AND matter_id=?',
                                                             (job.job_id, matter.matter_id)).fetchone()
        projected['context_url'] = f'/matters/{matter.slug}/answer-jobs/{job.job_id}/context' if has_context else None

        origin = _source_review_return_href(matter.slug, request.query_params.get("entity_return_to", ""))
        for key in ("status_url", "result_url", "cancel_url", "retry_url", "workspace_url", "fragment_url", "open_url"):
            projected[key] = _entity_context_href(projected[key], origin)
        return projected

    @app.get("/health")
    def health() -> dict[str, object]:
        capabilities = bench.capabilities()
        storage = bench.storage_capacity_projection(include_managed_usage=False)
        maintenance = (
            bench.maintenance.status()
            if bench.maintenance is not None
            else {
                "enabled": False,
                "last_run_at": "",
                "last_error": "",
                "uploads_cleaned": 0,
                "matters_purged": 0,
                "matters_deferred": 0,
            }
        )
        return {
            "status": (
                "ok"
                if capabilities["answering"] == "ready" and storage["ready"]
                else "degraded"
            ),
            "product": "RecordBench",
            "data": "private RecordBench node",
            "capabilities": capabilities,
            "ingest_jobs": bench.workspace.ingest_counts(),
            "media_jobs": bench.workspace.media_job_counts(),
            "media_summaries": bench.workspace.media_summary_counts(),
            "answer_jobs": bench.workspace.answer_counts(),
            "research_jobs": bench.workspace.research_counts(),
            "review_runs": bench.workspace.review_counts(),
            "report_compilation_jobs": bench.report_compilation.jobs.counts(),
            "maintenance": maintenance,
            "storage": {
                "status": "ready" if storage["ready"] else "degraded",
                "reserve_satisfied": bool(storage["ready"]),
                "optimized_finalize": bool(storage["optimized_finalize"]),
            },
        }

    if os.getenv("CASE_INTELLIGENCE_ACCEPTANCE_DIAGNOSTICS") == "1":
        @app.get("/internal/acceptance")
        def acceptance_diagnostics(request: Request):
            if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
                raise HTTPException(403, "Acceptance diagnostics are loopback-only.")
            return {
                "generation_requests_started": bench.generator.requests_started,
                "generation_requests_completed": bench.generator.requests_completed,
                "learned_retrieval": bench.learned_retrieval,
            }

    @app.get("/auth/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(
        request: Request,
        next_path: str = Query("/", alias="next", max_length=2_048),
        error: str = Query("", max_length=240),
    ):
        destination = IdentityService.safe_next(next_path)
        existing = identity.resolve(request.cookies.get(SESSION_COOKIE))
        if (
            existing is not None
            and (
                identity.auth_mode != "kerberos"
                or identity.kerberos_request_valid(
                    existing,
                    *kerberos_headers(request.headers),
                )
            )
        ):
            return RedirectResponse(destination, status_code=303)
        if identity.auth_mode == "kerberos":
            try:
                context, raw_token = identity.login_kerberos(
                    *kerberos_headers(request.headers),
                )
            except KerberosAuthenticationError as exc:
                audit(
                    request,
                    "auth.login",
                    "denied",
                    details={"auth_method": "spnego"},
                )
                return templates.TemplateResponse(
                    request=request,
                    name="workbench_login.html",
                    status_code=401,
                    context={
                        "identities": (),
                        "login_challenge": "",
                        "next_path": destination,
                        "error": str(exc),
                        "auth_mode": identity.auth_mode,
                        "oidc_start_url": "",
                        "kerberos_continue_url": _query_url(
                            "/auth/login", next=destination
                        ),
                        "signed_out": False,
                    },
                )
            audit(
                request,
                "auth.login",
                "success",
                context=context,
                object_type="session",
                object_id=(
                    context.session.session_id if context.session is not None else None
                ),
                details={
                    "auth_method": "spnego",
                    **(
                        {"role": "administrator"}
                        if context.is_administrator
                        else {}
                    ),
                },
            )
            response = RedirectResponse(destination, status_code=303)
            response.set_cookie(
                SESSION_COOKIE,
                raw_token,
                **identity.session_cookie_options,
            )
            return response
        challenge = (
            identity.new_login_challenge()
            if identity.auth_mode in {"preview", "local"}
            else ""
        )
        response = templates.TemplateResponse(
            request=request,
            name="workbench_login.html",
            context={
                "identities": identity.preview_identities(),
                "login_challenge": challenge,
                "next_path": destination,
                "error": error,
                "auth_mode": identity.auth_mode,
                "oidc_start_url": _query_url("/auth/oidc/start", next=destination),
                "kerberos_continue_url": _query_url("/auth/login", next=destination),
                "signed_out": False,
            },
        )
        if identity.auth_mode in {"preview", "local"}:
            response.set_cookie(
                LOGIN_CHALLENGE_COOKIE,
                challenge,
                **identity.login_cookie_options,
            )
        return response

    @app.get("/auth/signed-out", response_class=HTMLResponse, include_in_schema=False)
    def signed_out_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="workbench_login.html",
            context={
                "identities": (),
                "login_challenge": "",
                "next_path": "/",
                "error": "",
                "auth_mode": identity.auth_mode,
                "oidc_start_url": _query_url("/auth/oidc/start", next="/"),
                "kerberos_continue_url": _query_url("/auth/login", next="/"),
                "signed_out": True,
            },
        )

    @app.get("/auth/oidc/start", include_in_schema=False)
    async def start_oidc_login(
        request: Request,
        next_path: str = Query("/", alias="next", max_length=2_048),
    ):
        destination = IdentityService.safe_next(next_path)
        if identity.auth_mode != "oidc":
            raise HTTPException(404, "Organization sign-in is unavailable.")
        try:
            start = await identity.begin_oidc_login(destination)
        except OidcAuthenticationError as exc:
            audit(
                request,
                "auth.login",
                "failure",
                details={"auth_method": "oidc"},
            )
            return RedirectResponse(
                _query_url("/auth/login", next=destination, error=str(exc)),
                status_code=303,
            )
        response = RedirectResponse(start.authorization_url, status_code=302)
        response.set_cookie(
            OIDC_STATE_COOKIE,
            start.state,
            **identity.oidc_state_cookie_options,
        )
        return response

    @app.get("/auth/oidc/callback", include_in_schema=False)
    async def oidc_callback(
        request: Request,
        code: str | None = Query(None, max_length=4_096),
        state: str | None = Query(None, max_length=256),
        error: str | None = Query(None, max_length=128),
    ):
        if identity.auth_mode != "oidc":
            raise HTTPException(404, "Organization sign-in is unavailable.")
        try:
            context, raw_token, destination = await identity.finish_oidc_login(
                cookie_state=request.cookies.get(OIDC_STATE_COOKIE),
                returned_state=state,
                code=code,
                provider_error=error,
            )
        except OidcAuthenticationError as exc:
            audit(
                request,
                "auth.login",
                "denied",
                details={"auth_method": "oidc"},
            )
            response = RedirectResponse(
                _query_url("/auth/login", error=str(exc)),
                status_code=303,
            )
            response.delete_cookie(OIDC_STATE_COOKIE, path="/auth/oidc")
            return response
        audit(
            request,
            "auth.login",
            "success",
            context=context,
            object_type="session",
            object_id=context.session.session_id if context.session is not None else None,
            details={"auth_method": "oidc"},
        )
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(SESSION_COOKIE, raw_token, **identity.session_cookie_options)
        response.delete_cookie(OIDC_STATE_COOKIE, path="/auth/oidc")
        return response

    @app.post("/auth/login", include_in_schema=False)
    def login(
        request: Request,
        identity_subject: str = Form(..., max_length=80),
        login_challenge: str = Form(..., max_length=256),
        next_path: str = Form("/", alias="next", max_length=2_048),
    ):
        destination = IdentityService.safe_next(next_path)
        if not identity.valid_login_challenge(
            request.cookies.get(LOGIN_CHALLENGE_COOKIE), login_challenge
        ):
            return RedirectResponse(
                _query_url(
                    "/auth/login",
                    next=destination,
                    error="The sign-in page expired. Please choose your preview identity again.",
                ),
                status_code=303,
            )
        try:
            context, raw_token = identity.login_preview(identity_subject)
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url("/auth/login", next=destination, error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "auth.login",
            "success",
            context=context,
            object_type="session",
            object_id=context.session.session_id if context.session is not None else None,
            details={"auth_method": context.auth_method},
        )
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(SESSION_COOKIE, raw_token, **identity.session_cookie_options)
        response.delete_cookie(LOGIN_CHALLENGE_COOKIE, path="/auth")
        return response

    @app.post("/auth/local", include_in_schema=False)
    def local_login(
        request: Request,
        username: str = Form(..., max_length=128),
        password: str = Form(..., max_length=1_024),
        login_challenge: str = Form(..., max_length=256),
        next_path: str = Form("/", alias="next", max_length=2_048),
    ):
        destination = IdentityService.safe_next(next_path)
        if identity.auth_mode != "local":
            raise HTTPException(404, "Local account sign-in is unavailable.")
        if not identity.valid_login_challenge(
            request.cookies.get(LOGIN_CHALLENGE_COOKIE), login_challenge
        ):
            return RedirectResponse(
                _query_url(
                    "/auth/login",
                    next=destination,
                    error="The sign-in page expired. Please try again.",
                ),
                status_code=303,
            )
        try:
            context, raw_token = identity.login_local(
                username,
                password,
                client_key=request.client.host if request.client is not None else "unknown",
            )
        except LocalAuthenticationError as exc:
            audit(
                request,
                "auth.login",
                "denied",
                details={"auth_method": "local"},
            )
            return RedirectResponse(
                _query_url("/auth/login", next=destination, error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "auth.login",
            "success",
            context=context,
            object_type="session",
            object_id=context.session.session_id if context.session is not None else None,
            details={
                "auth_method": "local",
                **({"role": "administrator"} if context.is_administrator else {}),
            },
        )
        if destination == "/" and context.is_administrator and not bench.matters(context.principal_id):
            destination = "/admin/setup"
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(SESSION_COOKIE, raw_token, **identity.session_cookie_options)
        response.delete_cookie(LOGIN_CHALLENGE_COOKIE, path="/auth")
        return response

    @app.post(
        "/auth/logout",
        dependencies=[Depends(require_csrf)],
        include_in_schema=False,
    )
    def logout(request: Request):
        context = auth_context(request)
        audit(
            request,
            "auth.logout",
            "success",
            context=context,
            object_type="session",
            object_id=context.session.session_id if context.session is not None else None,
            details={"auth_method": context.auth_method},
        )
        identity.logout(context)
        destination = (
            "/auth/signed-out" if identity.auth_mode == "kerberos" else "/auth/login"
        )
        response = RedirectResponse(destination, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post(
        "/preferences/theme",
        dependencies=[Depends(require_csrf)],
        include_in_schema=False,
    )
    def set_appearance_theme(
        request: Request,
        theme: str = Form(..., max_length=24),
        next_path: str = Form("/", alias="next", max_length=2_048),
    ):
        context = auth_context(request)
        # Cyberpunk and retro are schema-ready design directions. Only themes
        # with a complete, reviewed component treatment are exposed to staff.
        if theme not in {"light", "dusk"}:
            raise HTTPException(400, "Choose an available appearance theme.")
        try:
            preference = bench.workspace.set_principal_theme(
                context.principal_id, theme
            )
        except WorkspaceProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(
            request,
            "preference.theme",
            "success",
            context=context,
            object_type="principal",
            object_id=context.principal_id,
        )
        return RedirectResponse(
            IdentityService.safe_next(next_path, default="/"),
            status_code=303,
            headers=response_headers("Theme", preference.theme),
        )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(PACKAGE_ROOT / "static/favicon.ico", media_type="image/x-icon")

    register_local_account_routes(
        app, identity=identity, bench=bench, templates=templates,
        auth_context=auth_context, base_context=base_context, audit=audit,
    )
    from .team_groups import register_team_group_routes
    register_team_group_routes(
        app, identity=identity, bench=bench, templates=templates,
        auth_context=auth_context, base_context=base_context,
        require_csrf=require_csrf, authorized_matter=authorized_matter, audit=audit,
    )
    from .onboarding import register_onboarding_routes
    register_onboarding_routes(
        app, identity=identity, bench=bench, templates=templates,
        auth_context=auth_context, base_context=base_context, audit=audit,
    )

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def administrator_console(
        request: Request,
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        if not context.is_administrator:
            audit(
                request,
                "admin.console",
                "denied",
                context=context,
            )
            raise HTTPException(403, "Administrator access is required.")
        matter_rows: list[dict[str, object]] = []
        now = bench.workspace.current_time()
        for matter in bench.workspace.all_matters():
            owner = bench.workspace.get_principal(matter.owner_id)
            try:
                bench.workspace.membership(matter.matter_id, context.principal_id)
                current_is_member = True
            except KeyError:
                current_is_member = False
            activity_at = bench.workspace.matter_activity_at(matter.matter_id)
            inactive_days = max(
                0,
                int((now - bench._parse_utc(activity_at)).total_seconds() // 86_400),
            )
            retention = bench.retention_projection(matter)
            stale_reasons: list[str] = []
            if inactive_days >= 30:
                stale_reasons.append(f"No recorded activity for {inactive_days} days")
            if retention["status"] in {"unscheduled", "warning", "grace", "due"}:
                stale_reasons.append(str(retention["label"]))
            matter_rows.append(
                {
                    "matter": matter,
                    "owner": owner,
                    "member_count": len(bench.workspace.members(matter.matter_id)),
                    "current_is_member": current_is_member,
                    "storage": bench.matter_storage_projection(matter),
                    "retention": retention,
                    "activity_at": activity_at,
                    "inactive_days": inactive_days,
                    "stale_reasons": tuple(stale_reasons),
                }
            )
        closure_recoveries = tuple(
            {
                "matter": matter,
                "owner": bench.workspace.get_principal(matter.owner_id),
            }
            for matter in bench.workspace.closure_recovery_matters(
                context.principal_id,
                administrator_override=True,
            )
        )
        principals = identity.membership_candidates(include_disabled=True)
        storage_capacity = bench.storage_capacity_projection(include_managed_usage=True)
        abandoned_uploads = bench.workspace.abandoned_upload_sessions(
            maximum_age_hours=24
        )
        matter_lookup = {
            row["matter"].matter_id: row["matter"] for row in matter_rows
        }
        abandoned_upload_rows = tuple(
            {
                "session": session,
                "matter": matter_lookup.get(session.matter_id),
            }
            for session in abandoned_uploads
        )
        stale_matters = tuple(row for row in matter_rows if row["stale_reasons"])
        maintenance_status = (
            bench.maintenance.status()
            if bench.maintenance is not None
            else {"enabled": False, "last_run_at": "", "last_error": ""}
        )
        backup_status = _backup_status_projection()
        malware_status = scanner_status(bench.malware_scanner, force=True)
        model_portfolio = model_portfolio_projection(
            bench.generator,
            learned_retrieval=bench.learned_retrieval,
            answer_workers=bench.answers.worker_count if bench.answers is not None else 0,
            research_workers=(
                bench.research.worker_count if bench.research is not None else 0
            ),
            review_workers=(
                bench.full_review.worker_count if bench.full_review is not None else 0
            ),
            review_source_concurrency=(
                bench.full_review.source_concurrency
                if bench.full_review is not None
                else 0
            ),
            media_ready=bench.media.available,
        )
        audit(
            request,
            "admin.console",
            "success",
            context=context,
            details={"role": "administrator", "count": len(matter_rows)},
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_admin.html",
            context={
                **base_context(request),
                "admin_matters": tuple(matter_rows),
                "closure_recoveries": closure_recoveries,
                "stale_matters": stale_matters,
                "admin_principals": principals,
                "abandoned_uploads": abandoned_uploads,
                "abandoned_upload_rows": abandoned_upload_rows,
                "maintenance_status": maintenance_status,
                "backup_status": backup_status,
                "malware_status": malware_status,
                "malware_scan_mode": bench.malware_scan_mode,
                "model_portfolio": model_portfolio,
                "ingest_counts": bench.workspace.ingest_counts(),
                "media_counts": bench.workspace.media_job_counts(),
                "media_summary_counts": bench.workspace.media_summary_counts(),
                "answer_counts": bench.workspace.answer_counts(),
                "research_counts": bench.workspace.research_counts(),
                "review_counts": bench.workspace.review_counts(),
                "storage_capacity": storage_capacity,
                "storage_policy": bench.storage_policy,
                "format_bytes": format_bytes,
                "notice": notice,
                "error": error,
            },
        )

    @app.post(
        "/admin/matters/{slug}/retention",
        dependencies=[Depends(require_csrf)],
        include_in_schema=False,
    )
    def administrator_set_matter_retention(
        request: Request,
        slug: str,
        expires_on: str = Form(..., max_length=10),
    ):
        context = auth_context(request)
        if not context.is_administrator:
            raise HTTPException(403, "Administrator access is required.")
        matter = authorized_matter(
            request, slug, allow_administrator_write=True
        )
        try:
            retention = bench.retention_for_date(
                matter,
                context.principal_id,
                expires_on,
                administrator_override=True,
            )
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url("/admin", error=str(exc)), status_code=303
            )
        audit(
            request,
            "matter.retention",
            "success",
            context=context,
            matter=matter,
            object_type="matter",
            object_id=matter.matter_id,
            details={"state": "scheduled", "role": "administrator"},
        )
        return RedirectResponse(
            _query_url(
                "/admin",
                notice=(
                    f"Review end date updated. Automatic deletion is scheduled for "
                    f"{bench.retention_projection(matter)['purge_on']}."
                ),
            ),
            status_code=303,
        )

    @app.post(
        "/admin/maintenance/uploads",
        dependencies=[Depends(require_csrf)],
        include_in_schema=False,
    )
    def administrator_cleanup_uploads(request: Request):
        context = auth_context(request)
        if not context.is_administrator:
            raise HTTPException(403, "Administrator access is required.")
        cleaned = bench.cleanup_abandoned_uploads(maximum_age_hours=24)
        audit(
            request,
            "admin.maintenance",
            "success",
            context=context,
            object_type="upload_session",
            details={"count": cleaned, "state": "completed"},
        )
        return RedirectResponse(
            _query_url(
                "/admin",
                notice=(
                    f"Cleaned {cleaned} abandoned upload "
                    f"{'session' if cleaned == 1 else 'sessions'}."
                ),
            ),
            status_code=303,
        )

    @app.get("/", include_in_schema=False)
    def home(request: Request) -> RedirectResponse:
        context = auth_context(request)
        if context.is_administrator and not bench.matters(context.principal_id):
            return RedirectResponse("/admin/setup", status_code=303)
        matters = bench.matters(
            context.principal_id,
            administrator=context.is_administrator,
        )
        return RedirectResponse(
            f"/matters/{matters[0].slug}/home" if matters else "/matters/new",
            status_code=303,
        )

    @app.get("/activity", response_class=HTMLResponse, include_in_schema=False)
    def workspace_activity(
        request: Request,
        matter: str = Query("", max_length=80),
    ):
        context = auth_context(request)
        activity = activity_center_projection(context, active_slug=matter)
        return templates.TemplateResponse(
            request=request,
            name="workbench_activity_center.html",
            context={"activity": activity},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/matters/new", response_class=HTMLResponse)
    def new_matter(
        request: Request,
        error: str = Query("", max_length=240),
        notice: str = Query("", max_length=240),
    ):
        return templates.TemplateResponse(
            request=request,
            name="workbench_new_matter.html",
            context={**base_context(request), "error": error, "notice": notice},
        )

    @app.get("/matters/manage", response_class=HTMLResponse)
    def manage_matters(request: Request):
        context = auth_context(request)
        matters = bench.matters(
            context.principal_id,
            administrator=context.is_administrator,
        )
        rows: list[dict[str, object]] = []
        for matter in matters:
            try:
                membership = bench.workspace.membership(
                    matter.matter_id, context.principal_id
                )
            except KeyError:
                membership = None
            owner = bench.workspace.get_principal(matter.owner_id)
            is_owner = membership is not None and membership.role == "owner"
            if is_owner:
                authority_label = "You own this matter"
            elif context.is_administrator:
                authority_label = "Administrator oversight"
            else:
                authority_label = "Case team access"
            rows.append(
                {
                    "matter": matter,
                    "owner_name": owner.display_name,
                    "authority_label": authority_label,
                    "retention": bench.retention_projection(matter),
                    "can_close": is_owner or context.is_administrator,
                }
            )
        closure_recoveries: list[dict[str, object]] = []
        for matter in bench.workspace.closure_recovery_matters(
            context.principal_id,
            administrator_override=context.is_administrator,
        ):
            owner = bench.workspace.get_principal(matter.owner_id)
            closure_recoveries.append(
                {
                    "matter": matter,
                    "owner_name": owner.display_name,
                    "administrator_override": bool(
                        context.is_administrator
                        and matter.owner_id != context.principal_id
                    ),
                }
            )
        audit(
            request,
            "matter.manage",
            "success",
            context=context,
            details={"count": len(rows) + len(closure_recoveries)},
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_manage_matters.html",
            context={
                **base_context(request),
                "managed_matters": tuple(rows),
                "closure_recoveries": tuple(closure_recoveries),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/matters", dependencies=[Depends(require_csrf)])
    def create_matter(
        request: Request,
        name: str = Form(..., max_length=140),
        descriptor: str = Form("", max_length=240),
        retention_days: int = Form(30),
    ):
        context = auth_context(request)
        if retention_days not in {7, 30, 60, 90}:
            return RedirectResponse(
                "/matters/new?error="
                + quote_plus("Choose a supported temporary review period."),
                status_code=303,
            )
        try:
            matter = bench.create_matter(
                name,
                descriptor,
                context.principal_id,
                retention_days=retention_days,
            )
        except WorkspaceProblem as exc:
            return RedirectResponse(
                "/matters/new?error=" + quote_plus(str(exc)), status_code=303
            )
        audit(
            request,
            "matter.create",
            "success",
            context=context,
            matter=matter,
            object_type="matter",
            object_id=matter.matter_id,
            details={"role": "owner", "state": "scheduled"},
        )
        return RedirectResponse(f"/matters/{matter.slug}/setup", status_code=303)

    @app.post(
        "/matters/{slug}/retention",
        dependencies=[Depends(require_csrf)],
        include_in_schema=False,
    )
    def set_matter_retention(
        request: Request,
        slug: str,
        expires_on: str = Form(..., max_length=10),
        next_path: str = Form("", alias="next", max_length=2_048),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            bench.retention_for_date(matter, context.principal_id, expires_on)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except WorkspaceProblem as exc:
            destination = IdentityService.safe_next(
                next_path, default=f"/matters/{slug}"
            )
            return RedirectResponse(
                _query_url(destination.split("?", 1)[0], error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "matter.retention",
            "success",
            context=context,
            matter=matter,
            object_type="matter",
            object_id=matter.matter_id,
            details={"state": "scheduled", "role": "owner"},
        )
        destination = IdentityService.safe_next(
            next_path, default=f"/matters/{slug}"
        )
        return RedirectResponse(destination, status_code=303)

    @app.get(
        "/matters/{slug}/assistant",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def matter_assistant(request: Request, slug: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            membership = bench.workspace.membership(
                matter.matter_id, context.principal_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        assistant = assistant_context(
            request,
            matter,
            context,
            membership_present=membership is not None,
            readiness=matter_readiness_projection(matter),
        )
        if assistant is None:
            raise HTTPException(404, "Matter assistant is unavailable")
        return templates.TemplateResponse(
            request=request,
            name="workbench_assistant.html",
            context={
                "assistant": assistant,
                "matter": matter,
                "actor_initials": context.initials,
                "csrf_token": context.csrf_token,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/matters/{slug}/processing-status")
    def matter_processing_status(request: Request, slug: str):
        try:
            matter = authorized_matter(request, slug)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        return JSONResponse(
            matter_readiness_projection(matter),
            headers={"Cache-Control": "no-store"},
        )

    def render_matter_settings(
        request: Request, slug: str, *, proposed_name: str | None = None,
        rename_error: str = "", notice: str = "", status_code: int = 200,
    ):
        context = auth_context(request)
        matter = authorized_matter(request, slug, allow_administrator_write=True)
        owner = bench.workspace.get_principal(matter.owner_id)
        administrator_override = bool(
            context.is_administrator and matter.owner_id != context.principal_id
        )
        audit(
            request,
            "matter.settings",
            "success",
            context=context,
            matter=matter,
            object_type="matter",
            object_id=matter.matter_id,
            details={
                "role": "administrator" if administrator_override else "owner"
                if matter.owner_id == context.principal_id
                else "member"
            },
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_matter_settings.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "owner_name": owner.display_name,
                "administrator_override": administrator_override,
                "proposed_name": matter.display_name if proposed_name is None else proposed_name,
                "rename_error": rename_error,
                "notice": notice,
            },
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/matters/{slug}/settings", response_class=HTMLResponse)
    def matter_settings(request: Request, slug: str, renamed: bool = False):
        return render_matter_settings(request, slug, notice="Matter renamed." if renamed else "")

    @app.post("/matters/{slug}/rename", dependencies=[Depends(require_csrf)])
    def rename_matter(
        request: Request, slug: str, name: str = Form("", max_length=4096),
        expected_name: str = Form("", max_length=140),
    ):
        context = auth_context(request)
        matter = authorized_matter(request, slug, allow_administrator_write=True)
        if matter.owner_id != context.principal_id and not context.is_administrator:
            audit(request, "matter.rename", "denied", context=context, matter=matter)
            raise HTTPException(403, "Only the matter owner or an administrator can rename this matter.")
        try:
            bench.workspace.rename_matter(
                slug, context.principal_id, name, expected_name=expected_name,
                administrator_override=context.is_administrator,
                request_id=request.state.request_id,
                session_id=context.session.session_id if context.session else None,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except WorkspaceProblem as exc:
            return render_matter_settings(
                request, slug, proposed_name=name, rename_error=str(exc),
                status_code=409 if isinstance(exc, MatterNameConflict) else 400,
            )
        return RedirectResponse(f"/matters/{slug}/settings?renamed=true", status_code=303)

    @app.get("/matters/{slug}/home", response_class=HTMLResponse)
    def matter_home(
        request: Request,
        slug: str,
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            readiness = matter_readiness_projection(matter)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            queue = bench.source_library(
                matter,
                view="list",
                status="ready",
                review="unreviewed",
                sort="oldest",
                page_size=25,
            )
            attention = bench.workspace.source_catalog_page(
                matter.matter_id, tone="attention", actionable_only=True,
                sort="status", limit=3,
            )
            catalog = bench.workspace.source_catalog_page(
                matter.matter_id, limit=1
            )
            media_activity = media_activity_projection(matter)
            resumes = matter_resume_projection(matter, context)
            notebook = bench.workspace.notebook_page(
                matter.matter_id,
                context.principal_id,
                administrator_override=administrator_override,
                status="all",
                page_size=10,
            )
            reports = bench.workspace.reports(
                matter.matter_id,
                context.principal_id,
                administrator_override=administrator_override,
            )
            findings = bench.workspace.review_findings(matter.matter_id)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc

        queue_items = tuple(
            source_catalog_projection(
                matter,
                bench.workspace.source_catalog_record(matter.matter_id, row.document_id),
            )
            for row in queue.items[:4]
        )
        attention_items = tuple(
            source_catalog_projection(
                matter,
                bench.workspace.source_catalog_record(matter.matter_id, row.document_id),
            )
            for row in attention.items[:3]
        )
        continue_item = resumes[0] if resumes else (
            queue_items[0] if queue_items else None
        )
        continue_url = (
            str(continue_item["href"])
            if continue_item is not None
            else f"/matters/{matter.slug}"
        )
        stats = dict(catalog.stats)
        total = stats["total"]
        reviewed = stats["reviewed"]
        progress_percent = round((reviewed / total) * 100) if total else 0
        stats["attention"] = max(0, stats["attention"] - int(readiness["playback_only_count"]))
        status_parts = [
            f"{stats['ready']} searchable",
            f"{readiness['playback_only_count']} playback only" if readiness["playback_only_count"] else "",
            f"{stats['processing']} processing" if stats["processing"] else "",
            f"{stats['attention']} need attention" if stats["attention"] else "",
        ]
        audit(
            request,
            "matter.home",
            "success",
            context=context,
            matter=matter,
            object_type="matter",
            object_id=matter.matter_id,
            details={"count": total},
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_matter_home.html",
            context={
                **base_context(request, matter, readiness=readiness),
                "matter": matter,
                "continue_item": continue_item,
                "continue_url": continue_url,
                "recent_work": resumes[:3],
                "queue_items": queue_items,
                "queue_total": queue.total,
                "attention_items": attention_items,
                "stats": stats,
                "progress_percent": progress_percent,
                "status_line": " · ".join(part for part in status_parts if part),
                "media_activity": media_activity,
                "work_product_counts": {
                    "notes": notebook.total,
                    "findings": len(findings),
                    "reports": len(reports),
                },
                "notice": notice,
                "error": error,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/matters/{slug}/work-product", response_class=HTMLResponse)
    def matter_work_product(
        request: Request,
        slug: str,
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            read_actor_id = (
                matter.owner_id if administrator_override else context.principal_id
            )
            notebook = bench.workspace.notebook_page(
                matter.matter_id, read_actor_id, status="all", page_size=10
            )
            findings = bench.workspace.review_findings(matter.matter_id)
            reports = bench.workspace.reports(matter.matter_id, read_actor_id)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        audit(
            request,
            "work_product.open",
            "success",
            context=context,
            matter=matter,
            details={"count": notebook.total + len(findings) + len(reports)},
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_work_product.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "notebook": notebook,
                "findings": findings,
                "reports": reports,
                "notice": notice,
                "error": error,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/matters/{slug}/setup", response_class=HTMLResponse)
    def setup_matter(
        request: Request,
        slug: str,
        plan: str = Query("", max_length=64),
        view: str = Query("overview", max_length=16),
        q: str = Query("", max_length=240),
        status: str = Query("", max_length=20),
        kind: str = Query("", max_length=12),
        review: str = Query("", max_length=20),
        collection: str = Query("", max_length=80),
        source_set: str = Query("", max_length=80),
        folder: str = Query("", max_length=2048),
        same_content: str = Query("", max_length=32),
        matching_only: bool = Query(False),
        folder_page: int = Query(1, ge=1, le=100_000),
        sort: str = Query("newest", max_length=20),
        page: int = Query(1, ge=1, le=100_000),
        page_size: int = Query(50, ge=1, le=100),
        receipt_page: int = Query(1, ge=1, le=100_000),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            ingest_plan = (
                bench.workspace.get_ingest_plan(matter.matter_id, plan) if plan else None
            )
            plan_items = (
                bench.workspace.ingest_plan_items(matter.matter_id, plan)
                if ingest_plan is not None
                else ()
            )
            library = bench.source_library(
                matter,
                view=view,
                query=q,
                status=status,
                kind=kind,
                review=review,
                collection_id=collection,
                source_set_id=source_set,
                folder=folder,
                same_content=same_content, matching_only=matching_only,
                sort=sort,
                page=page,
                page_size=page_size,
            )
            folder_filters = dict(folder=library.folder, query_key=library.query.casefold(),
                tone=library.status, kind=library.kind, review_state=library.review,
                collection_id=library.collection_id, source_set_id=library.source_set_id,
                same_content=library.same_content, matching_only=library.matching_only)
            folders = bench.workspace.source_catalog_folders(matter.matter_id,
                **folder_filters, limit=50, offset=(folder_page - 1) * 50)
            folder_pages = max(math.ceil(folders.total / 50), 1)
            if folder_page > folder_pages:
                folder_page = folder_pages
                folders = bench.workspace.source_catalog_folders(matter.matter_id,
                    **folder_filters, limit=50, offset=(folder_page - 1) * 50)
            collections = bench.workspace.source_collections(matter.matter_id)
            source_sets = bench.workspace.source_sets(matter.matter_id)
            active_collection = next(
                (
                    item
                    for item in collections
                    if item.collection_id == library.collection_id
                ),
                None,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter, import plan, or source group not found") from exc
        except WorkspaceProblem as exc:
            raise HTTPException(400, str(exc)) from exc

        def library_url(target_page: int, **changes: str) -> str:
            values = {
                "view": library.view,
                "q": library.query,
                "status": library.status,
                "kind": library.kind,
                "review": library.review,
                "collection": library.collection_id,
                "source_set": library.source_set_id,
                "folder": library.folder,
                "same_content": library.same_content,
                "matching_only": "true" if library.matching_only else "",
                "sort": library.sort,
                "page_size": str(library.page_size),
                "page": str(target_page),
            }
            values.update(changes)
            return _query_url(f"/matters/{matter.slug}/setup", **values)

        with bench.workspace._lock:
            team_members = bench.workspace.members(matter.matter_id)
            team_reasons = {
                member.principal_id: bench.workspace.access_reasons(matter.matter_id, member.principal_id)
                for member in team_members
            }
            matter_groups = bench.workspace.matter_group_grants(matter.matter_id)
            available_groups = bench.workspace.team_groups()
            inactive_direct_grants = tuple(
                member for member in bench.workspace.direct_members(matter.matter_id)
                if member.principal_id not in team_reasons
            )
        direct_principals = {
            principal_id for principal_id, reasons in team_reasons.items()
            if "Owner" in reasons or "Direct member" in reasons
        }
        return templates.TemplateResponse(
            request=request,
            name="workbench_setup.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "sources": library.items,
                "source_library": library,
                "source_collections": collections,
                "active_source_collection": active_collection,
                "source_sets": source_sets,
                "matter_storage": bench.matter_storage_projection(matter),
                "storage_policy": bench.storage_policy,
                "upload_limits": {
                    "items": MAX_UPLOAD_INTAKE_ITEMS,
                    "item_label": f"{MAX_UPLOAD_INTAKE_ITEMS:,}",
                    "batch_items": MAX_UPLOAD_SESSION_ITEMS,
                    "batch_item_label": f"{MAX_UPLOAD_SESSION_ITEMS:,}",
                    "preflight_request_bytes": MAX_UPLOAD_PREFLIGHT_REQUEST_BYTES,
                    "document_bytes": bench.storage_policy.document_file_bytes,
                    "media_bytes": bench.storage_policy.media_file_bytes,
                    "collection_bytes": bench.storage_policy.upload_session_bytes,
                    "document_label": format_bytes(
                        bench.storage_policy.document_file_bytes
                    ),
                    "media_label": format_bytes(bench.storage_policy.media_file_bytes),
                    "collection_label": format_bytes(
                        bench.storage_policy.upload_session_bytes
                    ),
                },
                "library_url": library_url,
                "source_return_query": urlparse(library_url(library.page, view="list", folder_page=str(folder_page))).query,
                "source_folders": folders,
                "folder_page": folder_page,
                "folder_pages": folder_pages,
                "folder_parent": library.folder.rpartition("/")[0],
                "recent_upload_sessions": bench.workspace.recent_upload_sessions(
                    matter.matter_id, context.principal_id
                ),
                "intake_receipts": IntakeReceipts(bench.workspace).recent(
                    matter.matter_id,
                    context.principal_id,
                    limit=11,
                    offset=(receipt_page - 1) * 10,
                    administrator_override=(
                        getattr(request.state, "administrator_matter_override", None)
                        == matter.matter_id
                    ),
                ),
                "receipt_page": receipt_page,
                "registered_locations": bench.source_registry.staff_locations(),
                "ingest_plan": ingest_plan,
                "plan_items": plan_items[:200],
                "plan_items_omitted": max(len(plan_items) - 200, 0),
                "notice": notice,
                "error": error,
                "members": team_members,
                "access_reasons": team_reasons,
                "inactive_direct_grants": inactive_direct_grants,
                "matter_groups": matter_groups,
                "available_groups": available_groups,
                "available_principals": tuple(
                    principal
                    for principal in identity.membership_candidates()
                    if principal.principal_id
                    not in direct_principals
                ),
            },
        )

    def reconcile_upload_session(
        matter: MatterRecord, actor_id: str, session_id: str
    ):
        session, items = bench.workspace.upload_session(
            matter.matter_id, actor_id, session_id
        )
        store = bench.source_store(matter)
        # Serialize staging inspection with this matter's chunk writes. Never
        # hold the process-wide workspace lock while touching the filesystem.
        with store._lock:
            session, items = bench.workspace.upload_session(
                matter.matter_id, actor_id, session_id
            )
            for item in items:
                if item.state not in {"pending", "uploading", "uploaded"}:
                    continue
                problem: str | None = None
                try:
                    actual = store.resumable_size(
                        item.upload_item_id, expected_size=item.expected_size
                    )
                except UploadProblem as exc:
                    problem = str(exc)
                if problem is None and actual == item.received_size:
                    continue
                # Terminal ledger transitions can commit during inspection.
                # Refresh only items needing recovery, and keep that read and
                # mutation atomic; ordinary scans need no per-item ledger query.
                with bench.workspace._lock:
                    item = bench.workspace.upload_item(
                        matter.matter_id, actor_id, session_id, item.upload_item_id
                    )
                    if item.state not in {"pending", "uploading", "uploaded"}:
                        continue
                    if problem is not None or actual < item.received_size:
                        bench.workspace.fail_upload_item(
                            matter.matter_id, actor_id, session_id,
                            item.upload_item_id,
                            problem if problem is not None else
                            "The saved upload is incomplete. Select this source in a new upload collection.",
                        )
                    elif actual > item.received_size:
                        try:
                            bench.workspace.set_upload_item_offset(
                                matter.matter_id, actor_id, session_id,
                                item.upload_item_id, item.received_size, actual,
                            )
                        except WorkspaceProblem:
                            current = bench.workspace.upload_item(
                                matter.matter_id, actor_id, session_id, item.upload_item_id
                            )
                            if (current.received_size, current.state) == (
                                item.received_size, item.state
                            ):
                                raise
                            # Another writer won the compare-and-set. Do not
                            # retry stale bytes against a newer ledger state.
            return bench.workspace.upload_session(matter.matter_id, actor_id, session_id)

    @app.get("/matters/{slug}/close", response_class=HTMLResponse)
    def close_matter_page(
        request: Request,
        slug: str,
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter, lifecycle = bench.matter_for_closure(
                slug,
                context.principal_id,
                administrator_override=context.is_administrator,
            )
            administrator_override = bool(
                context.is_administrator and matter.owner_id != context.principal_id
            )
            owner = bench.workspace.get_principal(matter.owner_id)
            counts = bench.workspace.matter_content_counts(matter.matter_id)
            source_count = (
                bench._matter_source_count_for_purge(matter, lifecycle)
                if lifecycle.state == "active"
                else lifecycle.source_count
            )
            active_work = bench.matter_active_work_counts(matter.matter_id)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except RuntimeError as exc:
            raise HTTPException(
                503,
                "Matter storage could not be checked safely. Ask an administrator to review it.",
            ) from exc
        return templates.TemplateResponse(
            request=request,
            name="workbench_close_matter.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "lifecycle": lifecycle,
                "source_count": max(source_count, lifecycle.source_count),
                "conversation_count": max(
                    counts["conversations"], lifecycle.conversation_count
                ),
                "message_count": max(counts["messages"], lifecycle.message_count),
                "notebook_count": counts["notebook_items"],
                "active_work": active_work,
                "administrator_override": administrator_override,
                "owner_name": owner.display_name,
                "error": error,
            },
        )

    @app.post(
        "/matters/{slug}/close",
        dependencies=[Depends(require_csrf)],
    )
    def close_matter(
        request: Request,
        slug: str,
        confirmed_name: str = Form("", max_length=140),
        acknowledge: str = Form("", max_length=8),
    ):
        context = auth_context(request)
        try:
            matter, lifecycle = bench.matter_for_closure(
                slug,
                context.principal_id,
                administrator_override=context.is_administrator,
            )
            administrator_override = bool(
                context.is_administrator and matter.owner_id != context.principal_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        audit_role = {"role": "administrator"} if administrator_override else {}
        if acknowledge != "yes":
            audit(
                request,
                "matter.purge",
                "failure",
                context=context,
                matter=matter,
                details={"state": lifecycle.state, **audit_role},
            )
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/close",
                    error="Confirm that you understand this deletion cannot be undone.",
                ),
                status_code=303,
            )
        try:
            matter, lifecycle = bench.begin_matter_purge(
                slug,
                context.principal_id,
                confirmed_name,
                administrator_override=administrator_override,
            )
        except RuntimeError:
            audit(
                request,
                "matter.purge",
                "failure",
                context=context,
                matter=matter,
                details={"state": lifecycle.state, **audit_role},
            )
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/close",
                    error="Matter storage could not be checked safely. Ask an administrator to review it.",
                ),
                status_code=303,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except WorkspaceProblem as exc:
            audit(
                request,
                "matter.purge",
                "failure",
                context=context,
                matter=matter,
                details={"state": lifecycle.state, **audit_role},
            )
            return RedirectResponse(
                _query_url(f"/matters/{slug}/close", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "matter.purge_request",
            "success",
            context=context,
            matter=matter,
            object_type="matter_purge",
            object_id=lifecycle.purge_id,
            details={"state": "purging", **audit_role},
        )
        try:
            completed = bench.execute_matter_purge(matter, lifecycle)
        except WorkspaceProblem as exc:
            failed = bench.workspace.matter_lifecycle(matter.matter_id)
            audit(
                request,
                "matter.purge",
                "failure",
                context=context,
                matter=matter,
                object_type="matter_purge",
                object_id=lifecycle.purge_id,
                details={"state": failed.state, **audit_role},
            )
            return RedirectResponse(
                _query_url(f"/matters/{slug}/close", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "matter.purge",
            "success",
            context=context,
            matter=matter,
            object_type="matter_purge",
            object_id=completed.purge_id,
            details={
                "state": "deleted",
                "count": completed.message_count,
                **audit_role,
            },
        )
        return RedirectResponse(
            _query_url(
                "/matters/new",
                notice="Matter deleted. Its processing files, conversations, and derived search data were removed.",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/members",
        dependencies=[Depends(require_csrf)],
    )
    def add_matter_member(
        request: Request,
        slug: str,
        principal_id: str = Form(..., max_length=100),
    ):
        matter = authorized_matter(
            request,
            slug,
            allow_administrator_write=True,
        )
        context = auth_context(request)
        allowed = {
            principal.principal_id for principal in identity.membership_candidates()
        }
        if principal_id not in allowed:
            audit(
                request,
                "membership.add",
                "denied",
                context=context,
                matter=matter,
            )
            raise HTTPException(403, "That identity cannot be added.")
        try:
            membership = bench.workspace.add_member(
                matter.matter_id,
                principal_id,
                context.principal_id,
                administrator_override=context.is_administrator,
            )
        except WorkspaceProblem as exc:
            audit(
                request,
                "membership.add",
                "denied",
                context=context,
                matter=matter,
                object_type="principal",
                object_id=principal_id,
            )
            raise HTTPException(403, str(exc)) from exc
        audit(
            request,
            "membership.add",
            "success",
            context=context,
            matter=matter,
            object_type="principal",
            object_id=membership.principal_id,
            details={"role": membership.role},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/setup",
                notice=f"{membership.display_name} added to the case team",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/members/{principal_id}/remove",
        dependencies=[Depends(require_csrf)],
    )
    def remove_matter_member(request: Request, slug: str, principal_id: str):
        matter = authorized_matter(
            request,
            slug,
            allow_administrator_write=True,
        )
        context = auth_context(request)
        try:
            target = bench.workspace.get_principal(principal_id)
            bench.workspace.revoke_member(
                matter.matter_id,
                principal_id,
                context.principal_id,
                administrator_override=context.is_administrator,
            )
        except KeyError as exc:
            raise HTTPException(404, "Case team member not found") from exc
        except WorkspaceProblem as exc:
            audit(
                request,
                "membership.revoke",
                "denied",
                context=context,
                matter=matter,
                object_type="principal",
                object_id=principal_id,
            )
            raise HTTPException(403, str(exc)) from exc
        audit(
            request,
            "membership.revoke",
            "success",
            context=context,
            matter=matter,
            object_type="principal",
            object_id=principal_id,
            details={"role": "member"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/setup",
                notice=f"Direct grant removed for {target.display_name}; any group grants still apply",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/registered-sources/preflight",
        dependencies=[Depends(require_csrf)],
    )
    def preflight_registered_sources(
        request: Request,
        slug: str,
        source_location_id: str = Form(..., max_length=64),
        relative_folder: str = Form("", max_length=2_048),
    ):
        try:
            matter = authorized_matter(request, slug)
            preflight = bench.source_registry.preflight(
                source_location_id, relative_folder
            )
            if not preflight.supported:
                raise SourceLocationProblem(
                    "No supported PDF, DOCX, or TXT sources were found in that folder."
                )
            plan = bench.workspace.create_ingest_plan(matter.matter_id, preflight)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except SourceLocationProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/setup", error=str(exc)), status_code=303
            )
        audit(
            request,
            "source.preflight",
            "success",
            matter=matter,
            object_type="ingest_plan",
            object_id=plan.plan_id,
            details={"count": plan.supported_count, "state": plan.state},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/setup", plan=plan.plan_id), status_code=303
        )

    @app.post(
        "/matters/{slug}/registered-sources/{plan_id}/confirm",
        dependencies=[Depends(require_csrf)],
    )
    def confirm_registered_sources(request: Request, slug: str, plan_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            plan = bench.workspace.get_ingest_plan(matter.matter_id, plan_id)
            items = bench.workspace.ingest_plan_items(matter.matter_id, plan_id)
        except KeyError as exc:
            raise HTTPException(404, "Matter or import plan not found") from exc
        if plan.state != "review":
            return PlainTextResponse("That import plan has already been resolved.", status_code=409)
        if bench.ingestion is None:
            return PlainTextResponse(
                "Background ingestion is not enabled for this workbench.", status_code=503
            )
        store = bench.source_store(matter)
        created: list[str] = []
        mapping: dict[int, str] = {}
        try:
            documents = store.register_linked_sources(
                source_location_id=plan.source_location_id,
                sources=items,
            )
            created.extend(document.document_id for document in documents)
            mapping.update(
                {item.ordinal: document.document_id for item, document in zip(items, documents)}
            )
            bench.workspace.confirm_ingest_plan(
                matter.matter_id,
                plan_id,
                mapping,
                context.principal_id,
            )
        except UploadProblem as exc:
            for document_id in created:
                store.remove(document_id)
            return RedirectResponse(
                _query_url(f"/matters/{slug}/setup", error=str(exc)), status_code=303
            )
        except Exception:
            for document_id in created:
                store.remove(document_id)
            return PlainTextResponse(
                "The import plan could not be queued. No new sources were added.",
                status_code=503,
            )
        bench.ingestion.notify()
        audit(
            request,
            "source.import_confirm",
            "success",
            matter=matter,
            object_type="ingest_plan",
            object_id=plan_id,
            details={"count": len(created), "state": "queued"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/setup",
                notice=f"{len(created)} registered source(s) queued",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/registered-sources/{plan_id}/cancel",
        dependencies=[Depends(require_csrf)],
    )
    def cancel_registered_sources(request: Request, slug: str, plan_id: str):
        try:
            matter = authorized_matter(request, slug)
            bench.workspace.cancel_ingest_plan(matter.matter_id, plan_id)
        except KeyError as exc:
            raise HTTPException(404, "Matter or import plan not found") from exc
        audit(
            request,
            "source.import_cancel",
            "success",
            matter=matter,
            object_type="ingest_plan",
            object_id=plan_id,
            details={"state": "cancelled"},
        )
        return RedirectResponse(f"/matters/{slug}/setup", status_code=303)

    def workflow_eta_projection(
        *, state: str, started_at: str | None, completed: int, total: int, unit: str
    ) -> dict[str, object]:
        if state == "queued":
            return {
                "eta_seconds": None,
                "eta_label": "Waiting for earlier saved work to finish.",
                "throughput_per_minute": None,
            }
        if state != "running":
            return {
                "eta_seconds": 0 if state == "succeeded" else None,
                "eta_label": "Complete." if state == "succeeded" else "",
                "throughput_per_minute": None,
            }
        if not started_at or completed <= 0 or total <= completed:
            return {
                "eta_seconds": None,
                "eta_label": f"Estimating after the first {unit} completes…",
                "throughput_per_minute": None,
            }
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return {
                "eta_seconds": None,
                "eta_label": "Calculating remaining time…",
                "throughput_per_minute": None,
            }
        elapsed = max((datetime.now(timezone.utc) - started).total_seconds(), 1.0)
        seconds = min(round((elapsed / completed) * (total - completed)), 2_592_000)
        rate = round((completed / elapsed) * 60, 2)
        if seconds < 60:
            duration = "less than a minute"
        elif seconds < 3_600:
            minutes = max(round(seconds / 60), 1)
            duration = f"about {minutes:,} minute{'s' if minutes != 1 else ''}"
        elif seconds < 86_400:
            hours = max(round(seconds / 3_600), 1)
            duration = f"about {hours:,} hour{'s' if hours != 1 else ''}"
        else:
            days = max(round(seconds / 86_400), 1)
            duration = f"about {days:,} day{'s' if days != 1 else ''}"
        return {
            "eta_seconds": seconds,
            "eta_label": f"Estimated remaining: {duration} · {rate:g} {unit}s/min",
            "throughput_per_minute": rate,
        }

    def research_status_projection(
        matter: MatterRecord, job: ResearchJobRecord, *, origin: str = ""
    ) -> dict[str, object]:
        progress = (
            round((job.completed_steps / job.total_steps) * 100)
            if job.total_steps else 0
        )
        eta = workflow_eta_projection(
            state=job.state,
            started_at=job.started_at,
            completed=job.completed_steps,
            total=job.total_steps,
            unit="step",
        )
        result_url = _query_url(
            f"/matters/{matter.slug}/research", job=job.job_id
        )
        if job.conversation_id and job.state == "succeeded":
            result_url = (
                f"/matters/{matter.slug}?conversation={job.conversation_id}#latest"
            )
        return {
            "job_id": job.job_id,
            "state": job.state,
            "stage": job.stage,
            "message": job.message,
            "completed_steps": job.completed_steps,
            "total_steps": job.total_steps,
            "progress_percent": progress,
            "review_budget": job.review_budget,
            "review_budget_description": job.review_budget_description,
            "candidate_count": job.candidate_count,
            "evidence_count": job.evidence_count,
            **eta,
            "terminal": job.state in {"succeeded", "failed", "cancelled"},
            "result_url": _entity_context_href(result_url, origin),
        }

    @app.get("/matters/{slug}/research", response_class=HTMLResponse,
             dependencies=[Depends(require_matter_response_lease)])
    def matter_research(
        request: Request,
        slug: str,
        job: str = Query("", max_length=80),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            readiness = matter_readiness_projection(matter)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            read_actor = matter.owner_id if administrator_override else context.principal_id
            jobs = bench.workspace.research_jobs(matter.matter_id, read_actor)
            active = (
                bench.workspace.research_job(matter.matter_id, read_actor, job)
                if job else (jobs[0] if jobs else None)
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or research run not found") from exc
        if not administrator_override:
            bench.workspace.record_matter_activity(
                matter.matter_id,
                context.principal_id,
                "analysis",
                active.job_id if active else matter.matter_id,
            )
        audit(
            request, "research.open", "success", context=context, matter=matter,
            object_type="research_job", object_id=active.job_id if active else None,
        )
        research_stale = False
        rebuild_options = []
        full_text_synthesis = bool(active and is_full_text_synthesis(active.plan, active.result))
        if active and (active.plan.get("planner_version") == PLANNER_VERSION or is_full_text_synthesis(active.plan, active.result)):
            research_stale = bench._research_checkpoint_stale(matter, active)
            if research_stale:
                try:
                    scope_available = active.source_set_id is None or bool(bench.workspace.source_set_document_ids(matter.matter_id, active.source_set_id))
                except KeyError:
                    scope_available = False
                if scope_available and active.plan.get("planner_version") == PLANNER_VERSION and active.state in {"succeeded", "failed", "cancelled"}:
                    limits = active.plan["budget"]["effective"]
                    spent = len(active.result.get("passes", [])) + int(active.result.get("discarded_passes", 0))
                    elapsed = float(active.result.get("search_elapsed_seconds", 0))
                    rebuild_options = [count for count in range(6)
                                       if spent < limits["passes"] + count <= 15
                                       and elapsed < limits["search_seconds"] + count * 180 <= 2700]
                active = replace(active, result={"budget": active.review_budget})
        full_text_input = None
        if active and is_full_text_synthesis(active.plan, active.result) and not research_stale:
            from .full_text_synthesis import validate_prepared
            try:
                full_text_input = validate_prepared(active.result)
            except (ValueError, KeyError, TypeError):
                research_stale = True
                active = replace(active, result={})
        research_synthesis = None
        research_synthesis_invalid = False
        if active and active.state == "succeeded" and not research_stale:
            try:
                # Validate raw legacy and current results before presentation,
                # as exports and Report copies do. Normalization must not make
                # inconsistent saved summaries or per-search findings visible.
                validate_research_basis(matter, active)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError, ExportProblem):
                research_synthesis_invalid = True
                active = replace(active, result={"budget": active.review_budget})
                full_text_input = None
        if active and active.result.get("hierarchical_synthesis") is not None:
            from .hierarchical_synthesis import validate_state, completion_receipt
            try:
                saved = active.result["hierarchical_synthesis"]
                ledger, findings = validate_state(saved, active.result.get("passes", []),
                    active.result.get("evidence", []), final=active.state == "succeeded")
                research_synthesis = {**saved, **completion_receipt(saved, findings, ledger)}
            except (ValueError, KeyError, TypeError, AttributeError, IndexError, ExportProblem):
                research_synthesis_invalid = True
        response = templates.TemplateResponse(
            request=request,
            name="workbench_research.html",
            context={
                **base_context(request, matter, readiness=readiness),
                "matter": matter,
                "readiness": readiness,
                "review_mode": "research",
                "research_jobs": jobs,
                "active_research": active,
                "research_stale": research_stale,
                "research_synthesis": research_synthesis,
                "research_synthesis_invalid": research_synthesis_invalid,
                "full_text_input": full_text_input,
                "full_text_synthesis": full_text_synthesis,
                "rebuild_options": rebuild_options,
                "notice": notice,
                "error": error,
            },
            headers={"Cache-Control": "no-store"},
        )
        # Rendering may overlap a membership or source change. Check the actual
        # viewer again before returning source-bearing HTML.
        authorized_matter(request, slug)
        if full_text_input:
            try:
                with bench.source_store(matter).mutation_guard():
                    bench._validate_full_text_synthesis_sources(matter, active.result)
                    authorized_matter(request, slug)
            except (WorkspaceProblem, WorkflowFailure, ValueError, KeyError, TypeError) as exc:
                raise HTTPException(409, "The synthesis sources changed. Reload the saved review.") from exc
        return transfer_matter_response_lease(request, response)

    @app.post(
        "/matters/{slug}/research",
        dependencies=[Depends(require_csrf)],
    )
    def create_research(
        request: Request,
        slug: str,
        title: str = Form(..., max_length=160),
        question: str = Form(..., max_length=2_000),
        request_key: str = Form(..., max_length=80),
        source_set: str = Form("", max_length=80),
        conversation: str = Form("", max_length=80),
    ):
        context = auth_context(request)
        origin = _source_review_return_href(slug, request.query_params.get("entity_return_to", ""))
        try:
            matter = authorized_matter(request, slug)
            readiness = bench.workspace.matter_readiness(matter.matter_id)
            if not readiness.can_query:
                raise WorkspaceProblem(
                    "A broader investigation needs at least one searchable source and no active preparation."
                )
            if not bench.generator.available:
                raise WorkspaceProblem(
                    "Automatic answering is unavailable. Search and source review still work."
                )
            active_conversation = bench.workspace.get_conversation(
                matter.matter_id, conversation or None
            )
            research, created = bench.workspace.queue_research_job(
                matter.matter_id,
                context.principal_id,
                question,
                title,
                request_key,
                source_set or None,
                conversation_id=active_conversation.conversation_id,
            )
        except (WorkspaceProblem, KeyError) as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/research", error=str(exc), entity_return_to=origin), status_code=303
            )
        if bench.research is not None:
            bench.research.notify()
        audit(
            request, "research.create", "success", context=context, matter=matter,
            object_type="research_job", object_id=research.job_id,
            details={"created": created, "state": research.state},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=active_conversation.conversation_id,
                notice="Broader investigation saved in this conversation",
                entity_return_to=origin,
            ),
            status_code=303,
        )

    @app.get("/matters/{slug}/research/{job_id}/status")
    def research_status(request: Request, slug: str, job_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            actor = context.principal_id
            job = bench.workspace.research_job(
                matter.matter_id,
                actor,
                job_id,
                administrator_override=administrator_override,
            )
        except KeyError as exc:
            raise HTTPException(404, "Research run not found") from exc
        return JSONResponse(
            research_status_projection(matter, job, origin=_source_review_return_href(slug, request.query_params.get("entity_return_to", ""))),
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/matters/{slug}/research/{job_id}/cancel",
        dependencies=[Depends(require_csrf)],
    )
    def cancel_research(request: Request, slug: str, job_id: str):
        context = auth_context(request)
        origin = _source_review_return_href(slug, request.query_params.get("entity_return_to", ""))
        try:
            matter = authorized_matter(request, slug)
            job = bench.workspace.cancel_research_job(
                matter.matter_id, context.principal_id, job_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Research run not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/research", job=job_id, error=str(exc), entity_return_to=origin),
                status_code=303,
            )
        audit(
            request, "research.cancel", "success", context=context, matter=matter,
            object_type="research_job", object_id=job.job_id,
            details={"state": job.state},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/research", job=job.job_id, entity_return_to=origin), status_code=303
        )

    @app.post(
        "/matters/{slug}/research/{job_id}/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_research(request: Request, slug: str, job_id: str, additional_passes: int = Form(0), expected_passes: int | None = Form(None)):
        context = auth_context(request)
        origin = _source_review_return_href(slug, request.query_params.get("entity_return_to", ""))
        try:
            matter = authorized_matter(request, slug)
            if not bench.workspace.matter_readiness(matter.matter_id).can_query:
                raise WorkspaceProblem("No source is searchable for this research run yet.")
            # Freeze sources before the store takes its transaction lock, so
            # locator validation and recovery admission share one boundary.
            source_store = bench.source_store(matter)
            with source_store.mutation_guard():
                current = bench.workspace.research_job(matter.matter_id, context.principal_id, job_id)
                full_text_validator = None
                if is_full_text_synthesis(current.plan, current.result):
                    from .full_text_synthesis import validate_job_input
                    validate_job_input(current)
                    bench._validate_full_text_synthesis_sources(matter, current.result)
                    service = bench._full_text_synthesis_service(matter)
                    full_text_validator = lambda value: service.repository.validate_locked(
                        matter.matter_id, context.principal_id, value.result["full_text_synthesis_input"])
                job = bench.workspace.retry_research_job(
                    matter.matter_id, context.principal_id, job_id,
                    additional_passes=additional_passes, expected_passes=expected_passes,
                    checkpoint_is_stale=lambda current: bench._research_checkpoint_stale(matter, current, source_store),
                    validate_locked=full_text_validator,
                )
        except KeyError as exc:
            raise HTTPException(404, "Research run not found") from exc
        except (WorkspaceProblem, WorkflowFailure, ValueError) as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/research", job=job_id, error=str(exc), entity_return_to=origin),
                status_code=303,
            )
        if bench.research is not None:
            bench.research.notify()
        audit(
            request, "research.retry", "success", context=context, matter=matter,
            object_type="research_job", object_id=job.job_id,
            details={"state": job.state, "count": additional_passes},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/research", job=job.job_id, entity_return_to=origin), status_code=303
        )

    @app.get(
        "/matters/{slug}/research/{job_id}/export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def export_research_run(
        request: Request,
        slug: str,
        job_id: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|markdown|json)$"),
    ):
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            actor = context.principal_id
            job = bench.workspace.research_job(
                matter.matter_id,
                actor,
                job_id,
                administrator_override=administrator_override,
            )
            if job.state != "succeeded":
                raise WorkspaceProblem("This research run is not ready to export.")
            artifact = bench.export_research_work_product(
                matter, job, format_name
            )
            if is_full_text_synthesis(job.plan, job.result):
                authorized_matter(request, slug)
                with bench.source_store(matter).mutation_guard():
                    bench._validate_full_text_synthesis_sources(matter, job.result)
                    authorized_matter(request, slug)
        except KeyError as exc:
            raise HTTPException(404, "Research run not found") from exc
        except (WorkspaceProblem, ExportProblem, WorkflowFailure, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        audit(
            request, "research.export", "success", context=context, matter=matter,
            object_type="research_job", object_id=job.job_id,
            details={"format": format_name},
        )
        return download_response(request, artifact)

    @app.post(
        "/matters/{slug}/research/{job_id}/report",
        dependencies=[Depends(require_csrf)],
    )
    def research_to_report(request: Request, slug: str, job_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            job = bench.workspace.research_job(
                matter.matter_id, context.principal_id, job_id
            )
            if job.state != "succeeded":
                raise WorkspaceProblem("Research must finish before it can be saved to a report.")
            with bench.source_store(matter).mutation_guard():
                bench._assert_current_research_ledger(matter, job)
                report = bench.workspace.create_report_from_sections(
                    matter.matter_id,
                    context.principal_id,
                    f"Investigation — {job.title}"[:200],
                    f"Research question: {job.question}"[:2_000],
                    origin_id=job.job_id,
                    sections=research_sections(job),
                )
        except KeyError as exc:
            raise HTTPException(404, "Research run not found") from exc
        except (WorkspaceProblem, ExportProblem) as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/research", job=job_id, error=str(exc)),
                status_code=303,
            )
        audit(
            request, "research.report", "success", context=context, matter=matter,
            object_type="report", object_id=report.report_id,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports", report=report.report_id,
                notice="Research brief saved to a new report",
            ),
            status_code=303,
        )

    def review_status_projection(
        matter: MatterRecord, run: ReviewRunRecord
    ) -> dict[str, object]:
        progress = (
            round((run.reviewed_count / run.snapshot_count) * 100)
            if run.snapshot_count else 0
        )
        eta = workflow_eta_projection(
            state=run.state,
            started_at=run.started_at,
            completed=run.reviewed_count,
            total=run.snapshot_count,
            unit="source",
        )
        return {
            "run_id": run.run_id,
            "state": run.state,
            "stage": run.stage,
            "message": run.message,
            "reviewed_count": run.reviewed_count,
            "snapshot_count": run.snapshot_count,
            "progress_percent": progress,
            "included_count": run.included_count,
            "excluded_count": run.excluded_count,
            "attention_count": run.attention_count,
            **eta,
            "terminal": run.state in {"succeeded", "failed", "cancelled"},
            "result_url": _query_url(
                f"/matters/{matter.slug}/full-review",
                criterion=run.criterion_id,
                run=run.run_id,
            ),
        }

    def decision_reviewer_name(decision) -> str:
        if decision is None or not decision.reviewed_by:
            return ""
        try:
            return bench.workspace.get_principal(decision.reviewed_by).display_name
        except KeyError:
            return "Case-team member"

    def hydrate_selected_review_decision(matter, run, decision):
        if decision is None:
            return decision, ""
        try:
            return bench._hydrate_full_text_export_decisions(matter, run, (decision,))[0], ""
        except ExportProblem:
            return replace(decision, citations=()), (
                "Supporting passages are unavailable or changed. "
                "Open the source and rerun the check before relying on this decision."
            )

    def review_decision_recovery(
        request: Request, slug: str, run_id: str, document_id: str, *,
        error: str, draft: Mapping[str, object], status_code: int = 409,
    ):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        try:
            run = bench.workspace.review_run(matter.matter_id, context.principal_id, run_id)
            decision = bench.workspace.review_decision(matter.matter_id, context.principal_id, run_id, document_id)
        except KeyError:
            run, decision = None, None
            error = "This source review is no longer available. Copy your unsaved note before leaving."
        matter = authorized_matter(request, slug)
        decision, citation_error = hydrate_selected_review_decision(matter, run, decision)
        return templates.TemplateResponse(
            request=request, name="workbench_review_decision_recovery.html",
            context={
                **base_context(request, matter), "matter": matter, "active_run": run,
                "selected_decision": decision, "draft": draft, "error": error,
                "reviewer_name": decision_reviewer_name(decision),
                "citation_error": citation_error,
                "expected_updated_at": decision.updated_at if decision else "",
                "can_save": decision is not None and decision.machine_decision != "pending",
            }, status_code=status_code, headers={"Cache-Control": "no-store"},
        )

    @app.get("/matters/{slug}/full-review", response_class=HTMLResponse)
    def matter_full_review(
        request: Request,
        slug: str,
        criterion: str = Query("", max_length=80),
        run: str = Query("", max_length=80),
        decision: str = Query("", max_length=24),
        validation: bool = Query(False),
        page: int = Query(1, ge=1, le=100_000),
        source: str = Query("", max_length=100),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            readiness = matter_readiness_projection(matter)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            read_actor = matter.owner_id if administrator_override else context.principal_id
            criteria = bench.workspace.review_criteria(matter.matter_id, read_actor)
            active_criterion = (
                bench.workspace.review_criterion(matter.matter_id, criterion)
                if criterion else (criteria[0] if criteria else None)
            )
            active_version = (
                bench.workspace.review_criterion_version(
                    matter.matter_id, active_criterion.current_version_id
                ) if active_criterion else None
            )
            runs = bench.workspace.review_runs(matter.matter_id, read_actor)
            if active_criterion:
                runs = tuple(item for item in runs if item.criterion_id == active_criterion.criterion_id)
            active_run = (
                bench.workspace.review_run(matter.matter_id, read_actor, run)
                if run else (runs[0] if runs else None)
            )
            if active_run and active_criterion and active_run.criterion_id != active_criterion.criterion_id:
                raise KeyError(run)
            decision_page = (
                bench.workspace.review_decisions(
                    matter.matter_id,
                    read_actor,
                    active_run.run_id,
                    page=page,
                    page_size=50,
                    decision_filter=decision,
                    validation_only=validation,
                ) if active_run else None
            )
            selected_decision = (
                bench.workspace.review_decision(
                    matter.matter_id, read_actor, active_run.run_id, source
                ) if active_run and source else None
            )
            selected_decision, citation_error = hydrate_selected_review_decision(
                matter, active_run, selected_decision
            )
            metrics = (
                bench.workspace.review_validation_metrics(
                    matter.matter_id, read_actor, active_run.run_id
                ) if active_run else {}
            )
            source_sets = bench.workspace.source_sets(matter.matter_id)
        except (KeyError, WorkspaceProblem) as exc:
            raise HTTPException(404, "Matter, criterion, review run, or source decision not found") from exc
        if not administrator_override:
            bench.workspace.record_matter_activity(
                matter.matter_id,
                context.principal_id,
                "analysis",
                active_run.run_id if active_run else (
                    active_criterion.criterion_id if active_criterion else matter.matter_id
                ),
            )
        audit(
            request, "full_review.open", "success", context=context, matter=matter,
            object_type="review_run", object_id=active_run.run_id if active_run else None,
        )
        synthesis_snapshot, synthesis_unavailable = "", ""
        if (active_run and not administrator_override
                and active_run.state in {"succeeded", "failed", "cancelled"}
                and FullTextReviewLedger(bench.workspace).enabled(active_run.run_id)):
            try:
                captured = bench._full_text_synthesis_service(matter).repository.capture(
                    matter.matter_id, context.principal_id, active_run.run_id)
                synthesis_snapshot = captured["receipt"]["snapshot_digest"]
            except (WorkspaceProblem, ValueError, KeyError) as exc:
                synthesis_unavailable = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="workbench_full_review.html",
            context={
                **base_context(request, matter, readiness=readiness),
                "matter": matter,
                "readiness": readiness,
                "review_mode": "full",
                "criteria": criteria,
                "active_criterion": active_criterion,
                "active_version": active_version,
                "review_runs": runs,
                "text_review_runs": {item.run_id for item in (*runs, *((active_run,) if active_run else ())) if FullTextReviewLedger(bench.workspace).enabled(item.run_id)},
                "active_text_coverage": FullTextReviewLedger(bench.workspace).coverage(
                    matter.matter_id, read_actor, active_run.run_id
                ) if active_run else None,
                "active_run": active_run,
                "synthesis_snapshot": synthesis_snapshot,
                "synthesis_unavailable": synthesis_unavailable,
                "synthesis_request_key": "research-request-" + uuid.uuid4().hex,
                "synthesis_available": bench.generator.available,
                "decision_page": decision_page,
                "selected_decision": selected_decision,
                "reviewer_name": decision_reviewer_name(selected_decision),
                "citation_error": citation_error,
                "validation": metrics,
                "source_sets": source_sets,
                "notice": notice,
                "error": error,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/matters/{slug}/full-review/{run_id}/synthesize",
              dependencies=[Depends(require_csrf)])
    def synthesize_full_text_review(request: Request, slug: str, run_id: str,
            request_key: str = Form(..., max_length=80),
            expected_snapshot: str = Form(..., min_length=64, max_length=64)):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        try:
            job, created = bench.queue_full_text_synthesis(matter, context.principal_id,
                run_id, request_key, expected_snapshot=expected_snapshot)
        except (WorkspaceProblem, WorkflowFailure, ValueError, KeyError) as exc:
            return RedirectResponse(_query_url(f"/matters/{slug}/full-review",
                run=run_id, error=str(exc)), status_code=303)
        if bench.research is not None:
            bench.research.notify()
        audit(request, "full_text.synthesis.create", "success", context=context, matter=matter,
            object_type="research_job", object_id=job.job_id, details={"created": created})
        return RedirectResponse(_query_url(f"/matters/{slug}/research", job=job.job_id), status_code=303)

    @app.post(
        "/matters/{slug}/full-review/criteria",
        dependencies=[Depends(require_csrf)],
    )
    def create_full_review_criterion(
        request: Request,
        slug: str,
        title: str = Form(..., max_length=160),
        instructions: str = Form(..., max_length=8_000),
        include_guidance: str = Form("", max_length=4_000),
        exclude_guidance: str = Form("", max_length=4_000),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            criterion, version = bench.workspace.create_review_criterion(
                matter.matter_id,
                context.principal_id,
                title=title,
                instructions=instructions,
                include_guidance=include_guidance,
                exclude_guidance=exclude_guidance,
            )
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/full-review", error=str(exc)), status_code=303
            )
        audit(
            request, "full_review.criterion_create", "success", context=context,
            matter=matter, object_type="review_criterion", object_id=criterion.criterion_id,
            details={"count": version.version_number},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/full-review", criterion=criterion.criterion_id,
                notice="Criterion v1 saved",
            ), status_code=303
        )

    @app.post(
        "/matters/{slug}/full-review/criteria/{criterion_id}/versions",
        dependencies=[Depends(require_csrf)],
    )
    def version_full_review_criterion(
        request: Request,
        slug: str,
        criterion_id: str,
        title: str = Form(..., max_length=160),
        instructions: str = Form(..., max_length=8_000),
        include_guidance: str = Form("", max_length=4_000),
        exclude_guidance: str = Form("", max_length=4_000),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            version = bench.workspace.add_review_criterion_version(
                matter.matter_id,
                context.principal_id,
                criterion_id,
                title=title,
                instructions=instructions,
                include_guidance=include_guidance,
                exclude_guidance=exclude_guidance,
            )
        except KeyError as exc:
            raise HTTPException(404, "Criterion not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/full-review", criterion=criterion_id, error=str(exc)
                ), status_code=303
            )
        audit(
            request, "full_review.criterion_version", "success", context=context,
            matter=matter, object_type="review_criterion", object_id=criterion_id,
            details={"count": version.version_number},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/full-review", criterion=criterion_id,
                notice=f"Criterion v{version.version_number} saved",
            ), status_code=303
        )

    @app.post(
        "/matters/{slug}/full-review/runs",
        dependencies=[Depends(require_csrf)],
    )
    def create_full_review_run(
        request: Request,
        slug: str,
        criterion_version_id: str = Form(..., max_length=80),
        run_kind: str = Form(..., max_length=16),
        source_set: str = Form("", max_length=80),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            readiness = bench.workspace.matter_readiness(matter.matter_id)
            if not readiness.can_query:
                raise WorkspaceProblem(
                    "Checking every source needs at least one searchable source and no active preparation."
                )
            if not bench.generator.available:
                raise WorkspaceProblem("Automatic source checking is unavailable.")
            run = bench.workspace.queue_review_run(
                matter.matter_id,
                context.principal_id,
                criterion_version_id,
                run_kind="full" if run_kind == "full_text" else run_kind,
                review_mode="full_text" if run_kind == "full_text" else "selected_passages",
                source_set_id=source_set or None,
            )
        except KeyError as exc:
            raise HTTPException(404, "Criterion version not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/full-review", error=str(exc)), status_code=303
            )
        if bench.full_review is not None:
            bench.full_review.notify()
        audit(
            request, "full_review.create", "success", context=context, matter=matter,
            object_type="review_run", object_id=run.run_id,
            details={"state": run.state, "count": run.snapshot_count},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/full-review",
                criterion=run.criterion_id,
                run=run.run_id,
            ), status_code=303
        )

    @app.get("/matters/{slug}/full-review/{run_id}/status")
    def full_review_status(request: Request, slug: str, run_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            actor = context.principal_id
            run = bench.workspace.review_run(
                matter.matter_id,
                actor,
                run_id,
                administrator_override=administrator_override,
            )
            text_coverage = FullTextReviewLedger(bench.workspace).coverage(
                matter.matter_id, actor, run.run_id, administrator_override=administrator_override)
        except KeyError as exc:
            raise HTTPException(404, "Review run not found") from exc
        payload = review_status_projection(matter, run)
        payload["text_review"] = text_coverage
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/matters/{slug}/full-review/{run_id}/text")
    def full_text_ledger(request: Request, slug: str, run_id: str, after: int = Query(0, ge=0)):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            actor = context.principal_id
            override = getattr(request.state, "administrator_matter_override", None) == matter.matter_id
            run = bench.workspace.review_run(matter.matter_id, actor, run_id, administrator_override=override)
            ledger = FullTextReviewLedger(bench.workspace)
            coverage = ledger.coverage(matter.matter_id, actor, run_id, administrator_override=override)
            if coverage is None:
                raise KeyError(run_id)
            rows = ledger.rows(matter.matter_id, actor, run_id, after=after, limit=100, administrator_override=override)
            for row in rows:
                from .full_text_review import read_locator
                try:
                    row["citation"] = read_locator(row.pop("citation_json"))
                except WorkspaceProblem as exc:
                    row["citation"] = {}
                    row["locator_error"] = str(exc)
            sources = ledger.extraction_rows(matter.matter_id, actor, run_id, administrator_override=override)
        except KeyError as exc:
            raise HTTPException(404, "Text review not found") from exc
        audit(request, "full_review.text_open", "success", context=context, matter=matter,
            object_type="review_run", object_id=run.run_id)
        return templates.TemplateResponse(request=request, name="workbench_text_review.html", context={
            **base_context(request, matter), "matter": matter, "run": run, "coverage": coverage,
            "rows": rows, "sources": sources, "after": after,
            "can_delete_text_review": not override and run.actor_id == actor,
            "can_control_text_review": not override and run.actor_id == actor,
            "next_cursor": rows[-1]["cursor"] if len(rows) == 100 else None,
        }, headers={"Cache-Control": "no-store"})

    @app.post("/matters/{slug}/full-review/{run_id}/text/delete", dependencies=[Depends(require_csrf)])
    def delete_full_text_ledger(request: Request, slug: str, run_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            run = FullTextReviewLedger(bench.workspace).delete(matter.matter_id, context.principal_id, run_id)
        except KeyError as exc:
            raise HTTPException(404, "Text review not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(_query_url(f"/matters/{slug}/full-review", run=run_id, error=str(exc)), status_code=303)
        audit(request, "full_review.text_delete", "success", context=context, matter=matter,
            object_type="review_run", object_id=run_id)
        return RedirectResponse(_query_url(f"/matters/{slug}/full-review", criterion=run.criterion_id), status_code=303)

    @app.get("/matters/{slug}/full-review/{run_id}/text/export", dependencies=[Depends(require_matter_response_lease)])
    def export_full_text_ledger(request: Request, slug: str, run_id: str, format_name: str = Query("json", alias="format", pattern="^(json|csv)$")):
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            override = getattr(request.state, "administrator_matter_override", None) == matter.matter_id
            stream = iter_text_export(bench.workspace, matter.matter_id, context.principal_id, run_id,
                format_name, administrator_override=override)
        except KeyError as exc:
            raise HTTPException(404, "Text review not found") from exc
        try:
            audit(request, "full_review.text_export", "success", context=context, matter=matter,
                object_type="review_run", object_id=run_id, details={"format": format_name})
            response = TextLedgerStreamingResponse(stream,
                media_type="application/json" if format_name == "json" else "text/csv",
                release_lease=lambda: bench.finish_matter_response(matter.matter_id, request.state.matter_response_lease["lease_id"]),
                headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="full-text-review.{format_name}"'})
            return transfer_matter_response_lease(request, response)
        except BaseException:
            stream.close()
            raise

    @app.post(
        "/matters/{slug}/full-review/{run_id}/cancel",
        dependencies=[Depends(require_csrf)],
    )
    def cancel_full_review(request: Request, slug: str, run_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            run = bench.workspace.cancel_review_run(
                matter.matter_id, context.principal_id, run_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Review run not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/full-review", run=run_id, error=str(exc)),
                status_code=303,
            )
        audit(
            request, "full_review.cancel", "success", context=context, matter=matter,
            object_type="review_run", object_id=run.run_id,
            details={"state": run.state},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/full-review", criterion=run.criterion_id, run=run.run_id
            ), status_code=303
        )

    @app.post(
        "/matters/{slug}/full-review/{run_id}/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_full_review(request: Request, slug: str, run_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            run = bench.workspace.retry_review_run(
                matter.matter_id, context.principal_id, run_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Review run not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/full-review", run=run_id, error=str(exc)),
                status_code=303,
            )
        if bench.full_review is not None:
            bench.full_review.notify()
        audit(
            request, "full_review.retry", "success", context=context, matter=matter,
            object_type="review_run", object_id=run.run_id,
            details={"state": run.state},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/full-review", criterion=run.criterion_id, run=run.run_id
            ), status_code=303
        )

    @app.post(
        "/matters/{slug}/full-review/{run_id}/decisions/{document_id}",
        dependencies=[Depends(require_csrf)],
    )
    def adjudicate_full_review(
        request: Request,
        slug: str,
        run_id: str,
        document_id: str,
        human_decision: str = Form(..., max_length=16),
        note: str = Form("", max_length=2_000),
        expected_updated_at: str = Form("", max_length=64),
    ):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        draft = dict(human_decision=human_decision, human_note=note)
        try:
            run = bench.workspace.review_run(
                matter.matter_id, context.principal_id, run_id
            )
            decision = bench.workspace.adjudicate_review_decision(
                matter.matter_id,
                context.principal_id,
                run_id,
                document_id,
                human_decision=human_decision,
                expected_updated_at=expected_updated_at,
                note=note,
            )
        except KeyError:
            return review_decision_recovery(request, slug, run_id, document_id,
                                            error="Source review unavailable.", draft=draft)
        except WorkspaceProblem as exc:
            return review_decision_recovery(request, slug, run_id, document_id,
                error=str(exc), draft=draft,
                status_code=409 if isinstance(exc, ReviewDecisionConflict) else 400)
        audit(
            request, "full_review.adjudicate", "success", context=context, matter=matter,
            object_type="review_decision", object_id=decision.document_id,
            details={"state": "reviewed"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/full-review",
                criterion=run.criterion_id,
                run=run.run_id,
                source=decision.document_id,
                notice="Human validation saved",
            ), status_code=303
        )

    @app.get(
        "/matters/{slug}/full-review/{run_id}/export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def export_full_review_run(
        request: Request,
        slug: str,
        run_id: str,
        format_name: str = Query(
            "csv", alias="format", pattern="^(csv|docx|markdown|json)$"
        ),
    ):
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            actor = context.principal_id
            run = bench.workspace.review_run(
                matter.matter_id,
                actor,
                run_id,
                administrator_override=administrator_override,
            )
            criterion = bench.workspace.review_criterion(matter.matter_id, run.criterion_id)
            version = bench.workspace.review_criterion_version(
                matter.matter_id, run.criterion_version_id
            )
            decisions = bench.workspace.review_decisions_for_export(
                matter.matter_id,
                actor,
                run.run_id,
                administrator_override=administrator_override,
            )
            metrics = bench.workspace.review_validation_metrics(
                matter.matter_id,
                actor,
                run.run_id,
                administrator_override=administrator_override,
            )
            with bench.source_store(matter).mutation_guard():
                decisions = bench._hydrate_full_text_export_decisions(matter, run, decisions)
                artifact = export_full_review(
                    matter, criterion, version, run, decisions, metrics, format_name
                )
        except KeyError as exc:
            raise HTTPException(404, "Review run not found") from exc
        except ExportProblem as exc:
            raise HTTPException(409, str(exc)) from exc
        audit(
            request, "full_review.export", "success", context=context, matter=matter,
            object_type="review_run", object_id=run.run_id,
            details={"format": format_name, "count": len(decisions)},
        )
        return download_response(request, artifact)

    @app.post(
        "/matters/{slug}/full-review/{run_id}/report",
        dependencies=[Depends(require_csrf)],
    )
    def full_review_to_report(request: Request, slug: str, run_id: str):
        context = auth_context(request)
        criterion_id = ""
        try:
            matter = authorized_matter(request, slug)
            with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                run = bench.workspace.review_run(
                    matter.matter_id, context.principal_id, run_id
                )
                criterion_id = run.criterion_id
                if run.state != "succeeded":
                    raise WorkspaceProblem("Wait for this check to finish successfully before saving it to a Report.")
                criterion = bench.workspace.review_criterion(matter.matter_id, criterion_id)
                version = bench.workspace.review_criterion_version(
                    matter.matter_id, run.criterion_version_id
                )
                full_text = FullTextReviewLedger(bench.workspace).enabled(run.run_id)
                decisions = bench.workspace.iter_review_decisions_for_report(
                    matter.matter_id, context.principal_id, run.run_id
                )
                def verified_decisions():
                    for decision in decisions:
                        if full_text:
                            # Validate the complete frozen population, including
                            # uncited decisions and details beyond the Report cap.
                            bench._resolve_full_text_report_support(matter, run, decision, citations=())
                        yield decision
                def reviewer_name(reviewer_id):
                    try:
                        return bench.workspace.get_principal(reviewer_id).display_name
                    except KeyError:
                        return "Not recorded"
                def citation_resolver(decision, selected_locators):
                    return bench._resolve_full_text_report_support(matter, run, decision, citations=selected_locators)
                sections = review_sections(
                    run, verified_decisions(), criterion_title=criterion.title,
                    criterion_version=version.version_number, instructions=version.instructions,
                    ledger_path=_query_url(f"/matters/{matter.slug}/full-review", criterion=run.criterion_id, run=run_id),
                    reviewer_name=reviewer_name,
                    review_mode="full_text" if full_text else "selected_passages",
                    citation_resolver=citation_resolver if full_text else None,
                )
                bench._assert_current_report_section_citations(matter, sections)
                report = bench.workspace.create_report_from_sections(
                    matter.matter_id,
                    context.principal_id,
                    f"Every-source check — {criterion.title}"[:200],
                    f"Criterion version {version.version_number}; frozen population {run.snapshot_count:,} sources.",
                    origin_id=run.run_id,
                    sections=sections,
                )
        except KeyError as exc:
            raise HTTPException(404, "Review run not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/full-review", criterion=criterion_id, run=run_id, error=str(exc)),
                status_code=303,
            )
        audit(
            request, "full_review.report", "success", context=context, matter=matter,
            object_type="report", object_id=report.report_id,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports", report=report.report_id,
                notice="Every-source check summary saved to a new report",
            ), status_code=303
        )

    exact_search_active: set[str] = set()
    exact_search_admission_lock = threading.Lock()

    def exact_search_matter(request: Request, slug: str) -> MatterRecord:
        # Membership storage can block; use FastAPI's short synchronous
        # dependency dispatch rather than blocking the async event loop.
        return authorized_matter(request, slug)

    def prepare_exact_search(
        request: Request, slug: str,
        q: str = Query("", max_length=512),
        words: str = Query("", max_length=512),
        phrase: str = Query("", max_length=512),
        exclude: str = Query("", max_length=512),
        proximity_first: str = Query("", max_length=256),
        proximity_second: str = Query("", max_length=256),
        proximity_gap: str = Query("5", max_length=3),
        proximity_order: str = Query("either", max_length=10),
        search: bool = Query(False),
        advanced: bool = Query(False),
        source_set: str = Query("", max_length=80),
        collection: str = Query("", max_length=80),
        page: int = Query(1, ge=1),
        page_size: int = Query(25),
        fingerprint: str = Query("", max_length=64),
        matter: MatterRecord = Depends(exact_search_matter),
    ):
        # Resolve typed controls and scope before deciding whether this request
        # needs expensive scan admission. Storage reads stay off the event loop.
        form = {name: value for name, value in locals().items() if name not in {"request", "matter", "slug"}}
        effective_query = None
        error, status = "", 200
        using_expression = advanced or bool(q.strip() and not search)
        try:
            if source_set:
                bench.workspace.source_set(matter.matter_id, source_set)
            if collection:
                bench.workspace.source_collection(matter.matter_id, collection)
            if page_size not in {25, 50, 100}:
                raise ValueError("Choose a positive page and a page size of 25, 50, or 100.")
            if search or any(value.strip() for value in (q, words, phrase, exclude, proximity_first, proximity_second)):
                query = q if using_expression else build_search_query(words, phrase, exclude,
                    proximity_first=proximity_first, proximity_second=proximity_second,
                    proximity_gap=proximity_gap, proximity_order=proximity_order)
                parse_query(query)
                effective_query = query
        except KeyError as exc:
            raise HTTPException(404, "Search scope not found") from exc
        except QuerySyntaxError as exc:
            error = str(exc) if using_expression else "Use fewer words or a shorter phrase, then search again."
            status = 400
        except ValueError as exc:
            error, status = str(exc), 400
        return matter, form, effective_query, using_expression, error, status

    async def exact_search_admission(prepared=Depends(prepare_exact_search)):
        matter, _, query, _, _, _ = prepared
        if query is None:
            yield prepared
            return
        # Only validated scans claim a slot, before their worker is dispatched.
        with exact_search_admission_lock:
            if matter.matter_id in exact_search_active or len(exact_search_active) >= 4:
                raise HTTPException(429, "Search is busy. Please try again shortly.",
                                    headers={"Retry-After": "1"})
            exact_search_active.add(matter.matter_id)
        try:
            yield prepared
        finally:
            with exact_search_admission_lock:
                exact_search_active.discard(matter.matter_id)

    @app.get("/matters/{slug}/exact-search", response_class=HTMLResponse)
    def exact_search_page(request: Request, slug: str, prepared=Depends(exact_search_admission)):
        matter, form, effective_query, using_expression, action_error, status = prepared
        q = form["q"]
        words = form["words"]
        phrase = form["phrase"]
        exclude = form["exclude"]
        proximity_first = form["proximity_first"]
        proximity_second = form["proximity_second"]
        proximity_gap = form["proximity_gap"]
        proximity_order = form["proximity_order"]
        source_set = form["source_set"]
        collection = form["collection"]
        page = form["page"]
        page_size = form["page_size"]
        fingerprint = form["fingerprint"]
        results = None
        try:
            if effective_query is not None:
                results = bench.exact_search(matter, effective_query, source_set_id=source_set,
                    collection_id=collection, page=page, page_size=page_size,
                    expected_fingerprint=fingerprint)
        except KeyError as exc:
            raise HTTPException(404, "Search scope not found") from exc
        except ExactSearchChanged:
            action_error, status = "Your sources changed. Search again to refresh these results.", 409
        except ExactSearchUnavailable:
            action_error, status = "We couldn't finish this search. Choose a smaller collection or saved set, then try again. If it keeps happening, check source preparation.", 503
        except ValueError as exc:
            action_error, status = str(exc), 400
        links = {}
        if results:
            for label, number in (("current", results.page), ("previous", results.page - 1), ("next", results.page + 1)):
                if 1 <= number <= results.pages:
                    links[label] = _query_url(f"/matters/{slug}/exact-search", q=q if using_expression else "",
                        words=words, phrase=phrase, exclude=exclude,
                        proximity_first=proximity_first, proximity_second=proximity_second,
                        proximity_gap=proximity_gap, proximity_order=proximity_order,
                        source_set=source_set, collection=collection, page=number,
                        page_size=page_size, fingerprint=results.fingerprint)
        audit(request, "search.exact", "failure" if action_error else "success",
              context=auth_context(request), matter=matter,
              details={"result_count": results.total} if results else {})
        return templates.TemplateResponse(request=request, name="workbench_exact_search.html",
            status_code=status, context={
                **base_context(request, matter), "matter": matter, "query": q if using_expression else "",
                "words": words, "phrase": phrase, "exclude": exclude,
                "proximity_first": proximity_first, "proximity_second": proximity_second,
                "proximity_gap": proximity_gap, "proximity_order": proximity_order,
                "show_assistant_dock": False,
                "clear_refinements_url": _query_url(f"/matters/{slug}/exact-search", words=words,
                    q=q if using_expression else ""),
                "source_sets": bench.workspace.source_sets(matter.matter_id),
                "collections": bench.workspace.source_collections(matter.matter_id),
                "selected_source_set": source_set, "selected_collection": collection,
                "results": results, "links": links, "error": action_error,
                "extraction_coverage_notice": str(_source_coverage(
                    bench.workspace.matter_readiness(matter.matter_id))["notice"]),
                "previews": {item.document_id: item.previews for item in results.items} if results else {},
            })

    @app.get("/matters/{slug}", response_class=HTMLResponse)
    def workspace(
        request: Request,
        slug: str,
        q: str = Query("", max_length=MAX_SEARCH_CHARS),
        mode: str = Query("ask", pattern="^(ask|search|sources)$"),
        support: str = Query("", max_length=64),
        play: bool = Query(False),
        conversation: str = Query("", max_length=80),
        source_set: str = Query("", max_length=80),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            readiness = matter_readiness_projection(matter)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            active_conversation = bench.workspace.get_conversation(
                matter.matter_id, conversation or None
            )
            conversation_summaries = bench.workspace.conversation_summaries(
                matter.matter_id
            )
            active_conversation_summary = next(
                item
                for item in conversation_summaries
                if item.conversation_id == active_conversation.conversation_id
            )
            active_answer = (
                None
                if administrator_override
                else bench.workspace.latest_answer_job(
                    matter.matter_id,
                    active_conversation.conversation_id,
                    context.principal_id,
                )
            )
            source_sets = bench.workspace.source_sets(matter.matter_id)
            reports = bench.workspace.reports(
                matter.matter_id,
                matter.owner_id if administrator_override else context.principal_id,
            )
            conversation_research = next(
                (
                    item
                    for item in bench.workspace.research_jobs(
                        matter.matter_id,
                        matter.owner_id
                        if administrator_override
                        else context.principal_id,
                    )
                    if item.conversation_id
                    == active_conversation.conversation_id
                    and item.state in {"queued", "running"}
                ),
                None,
            )
            selected_source_set_id = ""
            if source_set:
                selected_source_set_id = bench.workspace.source_set(
                    matter.matter_id, source_set
                ).source_set_id
        except KeyError as exc:
            raise HTTPException(404, "Matter, conversation, or source set not found") from exc
        if not administrator_override:
            bench.workspace.record_matter_activity(
                matter.matter_id,
                context.principal_id,
                "conversation",
                active_conversation.conversation_id,
            )
        action_error = error
        search_results: tuple[WorkbenchCitation, ...] = ()
        if q.strip():
            if not readiness["can_query"]:
                mode = "search"
                action_error = (
                    f"{readiness['headline']}. {readiness['guidance']}"
                )
                audit(
                    request,
                    "search.execute",
                    "failure",
                    context=context,
                    matter=matter,
                    details={"state": str(readiness["state"])},
                )
            else:
                try:
                    search_results = bench.search(matter, q)
                    mode = "search"
                    audit(
                        request,
                        "search.execute",
                        "success",
                        context=context,
                        matter=matter,
                        details={"result_count": len(search_results)},
                    )
                except (RetrievalUnavailable, WorkspaceProblem):
                    action_error = "Search could not run. Your sources are still saved; try again in a moment."
                    audit(
                        request,
                        "search.execute",
                        "failure",
                        context=context,
                        matter=matter,
                    )
        support_view = None
        if support:
            try:
                support_view = bench.support(
                    matter,
                    support,
                    conversation_id=active_conversation.conversation_id,
                    autoplay=play,
                )
                audit(
                    request,
                    "support.open",
                    "success",
                    context=context,
                    matter=matter,
                )
            except KeyError as exc:
                raise HTTPException(404, "Source support is unavailable") from exc
        audit(
            request,
            "matter.open",
            "success",
            context=context,
            matter=matter,
            object_type="matter",
            object_id=matter.matter_id,
        )
        source_overview = bench.source_library(matter, view="overview")
        media_activity = media_activity_projection(matter)
        from .matter_context import MatterContextService
        saved_context, _ = MatterContextService(bench.assertion_service(matter)).inspect(
            matter.matter_id, matter.owner_id if administrator_override else context.principal_id)
        notebook_context_page = bench.workspace.notebook_page(
            matter.matter_id,
            matter.owner_id if administrator_override else context.principal_id,
            status="all",
            page_size=50,
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_workspace.html",
            context={
                **base_context(request, matter, readiness=readiness),
                "matter": matter,
                "review_mode": "quick",
                "sources": source_overview.items[:4],
                "source_total": source_overview.stats["total"],
                "searchable_source_count": int(readiness["searchable_count"]),
                "readiness": readiness,
                "media_activity": media_activity,
                "source_sets": source_sets,
                "reports": reports,
                "selected_source_set_id": selected_source_set_id,
                "conversations": conversation_summaries,
                "pinned_conversations": tuple(
                    item
                    for item in conversation_summaries
                    if item.state == "active" and item.is_pinned
                ),
                "recent_conversations": tuple(
                    item
                    for item in conversation_summaries
                    if item.state == "active" and not item.is_pinned
                ),
                "archived_conversations": tuple(
                    item for item in conversation_summaries if item.state == "archived"
                ),
                "conversation": active_conversation,
                "conversation_summary": active_conversation_summary,
                "messages": bench.workspace.messages(
                    matter.matter_id, active_conversation.conversation_id
                ),
                "active_answer": (
                    answer_projection(request, matter, active_answer)
                    if active_answer is not None
                    else None
                ),
                "active_research": conversation_research,
                "answer_request_key": f"answer-request-{uuid.uuid4().hex}",
                "query": q,
                "mode": mode,
                "search_results": search_results,
                "support": support_view,
                "saved_context": saved_context,
                "notebook_context_items": tuple(
                    item
                    for item in notebook_context_page.items
                    if item.status != "dismissed"
                ),
                "confirmed_notebook_count": notebook_context_page.counts["confirmed"],
                "notice": notice,
                "error": action_error,
            },
        )

    @app.get("/matters/{slug}/media-activity")
    def matter_media_activity(request: Request, slug: str):
        try:
            matter = authorized_matter(request, slug)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        return JSONResponse(
            media_activity_projection(matter),
            headers={"Cache-Control": "no-store"},
        )

    async def read_intake_json(request: Request) -> dict:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_UPLOAD_PREFLIGHT_REQUEST_BYTES:
                raise HTTPException(413, "The selected-file receipt batch is too large.")
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(400, "The selected-file receipt could not be read.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "The selected-file receipt request is invalid.")
        return payload

    def intake_response(matter: MatterRecord, receipt: dict, *, status_code: int = 200):
        return JSONResponse({**receipt, "receipt_url": f"/matters/{matter.slug}/intake/{receipt['receipt_id']}"},
            status_code=status_code, headers={"Cache-Control": "no-store"})

    @app.post("/matters/{slug}/intake-receipts", dependencies=[Depends(require_csrf_header)])
    async def record_intake_selection(request: Request, slug: str):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        payload = await read_intake_json(request)
        try:
            receipt = await run_in_threadpool(IntakeReceipts(bench.workspace).create, matter.matter_id, context.principal_id,
                selection_key=payload.get("selection_key"), selection_fingerprint=payload.get("selection_fingerprint"),
                selected_count=payload.get("selected_count"), eligible_indexes=payload.get("eligible_indexes"),
                collection_name=payload.get("collection_name") or "Uploaded sources")
        except KeyError as exc:
            raise HTTPException(404, "Selection receipt not found") from exc
        except (WorkspaceProblem, TypeError) as exc:
            return JSONResponse({"message": str(exc) if isinstance(exc, WorkspaceProblem) else "The selection receipt is invalid."},
                status_code=409, headers={"Cache-Control": "no-store"})
        audit(request, "source.intake_receipt_create", "success", context=context, matter=matter,
            object_type="matter", object_id=matter.matter_id, details={"count": receipt["selected_count"]})
        return intake_response(matter, receipt, status_code=201)

    @app.post("/matters/{slug}/intake-receipts/{receipt_id}/items", dependencies=[Depends(require_csrf_header)])
    async def record_intake_items(request: Request, slug: str, receipt_id: str):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        payload = await read_intake_json(request)
        capability = await run_in_threadpool(scanner_status, bench.malware_scanner)
        try:
            receipt = await run_in_threadpool(IntakeReceipts(bench.workspace).append, matter.matter_id, context.principal_id, receipt_id,
                start=payload.get("start"), files=payload.get("files"), reviewed_states=payload.get("reviewed_states"),
                document_limit=bench.storage_policy.document_file_bytes, media_limit=bench.storage_policy.media_file_bytes,
                malware_scan_mode=bench.malware_scan_mode, scanner_ready=capability.ready)
        except KeyError as exc:
            raise HTTPException(404, "Selection receipt not found") from exc
        except WorkspaceProblem as exc:
            return JSONResponse({"message": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"})
        return intake_response(matter, receipt)

    @app.post("/matters/{slug}/intake-receipts/{receipt_id}/seal", dependencies=[Depends(require_csrf_header)])
    def finish_intake_receipt(request: Request, slug: str, receipt_id: str):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        try:
            receipt = IntakeReceipts(bench.workspace).seal(matter.matter_id, context.principal_id, receipt_id)
        except KeyError as exc:
            raise HTTPException(404, "Selection receipt not found") from exc
        except WorkspaceProblem as exc:
            return JSONResponse({"message": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"})
        return intake_response(matter, receipt)

    @app.get("/matters/{slug}/intake-receipts/{receipt_id}")
    def intake_receipt_status(request: Request, slug: str, receipt_id: str):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        try:
            receipt = IntakeReceipts(bench.workspace).get(
                matter.matter_id, context.principal_id, receipt_id,
                administrator_override=(
                    getattr(request.state, "administrator_matter_override", None) == matter.matter_id
                ),
            )
        except KeyError as exc:
            raise HTTPException(404, "Selection receipt not found") from exc
        return intake_response(matter, receipt)

    @app.get("/matters/{slug}/intake/{receipt_id}")
    def view_intake_receipt(request: Request, slug: str, receipt_id: str,
                            page: int = Query(1, ge=1, le=100), error: str = Query('', max_length=240)):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        receipts = IntakeReceipts(bench.workspace)
        try:
            receipt = receipts.snapshot(
                matter.matter_id, context.principal_id, receipt_id, offset=(page - 1) * 100, limit=100,
                administrator_override=(
                    getattr(request.state, "administrator_matter_override", None) == matter.matter_id
                ),
            )
            rows = receipt.pop("items")
            store = bench.source_store(matter)
            for row in rows:
                row["source_url"] = ""
                if row["catalog_document_id"]:
                    try:
                        document = store.get(row["catalog_document_id"])
                        if document.version_id == row["version_id"]:
                            row["source_url"] = f"/matters/{slug}/sources/{store.action_token(document)}"
                    except KeyError:
                        pass
        except KeyError as exc:
            raise HTTPException(404, "Selection receipt not found") from exc
        return templates.TemplateResponse(request=request, name="workbench_intake_receipt.html",
            context={**base_context(request, matter), "matter": matter, "receipt": receipt,
                "receipt_items": rows, "intake_labels": INTAKE_LABELS, "page": page,
                "receipt_error": error, "can_discard_receipt": matter.owner_id == context.principal_id and not receipt['has_received_data']},
            headers={"Cache-Control": "no-store"})

    @app.post("/matters/{slug}/intake/{receipt_id}/discard", dependencies=[Depends(require_csrf)])
    def discard_intake_receipt(request: Request, slug: str, receipt_id: str, confirm: str = Form('')):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        try:
            store = bench.source_store(matter)
            # The same source lock covers upload writes plus offset commits.
            # Saved partial bytes are checked even if their earlier commit failed.
            with store._lock:
                cancelled = IntakeReceipts(bench.workspace).discard(matter.matter_id, context.principal_id,
                    receipt_id, confirmed=confirm == 'yes',
                    upload_is_empty=lambda item_id, size: store.resumable_size(item_id, expected_size=size) == 0)
                for item_id in cancelled:
                    store.discard_resumable_upload(item_id)
        except KeyError as exc:
            raise HTTPException(404, 'Selection receipt not found') from exc
        except (WorkspaceProblem, UploadProblem) as exc:
            return RedirectResponse(f'/matters/{slug}/intake/{receipt_id}?error=' + quote_plus(str(exc)), status_code=303)
        audit(request, 'source.intake_receipt_discard', 'success', context=context, matter=matter,
            object_type='matter', object_id=matter.matter_id, details={'kind': 'intake_receipt', 'count': 1})
        return RedirectResponse(f'/matters/{slug}/setup?notice=' + quote_plus('Receipt discarded.'), status_code=303)

    @app.get("/matters/{slug}/intake/{receipt_id}/export", dependencies=[Depends(require_matter_response_lease)])
    def download_intake_receipt(request: Request, slug: str, receipt_id: str,
                                format_name: str = Query("csv", alias="format", pattern="^(csv|json|markdown)$")):
        context = auth_context(request)
        matter = response_lease_matter(request, slug)
        receipts = IntakeReceipts(bench.workspace)
        try:
            receipt = receipts.snapshot(
                matter.matter_id, context.principal_id, receipt_id,
                administrator_override=(
                    getattr(request.state, "administrator_matter_override", None) == matter.matter_id
                ),
            )
            artifact = export_intake_receipt(receipt, format_name)
        except KeyError as exc:
            raise HTTPException(404, "Selection receipt not found") from exc
        except (WorkspaceProblem, ExportProblem) as exc:
            return PlainTextResponse(str(exc), status_code=409)
        audit(request, "work_product.export", "success", context=context, matter=matter,
            object_type="matter", object_id=matter.matter_id, details={"kind": "intake_receipt", "format": format_name})
        return download_response(request, artifact)

    @app.post(
        "/matters/{slug}/upload-preflight",
        dependencies=[Depends(require_csrf_header)],
    )
    async def loose_file_upload_preflight(request: Request, slug: str):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        request_limit = MAX_UPLOAD_PREFLIGHT_REQUEST_BYTES
        content_length = request.headers.get("content-length", "")
        if content_length:
            try:
                if int(content_length) > request_limit:
                    raise ValueError
            except ValueError:
                return JSONResponse(
                    {"message": "The selected-file list is too large."},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > request_limit:
                return JSONResponse(
                    {"message": "The selected-file list is too large."},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return JSONResponse(
                {"message": "The selected-file list could not be read."},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            return JSONResponse(
                {"message": "Choose one or more files to review."},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        selected = payload["files"]
        if not 1 <= len(selected) <= MAX_UPLOAD_INTAKE_ITEMS:
            return JSONResponse(
                {
                    "message": (
                        f"Choose between 1 and {MAX_UPLOAD_INTAKE_ITEMS:,} files "
                        "to review."
                    )
                },
                status_code=413 if len(selected) > MAX_UPLOAD_INTAKE_ITEMS else 400,
                headers={"Cache-Control": "no-store"},
            )
        selection_nonce = payload.get("selection_nonce")
        if "selection_nonce" in payload and (
            not isinstance(selection_nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", selection_nonce) is None
        ):
            return JSONResponse(
                {"message": "The selected-file review request is invalid."},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        checkpoint_session_id = payload.get("checkpoint_session_id", "")
        checkpoint_collection_id = payload.get("checkpoint_collection_id", "")
        if (
            not isinstance(checkpoint_session_id, str)
            or not isinstance(checkpoint_collection_id, str)
            or bool(checkpoint_session_id) != bool(checkpoint_collection_id)
            or (
                bool(checkpoint_session_id)
                and (
                    re.fullmatch(
                        r"upload-session-[0-9a-f]{32}", checkpoint_session_id
                    )
                    is None
                    or re.fullmatch(
                        r"source-collection-[0-9a-f]{32}", checkpoint_collection_id
                    )
                    is None
                )
            )
        ):
            return JSONResponse(
                {"message": "The selected-file review request is invalid."},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        scanner_projection = await run_in_threadpool(
            scanner_status, bench.malware_scanner
        )
        result = evaluate_loose_file_preflight(
            selected,
            document_limit=bench.storage_policy.document_file_bytes,
            media_limit=bench.storage_policy.media_file_bytes,
            malware_scan_mode=bench.malware_scan_mode,
            scanner_ready=scanner_projection.ready,
            duplicate_token_scope=(
                f"recordbench:loose-file-preflight:v1:{matter.matter_id}:{selection_nonce}"
                if selection_nonce is not None
                else None
            ),
        )
        try:
            result["matter_capacity"] = await run_in_threadpool(
                bench.upload_preflight_capacity_projection,
                matter,
                context.principal_id,
                checkpoint_session_id=checkpoint_session_id,
                checkpoint_collection_id=checkpoint_collection_id,
            )
        except (OSError, RuntimeError):
            return JSONResponse(
                {"message": "Matter upload capacity is temporarily unavailable."},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        audit(
            request,
            "source.upload_preflight",
            "success",
            context=context,
            matter=matter,
            details={
                "count": result["selected_count"],
                "result_count": len(result["eligible_indexes"]),
            },
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post(
        "/matters/{slug}/upload-sessions",
        dependencies=[Depends(require_csrf_header)],
    )
    async def create_upload_session(request: Request, slug: str):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        if bench.ingestion is None:
            return JSONResponse(
                {"message": "Background source processing is temporarily unavailable."},
                status_code=503,
            )
        content_length = request.headers.get("content-length", "")
        if content_length:
            try:
                if int(content_length) > 6 * 1024 * 1024:
                    raise ValueError
            except ValueError:
                return JSONResponse(
                    {"message": "The upload file list is too large."}, status_code=413
                )
        raw = await request.body()
        if len(raw) > 6 * 1024 * 1024:
            return JSONResponse(
                {"message": "The upload file list is too large."}, status_code=413
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(
                {"message": "The selected-file list could not be read."}, status_code=400
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            return JSONResponse(
                {"message": "Choose one or more supported files."}, status_code=400
            )
        selected = payload["files"]
        if not 1 <= len(selected) <= MAX_UPLOAD_SESSION_ITEMS:
            return JSONResponse(
                {
                    "message": (
                        f"Choose between 1 and {MAX_UPLOAD_SESSION_ITEMS:,} files "
                        "for one upload collection."
                    )
                },
                status_code=413 if len(selected) > MAX_UPLOAD_SESSION_ITEMS else 400,
            )
        prepared: list[dict[str, object]] = []
        total_bytes = 0
        try:
            for item in selected:
                if not isinstance(item, dict):
                    raise UploadProblem("The selected-file list is invalid.")
                relative_path, filename, suffix, _ = PilotStore.validate_relative_upload_path(
                    str(item.get("relative_path") or item.get("name") or "")
                )
                raw_size = item.get("size")
                if isinstance(raw_size, bool) or not isinstance(raw_size, int):
                    raise UploadProblem("Every selected source must have a valid size.")
                if raw_size <= 0:
                    raise UploadProblem("Empty files cannot be uploaded.")
                maximum = maximum_file_bytes(
                    suffix,
                    document_limit=bench.storage_policy.document_file_bytes,
                    media_limit=bench.storage_policy.media_file_bytes,
                )
                if raw_size > maximum:
                    limit_label = format_bytes(maximum)
                    raise UploadProblem(
                        f"Each {('recording' if suffix in MEDIA_TYPES else 'file')} must be "
                        f"{limit_label} or smaller.",
                        413,
                    )
                total_bytes += raw_size
                if total_bytes > bench.storage_policy.upload_session_bytes:
                    raise UploadProblem(
                        "One upload collection must total "
                        f"{format_bytes(bench.storage_policy.upload_session_bytes)} or less.",
                        413,
                    )
                media_type = canonical_media_type(suffix)
                prepared.append(
                    {
                        "display_name": filename,
                        "relative_path": relative_path,
                        "media_type": media_type,
                        "expected_size": raw_size,
                    }
                )
        except UploadProblem as exc:
            return JSONResponse({"message": str(exc)}, status_code=exc.status_code)

        intake_receipt_id = payload.get("intake_receipt_id", "")
        intake_ordinals = payload.get("intake_ordinals", [])
        if (not isinstance(intake_receipt_id, str) or not isinstance(intake_ordinals, list)
            or any(not isinstance(i, int) or isinstance(i, bool) for i in intake_ordinals)):
            return JSONResponse({"message": "The selected-file receipt binding is invalid."}, status_code=400)
        if intake_receipt_id:
            try:
                with bench.workspace._lock:
                    matching_session = IntakeReceipts(bench.workspace).validate_upload_locked(
                        matter.matter_id, context.principal_id, intake_receipt_id, intake_ordinals, prepared)
                if matching_session:
                    session, _ = bench.workspace.upload_session(matter.matter_id, context.principal_id, matching_session)
                    if ((payload.get("resume_session_id") and payload["resume_session_id"] != matching_session)
                        or (payload.get("collection_id") and payload["collection_id"] != session.collection_id)):
                        return JSONResponse({"code": "upload_resume_mismatch",
                            "message": "The saved upload no longer matches this reviewed selection."},
                            status_code=409, headers={"Cache-Control": "no-store"})
                    session, existing_items = reconcile_upload_session(matter, context.principal_id, matching_session)
                    return JSONResponse(upload_projection(matter, session, existing_items), headers={"Cache-Control": "no-store"})
            except KeyError as exc:
                raise HTTPException(404, "Selection receipt not found") from exc
            except WorkspaceProblem as exc:
                return JSONResponse({"message": str(exc)}, status_code=409)
        elif intake_ordinals:
            return JSONResponse({"message": "The selected-file receipt is missing."}, status_code=400)

        resume_session_id = str(payload.get("resume_session_id") or "")
        collection_id = str(payload.get("collection_id") or "")
        if resume_session_id and intake_receipt_id:
            try:
                IntakeReceipts(bench.workspace).adopt_upload(
                    matter.matter_id, context.principal_id, intake_receipt_id,
                    intake_ordinals, prepared, resume_session_id, collection_id=collection_id)
            except KeyError as exc:
                raise HTTPException(404, "Saved upload not found") from exc
            except IntakeUploadResumeMismatch as exc:
                return JSONResponse({"code": "upload_resume_mismatch", "message": str(exc)},
                    status_code=409, headers={"Cache-Control": "no-store"})
            except WorkspaceProblem as exc:
                return JSONResponse({"message": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"})
        if resume_session_id:
            requested_manifest = tuple(
                (
                    str(item["display_name"]),
                    str(item["relative_path"]),
                    str(item["media_type"]),
                    int(item["expected_size"]),
                )
                for item in prepared
            )
            try:
                session, existing_items = bench.workspace.upload_session(
                    matter.matter_id, context.principal_id, resume_session_id
                )
                existing_manifest = tuple(
                    (
                        item.display_name,
                        item.relative_path,
                        item.media_type,
                        item.expected_size,
                    )
                    for item in existing_items
                )
            except KeyError:
                session = None
                existing_items = ()
                existing_manifest = ()
            if (
                session is None
                or session.state not in {"open", "complete", "partial"}
                or existing_manifest != requested_manifest
                or (collection_id and collection_id != session.collection_id)
            ):
                return JSONResponse(
                    {
                        "code": "upload_resume_mismatch",
                        "message": (
                            "The saved upload no longer matches this reviewed selection."
                        ),
                    },
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            session, existing_items = reconcile_upload_session(
                matter, context.principal_id, resume_session_id
            )
            audit(
                request,
                "source.upload_resume",
                "success",
                context=context,
                matter=matter,
                object_type="upload_session",
                object_id=session.upload_session_id,
                details={"count": session.item_count, "state": session.state},
            )
            return JSONResponse(
                upload_projection(matter, session, existing_items), status_code=200
            )

        collection_name = str(payload.get("collection_name") or "Uploaded sources")
        try:
            session, items = bench.create_upload_session(
                matter,
                context.principal_id,
                collection_name,
                prepared,
                collection_id=collection_id,
                intake_receipt_id=intake_receipt_id,
                intake_ordinals=intake_ordinals,
            )
        except UploadProblem as exc:
            return JSONResponse({"message": str(exc)}, status_code=exc.status_code)
        except (WorkspaceProblem, KeyError) as exc:
            return JSONResponse({"message": str(exc)}, status_code=400)
        audit(
            request,
            "source.upload_session_create",
            "success",
            context=context,
            matter=matter,
            object_type="upload_session",
            object_id=session.upload_session_id,
            details={"count": session.item_count, "state": session.state},
        )
        return JSONResponse(upload_projection(matter, session, items), status_code=201)

    @app.get("/matters/{slug}/upload-sessions/{session_id}")
    def upload_session_status(
        request: Request,
        slug: str,
        session_id: str,
        compact: bool = Query(False),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            if compact:
                session = bench.workspace.upload_session_record(
                    matter.matter_id, context.principal_id, session_id
                )
                return upload_compact_projection(matter, session)
            session, items = reconcile_upload_session(
                matter, context.principal_id, session_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Upload collection not found") from exc
        return upload_projection(matter, session, items)

    @app.put(
        "/matters/{slug}/upload-sessions/{session_id}/items/{item_id}",
        dependencies=[Depends(require_csrf_header)],
    )
    async def upload_source_chunk(
        request: Request,
        slug: str,
        session_id: str,
        item_id: str,
        x_upload_offset: str = Header("", alias="X-Upload-Offset"),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            item = bench.workspace.upload_item(
                matter.matter_id, context.principal_id, session_id, item_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Upload item not found") from exc
        if item.state == "queued":
            session = bench.workspace.upload_session_record(
                matter.matter_id, context.principal_id, session_id
            )
            return upload_delta_projection(matter, session, item)
        if item.state not in {"pending", "uploading"}:
            return JSONResponse(
                {"message": "This upload item is not accepting more data."},
                status_code=409,
            )
        try:
            offset = int(x_upload_offset)
            if offset < 0:
                raise ValueError
        except ValueError:
            return JSONResponse(
                {"message": "The upload offset is invalid."}, status_code=400
            )
        content_length = request.headers.get("content-length", "")
        if content_length:
            try:
                if int(content_length) <= 0 or int(content_length) > MAX_UPLOAD_CHUNK_BYTES:
                    raise ValueError
            except ValueError:
                return JSONResponse(
                    {"message": "Each upload chunk must be between 1 byte and 2 MiB."},
                    status_code=413,
                )
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_UPLOAD_CHUNK_BYTES:
                return JSONResponse(
                    {"message": "Each upload chunk must be 2 MiB or smaller."},
                    status_code=413,
                )
        store = bench.source_store(matter)
        try:
            with store._lock:
                item = bench.workspace.upload_item(matter.matter_id, context.principal_id, session_id, item_id)
                if item.state == 'queued':
                    session = bench.workspace.upload_session_record(matter.matter_id, context.principal_id, session_id)
                    return upload_delta_projection(matter, session, item)
                if item.state not in {'pending', 'uploading'}:
                    raise UploadProblem('This upload item is not accepting more data.', 409)
                bench.ensure_upload_write_capacity()
                actual = store.resumable_size(
                    item.upload_item_id, expected_size=item.expected_size
                )
                if actual > item.received_size:
                    item = bench.workspace.set_upload_item_offset(
                        matter.matter_id,
                        context.principal_id,
                        session_id,
                        item_id,
                        item.received_size,
                        actual,
                    )
                elif actual < item.received_size:
                    raise UploadProblem(
                        "The saved upload is incomplete. Start a new upload collection.", 409
                    )
                if offset != item.received_size:
                    raise UploadProblem(
                        f"Resume this source at byte {item.received_size}.", 409
                    )
                new_size = store.append_resumable_chunk(
                    item.upload_item_id,
                    offset=offset,
                    expected_size=item.expected_size,
                    chunk=bytes(body),
                )
                item = bench.workspace.set_upload_item_offset(
                    matter.matter_id,
                    context.principal_id,
                    session_id,
                    item_id,
                    item.received_size,
                    new_size,
                )
                session = bench.workspace.upload_session_record(
                    matter.matter_id, context.principal_id, session_id
                )
        except (UploadProblem, WorkspaceProblem) as exc:
            return JSONResponse(
                {"message": str(exc)},
                status_code=getattr(exc, "status_code", 409),
            )
        return upload_delta_projection(matter, session, item)

    @app.post(
        "/matters/{slug}/upload-sessions/{session_id}/items/{item_id}/finalize",
        dependencies=[Depends(require_csrf_header)],
    )
    def finalize_uploaded_source(
        request: Request, slug: str, session_id: str, item_id: str
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            item = bench.workspace.upload_item(
                matter.matter_id, context.principal_id, session_id, item_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Upload item not found") from exc
        if item.state == "queued":
            session = bench.workspace.upload_session_record(
                matter.matter_id, context.principal_id, session_id
            )
            return upload_delta_projection(matter, session, item)
        store = bench.source_store(matter)
        try:
            document = store.finalize_resumable_upload(
                item.upload_item_id,
                display_name=item.display_name,
                relative_path=item.relative_path,
                content_type=item.media_type,
                expected_size=item.expected_size,
            )
            media_source = is_media_type(document.media_type)
            item, _ = bench.workspace.finish_upload_item(
                matter.matter_id,
                context.principal_id,
                session_id,
                item_id,
                document.document_id,
                queue_ingestion=not media_source,
                source_version_id=document.version_id,
                media_details=(
                    {
                        "source_version_id": document.version_id,
                        "source_sha256": document.digest,
                        "byte_size": document.size,
                        "media_type": document.media_type,
                        "duration_ms": document.duration_ms,
                    }
                    if media_source
                    else None
                ),
                maximum_media_bytes=bench.storage_policy.media_file_bytes,
            )
            store.discard_resumable_upload(item.upload_item_id)
            if media_source:
                if bench.media is not None:
                    bench.media.notify()
                if document.has_video and bench.playback is not None:
                    bench.playback.ensure(matter, document)
            elif bench.ingestion is not None:
                bench.ingestion.notify()
            session = bench.workspace.upload_session_record(
                matter.matter_id, context.principal_id, session_id
            )
        except UploadProblem as exc:
            try:
                store.discard_resumable_upload(item.upload_item_id)
            except Exception:
                # Detection may already have atomically moved the staged inode
                # into quarantine. Ledger finalization is independent of that
                # best-effort staging cleanup and must still become terminal.
                pass
            try:
                bench.workspace.fail_upload_item(
                    matter.matter_id,
                    context.principal_id,
                    session_id,
                    item_id,
                    str(exc),
                )
            except Exception:
                pass
            return JSONResponse({"message": str(exc)}, status_code=exc.status_code)
        except WorkspaceProblem as exc:
            return JSONResponse({"message": str(exc)}, status_code=409)
        audit(
            request,
            "source.upload_item_queue",
            "success",
            context=context,
            matter=matter,
            object_type="upload_session",
            object_id=session_id,
            details={"count": 1, "state": "queued"},
        )
        return upload_delta_projection(matter, session, item)

    @app.post(
        "/matters/{slug}/upload-sessions/{session_id}/cancel",
        dependencies=[Depends(require_csrf_header)],
    )
    def cancel_upload_session(request: Request, slug: str, session_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            cancelled = bench.workspace.cancel_upload_session(
                matter.matter_id, context.principal_id, session_id
            )
            store = bench.source_store(matter)
            for item in cancelled:
                store.discard_resumable_upload(item.upload_item_id)
            session, items = bench.workspace.upload_session(
                matter.matter_id, context.principal_id, session_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Upload collection not found") from exc
        except WorkspaceProblem as exc:
            return JSONResponse({"message": str(exc)}, status_code=409)
        audit(
            request,
            "source.upload_session_cancel",
            "success",
            context=context,
            matter=matter,
            object_type="upload_session",
            object_id=session_id,
            details={"count": len(cancelled), "state": "cancelled"},
        )
        return upload_projection(matter, session, items)

    @app.post(
        "/matters/{slug}/uploads",
        dependencies=[Depends(require_csrf)],
    )
    def upload_sources(
        request: Request,
        slug: str,
        files: list[UploadFile] = File(...),
        collection_name: str = Form("Uploaded sources"),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        if not files or len(files) > MAX_FILES:
            return RedirectResponse(
                f"/matters/{slug}/setup?error="
                + quote_plus("Choose between 1 and 10 files for each upload."),
                status_code=303,
            )
        try:
            collection_name = bench.workspace._safe_text(
                collection_name, label="Collection name", maximum=160
            )
        except WorkspaceProblem as exc:
            return PlainTextResponse(str(exc), status_code=400)
        declared_bytes = sum(
            int(upload.size)
            if isinstance(upload.size, int) and not isinstance(upload.size, bool)
            else 100 * 1024 * 1024
            for upload in files
        )
        try:
            bench.ensure_direct_upload_capacity(matter, declared_bytes)
        except UploadProblem as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)
        store = bench.source_store(matter)
        used = 0
        created: list[str] = []
        accepted: list[str] = []
        try:
            for upload in files:
                before = set(store.documents)
                document, used = store.store_stream(
                    upload.filename or "",
                    upload.content_type,
                    upload.file,
                    request_used=used,
                    retain_extraction_failure=True,
                    defer_processing=bench.background_ingestion,
                )
                if document.document_id not in before:
                    created.append(document.document_id)
                accepted.append(document.document_id)
        except UploadProblem as exc:
            for document_id in created:
                store.remove(document_id)
            return PlainTextResponse(str(exc), status_code=exc.status_code)
        finally:
            for upload in files:
                upload.file.close()
        collection = None
        if created:
            try:
                collection = bench.workspace.create_source_collection(
                    matter.matter_id,
                    collection_name,
                    "upload",
                    context.principal_id,
                )
                bench.workspace.register_source_organizations(
                    matter.matter_id,
                    collection.collection_id,
                    tuple(
                        (
                            document_id,
                            store.get(document_id).relative_path
                            or store.get(document_id).display_name,
                        )
                        for document_id in created
                    ),
                    context.principal_id,
                )
            except Exception:
                for document_id in created:
                    bench.workspace.remove_source_organization(
                        matter.matter_id, document_id
                    )
                    store.remove(document_id)
                return PlainTextResponse(
                    "The sources could not be organized. No new sources were added.",
                    status_code=503,
                )
        needs_queue = bench.background_ingestion or any(
            is_media_type(store.get(document_id).media_type) for document_id in created
        )
        if needs_queue:
            try:
                for document_id in created:
                    document = store.get(document_id)
                    if is_media_type(document.media_type):
                        bench.workspace.queue_media_job(
                            matter.matter_id,
                            document.document_id,
                            document.version_id,
                            context.principal_id,
                            source_sha256=document.digest,
                            byte_size=document.size,
                            media_type=document.media_type,
                            duration_ms=document.duration_ms,
                            maximum_byte_size=bench.storage_policy.media_file_bytes,
                        )
                    elif bench.background_ingestion:
                        bench.workspace.queue_upload(matter.matter_id, document_id)
            except Exception:
                for document_id in created:
                    bench.workspace.cancel_document_ingest_job(matter.matter_id, document_id)
                    bench.workspace.remove_media_document(matter.matter_id, document_id)
                    bench.workspace.remove_source_organization(
                        matter.matter_id, document_id
                    )
                    store.remove(document_id)
                return PlainTextResponse(
                    "The sources could not be queued. No new sources were added.",
                    status_code=503,
                )
            if bench.ingestion is not None:
                bench.ingestion.notify()
            if bench.media is not None:
                bench.media.notify()
            if bench.playback is not None:
                for document_id in created:
                    document = store.get(document_id)
                    if document.has_video:
                        bench.playback.ensure(matter, document)
        else:
            for document_id in accepted:
                bench.index_document(matter, document_id)
        path = f"/matters/{slug}/setup"
        audit(
            request,
            "source.upload",
            "success",
            matter=matter,
            details={
                "count": len(created),
                "state": "queued" if needs_queue else "ready",
            },
        )
        return RedirectResponse(
            _query_url(
                path,
                view="list",
                collection=(collection.collection_id if collection is not None else ""),
                notice=(
                    f"{len(created)} source(s) "
                    + ("queued" if needs_queue else "added")
                ),
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/{token}/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_source(request: Request, slug: str, token: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            bench.retry_document(matter, token, context.principal_id)
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except UploadProblem as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)
        audit(
            request,
            "source.retry",
            "success",
            matter=matter,
            details={"state": "queued" if bench.background_ingestion else "ready"},
        )
        return RedirectResponse(
            f"/matters/{slug}?notice=" + quote_plus("Processing retried"), status_code=303
        )

    @app.post(
        "/matters/{slug}/sources/{token}/recording-check",
        dependencies=[Depends(require_csrf)],
    )
    async def decide_recording_check(request: Request, slug: str, token: str):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        form = await request.form()
        action = str(form.get("action") or "")
        inspection_id = str(form.get("inspection_id") or "")
        if action not in {"retry", "continue"}:
            return PlainTextResponse("Choose a recording action.", status_code=400)

        def verify_and_decide() -> None:
            store = bench.source_store(matter)
            with store.mutation_guard():
                document = store.get_by_action_token(token)
                decision_args = (
                    matter.matter_id, document.document_id, document.version_id,
                    context.principal_id, inspection_id,
                )
                try:
                    bench.workspace.validate_media_preflight_decision(
                        *decision_args, retry=action == "retry"
                    )
                    store.source_path(document.document_id, verify_digest=True)
                    bench.workspace.decide_media_preflight(
                        *decision_args, retry=action == "retry",
                        request_id=getattr(request.state, "request_id", f"request-{uuid.uuid4().hex}"),
                        session_id=context.session.session_id if context.session is not None else None,
                    )
                except KeyError as exc:
                    raise HTTPException(
                        409, "This recording check changed. Reload the source to see its current state."
                    ) from exc

        if matter.matter_id in recording_decisions_pending:
            return PlainTextResponse(
                "Another recording decision is being checked in this matter. Try again shortly.",
                status_code=409,
            )
        if len(recording_decisions_pending) >= recording_decision_limit:
            return PlainTextResponse(
                "Recording checks are busy. Try again shortly.", status_code=503,
                headers={"Retry-After": "3"},
            )
        recording_decisions_pending.add(matter.matter_id)
        try:
            await anyio.to_thread.run_sync(
                verify_and_decide, limiter=recording_decision_capacity
            )
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except UploadProblem as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)
        except ValueError:
            return PlainTextResponse("Check the recording again before transcription.", status_code=409)
        except (sqlite3.Error, OSError):
            return PlainTextResponse("The recording decision could not be saved. Try again.", status_code=503)
        finally:
            recording_decisions_pending.discard(matter.matter_id)
        if bench.media is not None:
            bench.media.notify()
        return RedirectResponse(f"/matters/{slug}/sources/{token}", status_code=303)

    @app.post(
        "/matters/{slug}/sources/{token}/remove",
        dependencies=[Depends(require_csrf)],
    )
    def remove_source(slug: str, token: str, request: Request):
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "This development action is available only on localhost.")
        try:
            matter = authorized_matter(request, slug)
            bench.remove_document(matter, token)
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except UploadProblem as exc:
            return PlainTextResponse(str(exc), status_code=exc.status_code)
        except RuntimeError as exc:
            raise HTTPException(503, "The source could not be removed safely. Try again.") from exc
        audit(
            request,
            "source.remove",
            "success",
            matter=matter,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/setup", view="list", notice="Source removed"
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/bulk",
        dependencies=[Depends(require_csrf)],
    )
    def organize_sources(
        request: Request,
        slug: str,
        selected: list[str] = Form(default=[]),
        action: str = Form(..., max_length=40),
        collection_id: str = Form("", max_length=80),
        source_set_id: str = Form("", max_length=80),
        source_set_name: str = Form("", max_length=160),
        return_query: str = Form("", max_length=8192),
    ):
        context = auth_context(request)
        # Only listed Sources filters return to this same matter's fixed route.
        # No caller-selected redirect target or free-form query is forwarded.
        try:
            parsed_return = parse_qs(return_query, max_num_fields=16)
        except ValueError as exc:
            raise HTTPException(400, "Return to Sources and try again.") from exc
        return_filters = {key: values[0] for key, values in parsed_return.items()
            if key in {"q", "status", "kind", "review", "collection", "source_set",
                "folder", "same_content", "matching_only", "sort", "page_size", "page"}}
        try:
            matter = authorized_matter(request, slug)
            if not 1 <= len(selected) <= 100:
                raise WorkspaceProblem("Select between 1 and 100 sources on this page.")
            store = bench.source_store(matter)
            documents = tuple(
                store.get_by_action_token(token).document_id
                for token in dict.fromkeys(selected)
            )
            if action in {"unreviewed", "reviewed", "flagged"}:
                changed = bench.workspace.update_source_review_state(
                    matter.matter_id, documents, action, context.principal_id
                )
                notice = f"{changed} source(s) marked {action}"
                audit_state = action
            elif action == "move":
                changed = bench.workspace.move_sources_to_collection(
                    matter.matter_id,
                    documents,
                    collection_id,
                    context.principal_id,
                )
                notice = f"{changed} source(s) moved"
                audit_state = "completed"
            elif action == "create_set":
                source_set_record = bench.workspace.create_source_set(
                    matter.matter_id,
                    source_set_name,
                    documents,
                    context.principal_id,
                )
                changed = source_set_record.source_count
                notice = f"Source set “{source_set_record.name}” created"
                audit_state = "completed"
            elif action == "add_to_set":
                changed = bench.workspace.add_sources_to_set(
                    matter.matter_id,
                    source_set_id,
                    documents,
                    context.principal_id,
                )
                notice = f"{changed} new source(s) added to the set"
                audit_state = "completed"
            else:
                raise WorkspaceProblem("Choose a valid source action.")
        except KeyError as exc:
            raise HTTPException(404, "Source or source group not found") from exc
        except (WorkspaceProblem, UploadProblem) as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/setup", view="list", **return_filters, error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "source.bulk_organize",
            "success",
            context=context,
            matter=matter,
            details={"count": changed, "state": audit_state},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/setup", view="list", **return_filters, notice=notice),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/collections/{collection_id}/rename",
        dependencies=[Depends(require_csrf)],
    )
    def rename_source_collection(
        request: Request,
        slug: str,
        collection_id: str,
        name: str = Form(..., max_length=160),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            collection = bench.workspace.rename_source_collection(
                matter.matter_id, collection_id, name, context.principal_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Source collection not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/setup", error=str(exc)), status_code=303
            )
        audit(
            request,
            "source.collection_rename",
            "success",
            context=context,
            matter=matter,
            object_type="source_collection",
            object_id=collection.collection_id,
            details={"count": collection.source_count},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/setup",
                collection=collection.collection_id,
                view="list",
                notice="Collection renamed",
            ),
            status_code=303,
        )

    @app.get("/matters/{slug}/sources/{token}", response_class=HTMLResponse)
    def review_source(
        request: Request,
        slug: str,
        token: str,
        browse: str = Query("", max_length=8192),
        unit: int | None = Query(None, ge=1, le=100_000),
        start_ms: int = Query(0, ge=0, le=43_202_000),
        segment: str = Query("", max_length=100),
        q: str = Query("", max_length=240),
        speaker: str = Query("", max_length=100),
        speaker_review: str = Query("", max_length=100),
        flag: str = Query("", pattern="^(|low|overlap|edited)$"),
        page: int = Query(1, ge=1, le=100_000),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            document = bench.source_store(matter).get_by_action_token(token)
            source_sequence = source_browser_projection(
                matter, document.document_id, browse,
                _source_review_return_href(slug, request.query_params.get("entity_return_to", "")),
                reader_state=dict(unit=unit, start_ms=start_ms, segment=segment, q=q, speaker=speaker,
                                  speaker_review=speaker_review, flag=flag,
                                  page=page if page > 1 else None),
            )
            if is_media_type(document.media_type):
                with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                    media = bench.media_review(
                        matter,
                        token,
                        start_ms=start_ms,
                        focus_segment_id=segment,
                    )
                    source_note_basis = bench.source_note_basis(media.document)
                speaker_labels = transcript_speaker_labels(media.segments)
                query_key = " ".join(q.split()).casefold()
                selected = tuple(
                    item
                    for item in media.segments
                    if (
                        not query_key
                        or query_key in item.current_text.casefold()
                        or query_key
                        in speaker_labels.get(
                            item.speaker_cluster, item.speaker_display_name
                        ).casefold()
                    )
                    and (not speaker or item.speaker_cluster == speaker)
                    and (
                        not flag
                        or (flag == "low" and item.low_confidence)
                        or (flag == "overlap" and item.overlap)
                        or (flag == "edited" and item.current_revision)
                    )
                )
                page_size = 200
                if unit is not None and not any((q, speaker, flag)):
                    target = next(
                        (index for index, item in enumerate(selected) if item.ordinal == unit),
                        None,
                    )
                    if target is not None:
                        page = target // page_size + 1
                elif start_ms and not any((q, speaker, flag)):
                    target = next(
                        (
                            index
                            for index, item in enumerate(selected)
                            if item.start_ms <= start_ms < item.end_ms
                        ),
                        None,
                    )
                    if target is not None:
                        page = target // page_size + 1
                total_pages = max(math.ceil(len(selected) / page_size), 1)
                page = min(page, total_pages)
                offset = (page - 1) * page_size
                page_segments = selected[offset : offset + page_size]
                focused_speaker = (
                    speaker_review
                    if any(
                        item.speaker_cluster == speaker_review
                        for item in media.speakers
                    )
                    else ""
                )
                if not administrator_override:
                    bench.workspace.record_matter_activity(
                        matter.matter_id,
                        context.principal_id,
                        "source",
                        document.document_id,
                        locator=media.start_ms,
                    )
                audit(
                    request,
                    "source.media_review_open",
                    "success",
                    matter=matter,
                    object_type="source",
                    object_id=document.document_id,
                )
                return templates.TemplateResponse(
                    request=request,
                    name="workbench_media_review.html",
                    context={
                        **base_context(request, matter),
                        "matter": matter,
                        "media": media,
                        "segments": page_segments,
                        "segment_total": len(selected),
                        "segment_first": offset + 1 if selected else 0,
                        "segment_last": offset + len(page_segments),
                        "segment_page": page,
                        "segment_pages": total_pages,
                        "transcript_query": q,
                        "transcript_speaker": speaker,
                        "speaker_review": focused_speaker,
                        "speaker_labels": speaker_labels,
                        "transcript_flag": flag,
                        "summary_failure": _media_summary_failure_view(media.summary),
                        "transcript_notices": transcript_processing_notices(media.transcript),
                        "source_sequence": source_sequence,
                        "source_note_request_key": getattr(request.state, "source_note_request_key", uuid.uuid4().hex),
                        "source_note_draft": getattr(request.state, "source_note_draft", ""),
                        "source_note_basis": source_note_basis,
                        "format_timestamp": format_timestamp,
                        "notice": notice,
                        "error": error,
                    },
                )
            with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                source = bench.source_review(matter, token, unit_number=unit or 1)
                source_note_basis = bench.source_note_basis(source.document)
            if not administrator_override:
                bench.workspace.record_matter_activity(
                    matter.matter_id,
                    context.principal_id,
                    "source",
                    document.document_id,
                    locator=source.unit_index,
                )
        except (KeyError, StopIteration) as exc:
            raise HTTPException(404, "Source not found") from exc
        except WorkspaceProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(
            request,
            "source.review_open",
            "success",
            matter=matter,
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_source_review.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "source": source,
                "entity_support_token": (
                    bench._citation(matter, bench._candidate(matter, source.document, source.unit, source.unit_index)).support_token
                    if source.unit and source.document.state == "ready" else ""
                ),
                "source_coverage_notice": (
                    str(_source_coverage(bench.workspace.matter_readiness(matter.matter_id))["notice"])
                    if source.document.media_type in EMAIL_MEDIA_TYPES or source.document.media_type == "application/pdf" else ""
                ),
                "source_sequence": source_sequence,
                "source_find": find_in_source(source.document, q) if q else None,
                "find_query": " ".join(q.split())[:MAX_FIND_CHARS],
                "source_note_request_key": getattr(request.state, "source_note_request_key", uuid.uuid4().hex),
                "source_note_draft": getattr(request.state, "source_note_draft", ""),
                "source_note_basis": source_note_basis,
                "notice": notice,
                "error": error,
            },
        )

    @app.post("/matters/{slug}/sources/{token}/notes",
              dependencies=[Depends(require_csrf), Depends(require_matter_response_lease)])
    def create_source_note(
        request: Request, slug: str, token: str,
        body: str = Form(..., max_length=20_000),
        source_version_id: str = Form(..., max_length=160),
        source_basis: str = Form(..., max_length=64),
        request_key: str = Form(..., max_length=80),
        unit: int = Form(1, ge=1, le=100_000),
        start_ms: int = Form(0, ge=0, le=43_200_000),
        return_to: str = Form("", max_length=8192),
    ):
        def finish(response):
            response.headers["Cache-Control"] = "no-store"
            return transfer_matter_response_lease(request, response, through_send=True)

        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        return_path = _source_review_return_href(slug, return_to)
        fallback = _query_url(f"/matters/{slug}/sources/{token}", unit=unit, start_ms=start_ms)
        try:
            matter = authorized_matter(request, slug)
            with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                refresh_context_authority(request, slug, context, False)
                item, created = bench.save_source_note(
                    matter, context.principal_id, token, source_version_id=source_version_id,
                    source_basis=source_basis, body=body, request_key=request_key,
                    unit_number=unit, start_ms=start_ms,
                )
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return finish(JSONResponse({"message": str(exc)}, status_code=409))
            # Render the reader with the submitted draft intact; do not put
            # human notes in redirect URLs or silently discard failed saves.
            request.state.source_note_draft = body
            request.state.source_note_request_key = request_key
            recovery_query = urlparse(return_path or fallback).query
            query = parse_qs(recovery_query)
            page_value = query.get("page", ["1"])[0]
            recovery_page = min(max(int(page_value), 1), 100_000) if re.fullmatch(r"[0-9]{1,6}", page_value) else 1
            # The response contains a reader, so its links/forms must point at
            # that reader rather than at the POST-only save endpoint.
            recovery_request = Request({**request.scope,
                "path": f"/matters/{slug}/sources/{token}",
                "query_string": recovery_query.encode("utf-8")})
            response = review_source(recovery_request, slug, token,
                browse=query.get("browse", [""])[0], unit=unit, start_ms=start_ms,
                segment=query.get("segment", [""])[0], q=query.get("q", [""])[0],
                speaker=query.get("speaker", [""])[0], speaker_review=query.get("speaker_review", [""])[0],
                flag=query.get("flag", [""])[0],
                page=recovery_page,
                notice="", error=str(exc))
            response.status_code = 409
            return finish(response)
        audit(request, "notebook.item_create", "success", context=context, matter=matter,
              object_type="notebook_item", object_id=item.item_id,
              details={"created": created, "state": item.status})
        notice = "Your note was saved to case notes for review." if created else "This note was already saved to case notes."
        if wants_json:
            return finish(JSONResponse({"message": notice, "created": created,
                                        "item_id": item.item_id, "status": item.status}))
        parsed = urlparse(return_path or fallback)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query.pop("error", None)
        query["notice"] = [notice]
        return finish(RedirectResponse(parsed._replace(query=urlencode(query, doseq=True)).geturl(), status_code=303))

    @app.get("/matters/{slug}/sources/{token}/media-status")
    def media_status(request: Request, slug: str, token: str):
        try:
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            if not is_media_type(document.media_type):
                raise KeyError(token)
            job = bench.workspace.media_job(
                matter.matter_id, document.document_id, document.version_id
            )
            transcript = bench.workspace.media_transcript(
                matter.matter_id, document.document_id, document.version_id
            )
            summary = (
                bench.workspace.media_summary(
                    matter.matter_id, document.document_id, document.version_id
                )
                if transcript is not None
                else None
            )
        except KeyError as exc:
            raise HTTPException(404, "Media source not found") from exc
        return JSONResponse(
            {
                "state": job.display_state if job is not None else "unavailable",
                "stage": job.stage if job is not None else "Not queued",
                "progress": job.progress if job is not None else 0,
                "message": job.message if job is not None else "",
                "ready": document.state == "ready",
                "playback_state": document.playback_state or "unknown",
                "playback_message": document.playback_message,
                "playback_ready": document.playback_state in {"original", "ready"},
                "playback_compatible_copy": document.playback_state == "ready",
                "summary_state": summary.state if summary is not None else "not_created",
                "review_url": f"/matters/{matter.slug}/sources/{token}",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/matters/{slug}/sources/{token}/resume",
        dependencies=[Depends(require_csrf_header)],
        include_in_schema=False,
    )
    async def save_media_resume(request: Request, slug: str, token: str):
        context = auth_context(request)
        try:
            payload = await request.json()
            position_ms = payload.get("position_ms")
            if (
                isinstance(position_ms, bool)
                or not isinstance(position_ms, int)
                or not 0 <= position_ms <= 43_200_000
            ):
                raise ValueError("invalid resume position")
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            if not is_media_type(document.media_type):
                raise KeyError(token)
            bounded = min(position_ms, max(document.duration_ms - 1, 0))
            activity = bench.workspace.record_matter_activity(
                matter.matter_id,
                context.principal_id,
                "source",
                document.document_id,
                locator=bounded,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "Choose a valid playback position") from exc
        except KeyError as exc:
            raise HTTPException(404, "Media source not found") from exc
        return JSONResponse(
            {"saved": True, "position_ms": activity.locator},
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/matters/{slug}/sources/{token}/playback/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_browser_playback(request: Request, slug: str, token: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            if not document.has_video or bench.playback is None:
                raise KeyError(token)
            document = bench.playback.ensure(matter, document, retry=True)
        except KeyError as exc:
            raise HTTPException(404, "Video source not found") from exc
        audit(
            request,
            "source.playback_retry",
            "success",
            context=context,
            matter=matter,
            object_type="source",
            object_id=document.document_id,
            details={"state": document.playback_state},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/sources/{token}",
                notice="Browser playback preparation queued",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/{token}/summary",
        dependencies=[Depends(require_csrf)],
    )
    def request_media_summary(request: Request, slug: str, token: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            if not is_media_type(document.media_type):
                raise KeyError(token)
            summary = bench.workspace.queue_media_summary(
                matter.matter_id,
                document.document_id,
                document.version_id,
                context.principal_id,
            )
            bench.media.notify()
        except KeyError as exc:
            raise HTTPException(404, "Media transcript not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/sources/{token}", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "transcript.summary_request",
            "success",
            context=context,
            matter=matter,
            object_type="transcript_summary",
            object_id=summary.transcript_id,
            details={"state": summary.state},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/sources/{token}",
                notice="Transcript overview queued",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/{token}/segments/{segment_id}",
        dependencies=[Depends(require_csrf)],
    )
    def revise_media_segment(
        request: Request,
        slug: str,
        token: str,
        segment_id: str,
        expected_revision: int = Form(..., ge=0),
        text: str = Form(..., max_length=20_000),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            store = bench.source_store(matter)
            with store.mutation_guard():
                document = store.get_by_action_token(token)
                if not is_media_type(document.media_type):
                    raise KeyError(token)
                segment = bench.workspace.revise_transcript_segment(
                    matter.matter_id,
                    document.document_id,
                    document.version_id,
                    segment_id,
                    expected_revision=expected_revision,
                    text=text,
                    actor_id=context.principal_id,
                )
                bench.refresh_media_projection(matter, document)
            if bench.media is not None:
                bench.media.notify()
        except KeyError as exc:
            raise HTTPException(404, "Transcript passage not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/sources/{token}",
                    error=str(exc),
                ),
                status_code=303,
            )
        audit(
            request,
            "transcript.segment_edit",
            "success",
            context=context,
            matter=matter,
            object_type="transcript_segment",
            object_id=segment.segment_id,
            details={"state": "reviewed"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/sources/{token}",
                start_ms=str(segment.start_ms),
                segment=segment.segment_id,
                notice="Transcript correction saved and search refreshed",
            )
            + f"#segment-{segment.ordinal}",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/{token}/speakers",
        dependencies=[Depends(require_csrf)],
    )
    def revise_media_speaker(
        request: Request,
        slug: str,
        token: str,
        speaker_cluster: str = Form(..., max_length=100),
        expected_revision: int = Form(..., ge=0),
        display_name: str = Form(..., max_length=120),
        identity_state: str = Form(..., pattern="^(cluster|confirmed)$"),
        return_start_ms: int = Form(0, ge=0, le=43_200_000),
        return_page: int = Form(1, ge=1, le=100_000),
        return_q: str = Form("", max_length=240),
        return_speaker: str = Form("", max_length=100),
        return_flag: str = Form("", pattern="^(|low|overlap|edited)$"),
        return_segment: str = Form("", max_length=100),
    ):
        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            matter = authorized_matter(request, slug)
            store = bench.source_store(matter)
            with store.mutation_guard():
                document = store.get_by_action_token(token)
                if not is_media_type(document.media_type):
                    raise KeyError(token)
                mapping = bench.workspace.revise_speaker_mapping(
                    matter.matter_id,
                    document.document_id,
                    document.version_id,
                    speaker_cluster,
                    expected_revision=expected_revision,
                    display_name=display_name,
                    identity_state=identity_state,
                    actor_id=context.principal_id,
                )
                bench.refresh_media_projection(matter, document)
                speaker_summary = bench.workspace.media_summary(
                    matter.matter_id, document.document_id, document.version_id
                )
            if bench.media is not None:
                bench.media.notify()
        except KeyError as exc:
            raise HTTPException(404, "Speaker label not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409)
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/sources/{token}",
                    start_ms=str(return_start_ms),
                    page=str(return_page),
                    q=return_q,
                    speaker=return_speaker,
                    speaker_review=speaker_cluster,
                    flag=return_flag,
                    segment=return_segment,
                    error=str(exc),
                )
                + "#speaker-review",
                status_code=303,
            )
        audit(
            request,
            "transcript.speaker_review",
            "success",
            context=context,
            matter=matter,
            object_type="speaker_mapping",
            object_id=mapping.speaker_cluster,
            details={"state": "confirmed" if identity_state == "confirmed" else "review"},
        )
        if wants_json:
            return JSONResponse(
                {
                    "speaker_cluster": mapping.speaker_cluster,
                    "display_name": mapping.display_name,
                    "identity_state": mapping.identity_state,
                    "revision": mapping.revision,
                    "segment_count": mapping.segment_count,
                    "message": "Speaker label saved across this transcript.",
                    "overview_refreshing": bool(
                        speaker_summary is not None
                        and speaker_summary.state in {"queued", "running", "stale"}
                    ),
                }
            )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/sources/{token}",
                start_ms=str(return_start_ms),
                page=str(return_page),
                q=return_q,
                speaker=return_speaker,
                speaker_review=mapping.speaker_cluster,
                flag=return_flag,
                segment=return_segment,
                notice="Speaker label saved and search refreshed",
            )
            + "#speaker-review",
            status_code=303,
        )

    @app.get(
        "/matters/{slug}/sources/{token}/summary-export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def download_media_summary(
        request: Request,
        slug: str,
        token: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|markdown)$"),
    ):
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            if not is_media_type(document.media_type):
                raise KeyError(token)
            summary = bench.workspace.media_summary(
                matter.matter_id, document.document_id, document.version_id
            )
            if summary is None:
                raise KeyError(token)
            segments = bench.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            )
            artifact = export_media_summary(
                document.display_name, summary, segments, format_name
            )
        except KeyError as exc:
            raise HTTPException(404, "Transcript overview not found") from exc
        except ValueError as exc:
            raise HTTPException(
                409, "Transcript overview is not ready for export"
            ) from exc
        filename = (
            safe_file_stem(
                f"{document.display_name}-transcript-overview",
                "transcript-overview",
            )
            + artifact.suffix
        )
        audit(
            request,
            "work_product.export",
            "success",
            context=context,
            matter=matter,
            object_type="transcript_summary",
            object_id=summary.transcript_id,
            details={"kind": "transcript_summary", "format": format_name},
        )
        return transfer_matter_response_lease(
            request,
            Response(
                content=artifact.body,
                media_type=artifact.media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    **response_headers("Export", "work-product"),
                    "Cache-Control": "no-store",
                },
            ),
        )

    @app.get(
        "/matters/{slug}/sources/{token}/transcript-export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def download_media_transcript(
        request: Request,
        slug: str,
        token: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|txt|markdown|srt|vtt|csv|json)$"),
    ):
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            if not is_media_type(document.media_type):
                raise KeyError(token)
            segments = bench.workspace.transcript_segments(
                matter.matter_id, document.document_id, document.version_id
            )
            transcript = bench.workspace.media_transcript(
                matter.matter_id, document.document_id, document.version_id
            )
            artifact = export_transcript(
                document.display_name, segments, format_name,
                notices=transcript_processing_notices(transcript),
            )
        except KeyError as exc:
            raise HTTPException(404, "Transcript not found") from exc
        except ValueError as exc:
            raise HTTPException(409, "Transcript is not ready for export") from exc
        filename = (
            safe_file_stem(
                f"{document.display_name}-transcript", "transcript"
            )
            + artifact.suffix
        )
        audit(
            request,
            "transcript.export",
            "success",
            context=context,
            matter=matter,
            object_type="transcript",
            object_id=document.document_id,
            details={"format": format_name},
        )
        return transfer_matter_response_lease(
            request,
            Response(
                content=artifact.body,
                media_type=artifact.media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    **response_headers("Export", "work-product"),
                    "Cache-Control": "no-store",
                },
            ),
        )

    @app.post(
        "/matters/{slug}/sources/{token}/clips",
        dependencies=[Depends(require_csrf)],
    )
    def create_media_clip(
        request: Request,
        slug: str,
        token: str,
        title: str = Form(..., max_length=160),
        start_ms: int = Form(..., ge=0, le=43_200_000),
        end_ms: int = Form(..., ge=1, le=43_200_000),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            if not is_media_type(document.media_type):
                raise KeyError(token)
            clip = bench.workspace.create_media_clip(
                matter.matter_id,
                document.document_id,
                document.version_id,
                title=title,
                start_ms=start_ms,
                end_ms=end_ms,
                actor_id=context.principal_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Media source not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/sources/{token}", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "media.clip_create",
            "success",
            context=context,
            matter=matter,
            object_type="media_clip",
            object_id=clip.clip_id,
            details={"created": True},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/sources/{token}",
                start_ms=str(start_ms),
                notice="Clip saved as exportable work product",
            )
            + "#media-clips",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/{token}/clips/{clip_id}/delete",
        dependencies=[Depends(require_csrf)],
    )
    def delete_media_clip(
        request: Request, slug: str, token: str, clip_id: str
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            clip = bench.workspace.media_clip(matter.matter_id, clip_id)
            if clip.document_id != document.document_id or clip.source_version_id != document.version_id:
                raise KeyError(clip_id)
            bench.workspace.delete_media_clip(
                matter.matter_id, clip_id, context.principal_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Media clip not found") from exc
        audit(
            request,
            "media.clip_delete",
            "success",
            context=context,
            matter=matter,
            object_type="media_clip",
            object_id=clip_id,
            details={"state": "deleted"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/sources/{token}", notice="Clip deleted"
            )
            + "#media-clips",
            status_code=303,
        )

    @app.get("/matters/{slug}/sources/{token}/clips/{clip_id}/download")
    def download_media_clip(
        request: Request, slug: str, token: str, clip_id: str
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None) == matter.matter_id
            )
            directory = bench.begin_media_clip_export(matter)
            request.state.media_clip_response_lease = (matter.matter_id, directory)
            store = bench.source_store(matter)
            with store.mutation_guard():
                document = store.get_by_action_token(token)
                clip = bench.workspace.media_clip(matter.matter_id, clip_id)
                if (
                    not is_media_type(document.media_type)
                    or clip.document_id != document.document_id
                    or clip.source_version_id != document.version_id
                ):
                    raise KeyError(clip_id)
                source = store.source_path(document.document_id)
                suffix = ".mp4" if document.has_video else ".wav"
                output = directory / f"clip-{clip.clip_id[-12:]}{suffix}"
                render_clip(
                    source, clip, has_video=document.has_video, output=output
                )
        except KeyError as exc:
            raise HTTPException(404, "Media clip not found") from exc
        except (RuntimeError, ValueError, WorkspaceProblem) as exc:
            raise HTTPException(503, "The clip could not be rendered. Try again.") from exc

        response = FileResponse(
            output,
            media_type="video/mp4" if document.has_video else "audio/wav",
            filename=f"media-clip-{clip.clip_id[-12:]}{suffix}",
            headers={
                **response_headers("Export", "work-product"),
                "Cache-Control": "no-store",
            },
        )
        # Recheck live authority after rendering and response construction, at
        # the last practical boundary before returning the admitted download.
        with bench.workspace._lock:
            refresh_context_authority(request, slug, context, administrator_override)
            audit(
                request,
                "media.clip_export",
                "success",
                context=context,
                matter=matter,
                object_type="media_clip",
                object_id=clip.clip_id,
                details={"format": "mp4" if document.has_video else "wav"},
            )
            return response

    @app.post(
        "/matters/{slug}/sources/{token}/review-state",
        dependencies=[Depends(require_csrf)],
    )
    def update_source_review_state(
        request: Request,
        slug: str,
        token: str,
        state: str = Form(..., max_length=20),
        browse: str = Query("", max_length=8192),
    ):
        origin = _source_review_return_href(slug, request.query_params.get("entity_return_to", ""))
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            bench.workspace.update_source_review_state(
                matter.matter_id,
                (document.document_id,),
                state,
                context.principal_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/sources/{token}", error=str(exc), browse=browse,
                    entity_return_to=origin
                ),
                status_code=303,
            )
        audit(
            request,
            "source.review_state",
            "success",
            context=context,
            matter=matter,
            details={"count": 1, "state": state},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/sources/{token}",
                notice=f"Source marked {state}", browse=browse,
                entity_return_to=origin,
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/{token}/review-next",
        dependencies=[Depends(require_csrf)],
    )
    def review_source_and_continue(request: Request, slug: str, token: str,
                                   browse: str = Query("", max_length=8192)):
        context = auth_context(request)
        origin = _source_review_return_href(slug, request.query_params.get("entity_return_to", ""))
        try:
            matter = authorized_matter(request, slug)
            document = bench.source_store(matter).get_by_action_token(token)
            browser = source_browser_projection(matter, document.document_id, browse,
                _source_review_return_href(slug, request.query_params.get("entity_return_to", ""))) if browse else None
            bench.workspace.update_source_review_state(
                matter.matter_id,
                (document.document_id,),
                "reviewed",
                context.principal_id,
            )
            remaining = bench.workspace.source_catalog_page(
                matter.matter_id,
                tone="ready",
                review_state="unreviewed",
                sort="oldest",
                limit=1,
            )
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/sources/{token}", error=str(exc), browse=browse, entity_return_to=origin),
                status_code=303,
            )
        audit(
            request,
            "source.review_continue",
            "success",
            context=context,
            matter=matter,
            object_type="source",
            object_id=document.document_id,
            details={"count": 1, "state": "reviewed"},
        )
        if browser is not None:
            return RedirectResponse(
                browser["next"]["href"] if browser["next"] else (origin or browser["library_href"]),
                status_code=303,
            )
        if remaining.items:
            following = remaining.items[0]
            return RedirectResponse(
                _query_url(
                    source_catalog_href(matter, following),
                    notice="Source reviewed · next item opened", entity_return_to=origin,
                ),
                status_code=303,
            )
        if origin:
            return RedirectResponse(origin, status_code=303)
        return RedirectResponse(
            _query_url(
                f"/matters/{matter.slug}/home",
                notice="Review queue complete",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/sources/{token}/ask",
        dependencies=[Depends(require_csrf)],
    )
    def ask_using_source(request: Request, slug: str, token: str):
        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            matter = authorized_matter(request, slug)
            with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                refresh_context_authority(request, slug, context, False)
                document = bench.source_store(matter).get_by_action_token(token)
                if document.state != "ready":
                    raise WorkspaceProblem(
                        "This source must finish processing before it can answer questions."
                    )
                label = f"Only · {document.display_name}"
                source_set = bench.workspace.singleton_source_set(
                    matter.matter_id,
                    document.document_id,
                    label[:160],
                    context.principal_id,
                )
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"})
            return RedirectResponse(
                _query_url(f"/matters/{slug}/sources/{token}", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "source.ask_scope",
            "success",
            context=context,
            matter=matter,
            object_type="source_set",
            object_id=source_set.source_set_id,
            details={"count": 1},
        )
        if wants_json:
            return JSONResponse({"source_set_id": source_set.source_set_id, "name": label[:160]},
                                headers={"Cache-Control": "no-store"})
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                source_set=source_set.source_set_id,
                notice="Answer scope limited to this source",
                entity_return_to=_source_review_return_href(slug, request.query_params.get("entity_return_to", "")),
            )
            + "#matter-question",
            status_code=303,
        )

    @app.get(
        "/matters/{slug}/sources/{token}/content",
        dependencies=[Depends(require_matter_response_lease)],
    )
    @app.head(
        "/matters/{slug}/sources/{token}/content",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def source_content(
        request: Request,
        slug: str,
        token: str,
        range_header: str = Header("", alias="Range"),
    ):
        try:
            matter = response_lease_matter(request, slug)
            store = bench.source_store(matter)
            document = store.get_by_action_token(token)
        except KeyError as exc:
            raise HTTPException(404, "Source not found") from exc
        if document.origin != "upload" or not document.stored_name:
            raise HTTPException(404, "The original is not stored in this workbench.")
        response_media_type = document.media_type
        expected_size = document.size
        compatible_copy = False
        if is_media_type(document.media_type):
            try:
                path, response_media_type, expected_size, compatible_copy = (
                    store.browser_playback_source(document.document_id)
                )
            except UploadProblem as exc:
                raise HTTPException(409, "This source is unavailable for review.") from exc
            allowed_parent = store.derived if compatible_copy else store.files
        else:
            path = store.files / document.stored_name
            allowed_parent = store.files
        if path.parent != allowed_parent:
            raise HTTPException(404, "Source not found")
        parsed_range: tuple[int | None, int | None] | None = None
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if (
                match is None
                or (not match.group(1) and not match.group(2))
                or len(match.group(1)) > 20
                or len(match.group(2)) > 20
            ):
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{expected_size}"},
                )
            parsed_range = (
                int(match.group(1)) if match.group(1) else None,
                int(match.group(2)) if match.group(2) else None,
            )
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
                raise OSError
        except OSError as exc:
            try:
                os.close(descriptor)
            except (NameError, OSError):
                pass
            raise HTTPException(409, "This source is unavailable for review.") from exc
        size = int(metadata.st_size)
        start, end = 0, max(size - 1, 0)
        status_code = 200
        if parsed_range is not None:
            first, last = parsed_range
            if first is not None:
                start = first
                end = last if last is not None else end
            else:
                suffix_length = last or 0
                start = max(size - suffix_length, 0)
            end = min(end, size - 1)
            if start < 0 or start > end or start >= size:
                os.close(descriptor)
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status_code = 206
        length = end - start + 1

        def stream() -> Iterator[bytes]:
            remaining = length
            try:
                os.lseek(descriptor, start, os.SEEK_SET)
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                os.close(descriptor)

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Disposition": "inline",
            "Cache-Control": "no-store",
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'",
            **response_headers("Playback", "compatible-copy" if compatible_copy else "original"),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if request.method == "HEAD":
            os.close(descriptor)
            return transfer_matter_response_lease(
                request,
                Response(
                    status_code=status_code,
                    media_type=response_media_type,
                    headers=headers,
                ),
            )
        return transfer_matter_response_lease(
            request,
            StreamingResponse(
                stream(),
                status_code=status_code,
                media_type=response_media_type,
                headers=headers,
            ),
        )

    @app.post(
        "/matters/{slug}/conversations",
        dependencies=[Depends(require_csrf)],
    )
    def new_conversation(request: Request, slug: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            conversation = bench.workspace.create_conversation(
                matter.matter_id,
                actor_id=context.principal_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        audit(
            request,
            "conversation.create",
            "success",
            matter=matter,
            object_type="conversation",
            object_id=conversation.conversation_id,
        )
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(
                {
                    "conversation_id": conversation.conversation_id,
                    "title": conversation.title,
                    "fragment_url": _query_url(
                        f"/matters/{slug}/assistant",
                        conversation=conversation.conversation_id,
                    ),
                    "open_url": _query_url(
                        f"/matters/{slug}",
                        conversation=conversation.conversation_id,
                    )
                    + "#evidence-conversation",
                },
                status_code=201,
            )
        return RedirectResponse(
            _query_url(f"/matters/{slug}", conversation=conversation.conversation_id),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/rename",
        dependencies=[Depends(require_csrf)],
    )
    def rename_conversation(
        request: Request,
        slug: str,
        conversation_id: str,
        title: str = Form(..., max_length=120),
    ):
        try:
            matter = authorized_matter(request, slug)
            conversation = bench.workspace.rename_conversation(
                matter.matter_id, conversation_id, title
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}",
                    conversation=conversation_id,
                    error=str(exc),
                ),
                status_code=303,
            )
        audit(
            request,
            "conversation.rename",
            "success",
            matter=matter,
            object_type="conversation",
            object_id=conversation.conversation_id,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=conversation.conversation_id,
                notice="Conversation renamed",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/pin",
        dependencies=[Depends(require_csrf)],
    )
    def pin_conversation(request: Request, slug: str, conversation_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            conversation = bench.workspace.set_conversation_pinned(
                matter.matter_id,
                conversation_id,
                context.principal_id,
                True,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        audit(
            request,
            "conversation.pin",
            "success",
            context=context,
            matter=matter,
            object_type="conversation",
            object_id=conversation.conversation_id,
            details={"state": "active"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=conversation.conversation_id,
                notice="Conversation pinned",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/unpin",
        dependencies=[Depends(require_csrf)],
    )
    def unpin_conversation(request: Request, slug: str, conversation_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            conversation = bench.workspace.set_conversation_pinned(
                matter.matter_id,
                conversation_id,
                context.principal_id,
                False,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        audit(
            request,
            "conversation.unpin",
            "success",
            context=context,
            matter=matter,
            object_type="conversation",
            object_id=conversation.conversation_id,
            details={"state": "active"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=conversation.conversation_id,
                notice="Conversation unpinned",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/archive",
        dependencies=[Depends(require_csrf)],
    )
    def archive_conversation(request: Request, slug: str, conversation_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            next_conversation = bench.workspace.archive_conversation(
                matter.matter_id,
                conversation_id,
                context.principal_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}",
                    conversation=conversation_id,
                    error=str(exc),
                ),
                status_code=303,
            )
        audit(
            request,
            "conversation.archive",
            "success",
            context=context,
            matter=matter,
            object_type="conversation",
            object_id=conversation_id,
            details={"state": "archived"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=next_conversation.conversation_id,
                notice="Conversation archived",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/restore",
        dependencies=[Depends(require_csrf)],
    )
    def restore_conversation(request: Request, slug: str, conversation_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            conversation = bench.workspace.restore_conversation(
                matter.matter_id,
                conversation_id,
                context.principal_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        audit(
            request,
            "conversation.restore",
            "success",
            context=context,
            matter=matter,
            object_type="conversation",
            object_id=conversation_id,
            details={"state": "active"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=conversation.conversation_id,
                notice="Conversation restored",
            ),
            status_code=303,
        )

    def owner_conversation(
        request: Request, slug: str, conversation_id: str
    ) -> tuple[object, object, object]:
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        membership = bench.workspace.membership(matter.matter_id, context.principal_id)
        if membership.role != "owner":
            audit(
                request,
                "conversation.delete",
                "denied",
                context=context,
                matter=matter,
                object_type="conversation",
                object_id=conversation_id,
            )
            raise HTTPException(
                403, "Only the matter owner can permanently delete conversations."
            )
        conversation = bench.workspace.get_conversation_any(
            matter.matter_id, conversation_id
        )
        return context, matter, conversation

    @app.get(
        "/matters/{slug}/conversations/{conversation_id}/delete",
        response_class=HTMLResponse,
    )
    def delete_conversation_page(
        request: Request,
        slug: str,
        conversation_id: str,
        error: str = Query("", max_length=240),
    ):
        try:
            _context, matter, conversation = owner_conversation(
                request, slug, conversation_id
            )
            counts = bench.workspace.conversation_content_counts(
                matter.matter_id, conversation_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        return templates.TemplateResponse(
            request=request,
            name="workbench_delete_conversation.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "conversation": conversation,
                "counts": counts,
                "error": error,
            },
        )

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/delete",
        dependencies=[Depends(require_csrf)],
    )
    def delete_conversation(
        request: Request,
        slug: str,
        conversation_id: str,
        confirmed_title: str = Form("", max_length=120),
        acknowledge: str = Form("", max_length=8),
    ):
        try:
            context, matter, _conversation = owner_conversation(
                request, slug, conversation_id
            )
            result = bench.workspace.delete_conversation(
                matter.matter_id,
                conversation_id,
                context.principal_id,
                confirmed_title=confirmed_title,
                acknowledged=acknowledge == "yes",
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        except WorkspaceProblem as exc:
            audit(
                request,
                "conversation.delete",
                "failure",
                context=context,
                matter=matter,
                object_type="conversation",
                object_id=conversation_id,
                details={"state": "active"},
            )
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/conversations/{conversation_id}/delete",
                    error=str(exc),
                ),
                status_code=303,
            )
        audit(
            request,
            "conversation.delete",
            "success",
            context=context,
            matter=matter,
            object_type="conversation",
            object_id=conversation_id,
            details={"state": "deleted", "count": result.message_count},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=result.next_conversation_id,
                notice="Conversation permanently deleted",
            ),
            status_code=303,
        )

    from .entity_routes import install_entity_routes
    install_entity_routes(app, service_for=bench.entity_service, discovery_for=bench.entity_discovery,
                          assertions_for=bench.assertion_service,
                          authorized_matter=authorized_matter, auth_context=auth_context,
                          require_csrf=require_csrf, templates=templates,
                          base_context=base_context, audit=audit,
                          require_response_lease=require_matter_response_lease,
                          transfer_response_lease=transfer_matter_response_lease,
                          automatic_progress=bench.automatic_discovery_progress,
                          wake_automatic=bench.wake_automatic_discovery)
    from .assertion_routes import install_assertion_routes
    install_assertion_routes(app, service_for=bench.assertion_service, entities_for=bench.entity_service,
                             authorized_matter=authorized_matter, auth_context=auth_context,
                             require_csrf=require_csrf, templates=templates, base_context=base_context,
                             audit=audit, require_response_lease=require_matter_response_lease,
                             transfer_response_lease=transfer_matter_response_lease)
    from .evidence_graph_routes import install_evidence_graph_routes
    install_evidence_graph_routes(app, assertions_for=bench.assertion_service,
                                  authorized_matter=authorized_matter, auth_context=auth_context,
                                  templates=templates, base_context=base_context)

    def refresh_context_authority(request, slug, original, administrator_override, *, require_administrator=False):
        current = identity.resolve(request.cookies.get(SESSION_COOKIE), read_only=True)
        if current is not None and identity.auth_mode == "kerberos":
            current = identity.bind_kerberos_request(current,
                *kerberos_headers(request.headers))
        try:
            if (current is None or current.principal_id != original.principal_id
                    or not current.principal.active
                    or ((administrator_override or require_administrator) and not current.is_administrator)):
                raise KeyError(original.principal_id)
            return bench.matter(slug, current.principal_id, administrator=administrator_override)
        except KeyError as exc:
            if not bench.workspace.connection.in_transaction:
                audit(request, "matter.access", "denied", context=original)
            raise HTTPException(404, "Matter is no longer available") from exc
    from .matter_context_routes import install_context_routes
    install_context_routes(app, assertions_for=bench.assertion_service, authorized_matter=authorized_matter,
                           auth_context=auth_context, refresh_authority=refresh_context_authority,
                           require_csrf=require_csrf, templates=templates, base_context=base_context)

    @app.get("/matters/{slug}/notebook", response_class=HTMLResponse)
    def matter_notebook(
        request: Request,
        slug: str,
        q: str = Query("", max_length=200),
        item_type: str = Query("", alias="type", max_length=24),
        status: str = Query("", max_length=24),
        page: int = Query(1, ge=1, le=100_000),
        entity_page: int = Query(1, ge=1, le=100_000),
        assertion_page: int = Query(1, ge=1, le=100_000),
        edit: str = Query("", max_length=80),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            read_actor_id = (
                matter.owner_id if administrator_override else context.principal_id
            )
            from .matter_knowledge import MatterKnowledgeService
            knowledge_service = MatterKnowledgeService(bench.assertion_service(matter))
            knowledge = knowledge_service.page(
                matter.matter_id, read_actor_id,
                entity_page=entity_page, assertion_page=assertion_page,
            )
            from .matter_context import MatterContextService
            context_selection, _ = MatterContextService(knowledge_service.assertions).inspect(
                matter.matter_id, read_actor_id)
            notebook_page = bench.workspace.notebook_page(
                matter.matter_id,
                read_actor_id,
                query=q,
                item_type=item_type,
                status=status,
                page=page,
            )
            references = {
                item.item_id: bench.workspace.notebook_references(
                    matter.matter_id, read_actor_id, item.item_id
                )
                for item in notebook_page.items
            }
            available_support_tokens = bench.available_notebook_support_tokens(
                matter,
                tuple(
                    reference
                    for item_references in references.values()
                    for reference in item_references
                ),
            )
            edit_item = (
                bench.workspace.notebook_item(
                    matter.matter_id, read_actor_id, edit
                )
                if edit
                else None
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter notebook item not found") from exc
        except WorkspaceProblem as exc:
            return PlainTextResponse(str(exc), status_code=400)
        if not administrator_override:
            bench.workspace.record_matter_activity(
                matter.matter_id,
                context.principal_id,
                "notebook",
                "notebook",
            )
        audit(
            request,
            "notebook.open",
            "success",
            context=context,
            matter=matter,
            object_type="notebook",
            object_id=matter.matter_id,
            details={"result_count": notebook_page.total},
        )
        def knowledge_url(*, notes=notebook_page.page,
                          identities=knowledge['entities']['page'],
                          assertions=knowledge['assertions']['page'],
                          note_status=notebook_page.status):
            return _query_url(
                f"/matters/{slug}/notebook",
                q=notebook_page.query,
                type=notebook_page.item_type,
                status=note_status,
                page=str(notes) if notes > 1 else "",
                entity_page=str(identities) if identities > 1 else "",
                assertion_page=str(assertions) if assertions > 1 else "",
            )

        current_knowledge_url = knowledge_url()

        def knowledge_record_url(kind, identifier=''):
            path = f'/matters/{slug}/{kind}' + ('/' + identifier if identifier else '')
            return _query_url(path, return_to=current_knowledge_url)

        def knowledge_source_url(token):
            return _query_url(f'/matters/{slug}', support=token,
                              entity_return_to=current_knowledge_url) + '#support-pane'

        response = templates.TemplateResponse(
            request=request,
            name="workbench_notebook.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "notebook": notebook_page,
                "references": references,
                "available_support_tokens": available_support_tokens,
                "edit_item": edit_item,
                "notebook_types": NOTEBOOK_TYPES,
                "notebook_statuses": NOTEBOOK_STATUSES,
                "source_sets": bench.workspace.source_sets(matter.matter_id),
                "notebook_url": lambda target_page: knowledge_url(notes=target_page),
                "notebook_status_url": lambda value: knowledge_url(notes=1, note_status=value),
                "knowledge": knowledge,
                "context_selection": context_selection,
                "knowledge_url": knowledge_url,
                "knowledge_record_url": knowledge_record_url,
                "knowledge_source_url": knowledge_source_url,
                "notice": notice,
                "error": error,
            },
            headers={"Cache-Control": "no-store"},
        )
        # Rendering and source validation may outlive the original access check.
        # Refresh session and provider authority without repeating the access audit.
        try:
            current = identity.resolve(request.cookies.get(SESSION_COOKIE))
            if current is not None and identity.auth_mode == "kerberos":
                current = identity.bind_kerberos_request(
                    current,
                    *kerberos_headers(request.headers),
                )
            if (
                current is None
                or current.principal_id != context.principal_id
                or not current.principal.active
                or (administrator_override and not current.is_administrator)
            ):
                raise KeyError(context.principal_id)
            bench.matter(slug, current.principal_id, administrator=administrator_override)
            with knowledge_service.assertions.repository.transaction(matter.matter_id, read_actor_id):
                pass
        except KeyError as exc:
            audit(request, "matter.access", "denied", context=context)
            raise HTTPException(404, "Matter is no longer available") from exc
        return response

    @app.get("/matters/{slug}/analysis", response_class=HTMLResponse)
    def matter_analysis(
        request: Request,
        slug: str,
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            read_actor_id = (
                matter.owner_id if administrator_override else context.principal_id
            )
            notebook_items = bench.workspace.all_notebook_items(
                matter.matter_id,
                read_actor_id,
                include_dismissed=False,
                limit=2_000,
            )
            timeline = tuple(
                sorted(
                    (
                        item
                        for item in notebook_items
                        if item.item_type in {"date", "event"}
                    ),
                    key=lambda item: (
                        (item.date_label or item.title).casefold(),
                        item.item_id,
                    ),
                )
            )
            entities = tuple(
                item
                for item in notebook_items
                if item.item_type in {"person", "place"}
            )
            notebook_references = {
                item.item_id: bench.workspace.notebook_references(
                    matter.matter_id, read_actor_id, item.item_id
                )
                for item in (*timeline, *entities)
            }
            findings = bench.workspace.review_findings(matter.matter_id)
            finding_references = {
                finding.finding_id: bench.workspace.review_finding_references(
                    matter.matter_id, finding.finding_id
                )
                for finding in findings
            }
            all_references = tuple(
                reference
                for references in (
                    *notebook_references.values(),
                    *finding_references.values(),
                )
                for reference in references
            )
            available_support_tokens = bench.available_notebook_support_tokens(
                matter, all_references
            )
            latest_run = bench.workspace.latest_analysis_run(matter.matter_id)
            source_overview = bench.source_library(matter, view="overview")
        except KeyError as exc:
            raise HTTPException(404, "Matter review map not found") from exc
        if not administrator_override:
            bench.workspace.record_matter_activity(
                matter.matter_id,
                context.principal_id,
                "analysis",
                "review-map",
            )
        audit(
            request,
            "analysis.open",
            "success",
            context=context,
            matter=matter,
            object_type="analysis",
            object_id=matter.matter_id,
            details={"count": len(findings)},
        )
        return templates.TemplateResponse(
            request=request,
            name="workbench_analysis.html",
            context={
                **base_context(request, matter),
                "matter": matter,
                "latest_run": latest_run,
                "timeline": timeline,
                "entities": entities,
                "findings": findings,
                "notebook_references": notebook_references,
                "finding_references": finding_references,
                "available_support_tokens": available_support_tokens,
                "source_stats": source_overview.stats,
                "notice": notice,
                "error": error,
            },
        )

    @app.post(
        "/matters/{slug}/analysis/refresh",
        dependencies=[Depends(require_csrf)],
    )
    def refresh_matter_analysis(request: Request, slug: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            bench.workspace.membership(matter.matter_id, context.principal_id)
            run = bench.run_review_analysis(matter, context.principal_id)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/analysis", error=str(exc)),
                status_code=303,
            )
        except Exception:
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}/analysis",
                    error="The review map could not be refreshed. Existing review decisions are unchanged.",
                ),
                status_code=303,
            )
        audit(
            request,
            "analysis.refresh",
            "success",
            context=context,
            matter=matter,
            object_type="analysis",
            object_id=run.analysis_id,
            details={
                "count": run.finding_count,
                "source_count": run.source_count,
                "capped": bool(run.capped),
            },
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/analysis",
                notice=(
                    "Review map refreshed within bounded scan limits"
                    if run.capped
                    else "Review map refreshed"
                ),
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/analysis/findings/{finding_id}/status",
        dependencies=[Depends(require_csrf)],
    )
    def update_analysis_finding(
        request: Request,
        slug: str,
        finding_id: str,
        status: str = Form(..., max_length=24),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            finding = bench.workspace.update_review_finding_status(
                matter.matter_id,
                finding_id,
                status,
                context.principal_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Review finding not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/analysis", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "analysis.finding_status",
            "success",
            context=context,
            matter=matter,
            object_type="review_finding",
            object_id=finding.finding_id,
            details={"state": finding.status},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/analysis", notice="Review decision saved")
            + f"#{finding.finding_id}",
            status_code=303,
        )

    def report_citation_hrefs(matter: MatterRecord, citations) -> dict[str, str]:
        hrefs: dict[str, str] = {}
        store = bench.source_store(matter)
        for citation in citations:
            hrefs[citation.citation_id] = ""
            if citation.kind == "media_clip":
                try:
                    document = store.get(citation.document_id)
                    if document.version_id != citation.source_version_id:
                        raise KeyError(citation.document_id)
                    token = store.action_token(document)
                    hrefs[citation.citation_id] = (
                        f"/matters/{matter.slug}/sources/{token}?"
                        f"start_ms={citation.start_ms}#transcript-segments"
                    )
                except KeyError:
                    pass
            elif citation.support_token:
                hrefs[citation.citation_id] = (
                    f"/matters/{matter.slug}?support={citation.support_token}#support-pane"
                )
        return hrefs

    def report_edit_recovery(
        request: Request, slug: str, report_id: str, *, error: str,
        mode: str = "info", section_id: str = "",
        draft: Mapping[str, object] | None = None, status_code: int = 409,
    ):
        matter = authorized_matter(request, slug)
        try:
            current_report = bench.workspace.report(matter.matter_id, report_id)
            sections = bench.workspace.report_sections(matter.matter_id, report_id)
        except KeyError:
            current_report, sections = None, ()
            error = "This Report was deleted. Your unsaved text is below." if draft else "This Report is no longer available."
        current_section = next((item for item in sections if item.section_id == section_id), None)
        if mode == "section" and current_report is not None and current_section is None:
            error = "This section was deleted. Your unsaved text is below."
        citations = bench.workspace.report_citations(matter.matter_id, report_id, section_id) if current_section else ()
        # Recheck access before showing current work or a submitted draft.
        matter = authorized_matter(request, slug)
        return templates.TemplateResponse(
            request=request, name="workbench_report_edit_recovery.html",
            context={
                **base_context(request, matter), "matter": matter,
                "report": current_report, "section": current_section,
                "sections": sections, "citations": citations,
                "citation_hrefs": report_citation_hrefs(matter, citations) if citations else {},
                "mode": mode, "draft": draft, "error": error,
                "header_revision": current_report.updated_at if current_report else "",
                "section_revision": current_section.updated_at if current_section else "",
                "can_save": current_report is not None and (mode != "section" or current_section is not None),
            }, status_code=status_code, headers={"Cache-Control": "no-store"},
        )

    report_labels = {"timeline": "Timeline", "entities": "People, places & things", "topic": "Topic brief"}

    def report_work_choices(matter, actor, selected_from=""):
        choices = []
        defaults = []
        if bench.workspace.all_notebook_items(matter.matter_id, actor, include_dismissed=False, limit=1):
            choices.append({"value": "notes:active", "title": "Team review notes", "description": "Your saved observations, people, places, events, and open questions"})
            defaults.append("notes:active")
        research = [item for item in bench.workspace.research_jobs(matter.matter_id, actor)
                    if item.state == "succeeded" and not is_full_text_synthesis(item.plan, item.result)]
        for index, item in enumerate(research[:15]):
            choices.append({"value": f"research:{item.job_id}", "title": item.title, "description": "AI investigation · saved findings and gaps"})
            if index == 0:
                defaults.append(f"research:{item.job_id}")
        for conversation in bench.workspace.conversations(matter.matter_id, include_archived=False)[:15]:
            with bench.workspace._lock:
                found = bench.workspace.connection.execute("SELECT 1 FROM workbench_message WHERE conversation_id=? AND role='assistant' LIMIT 1", (conversation.conversation_id,)).fetchone()
            if found:
                choices.append({"value": f"conversation:{conversation.conversation_id}", "title": conversation.title, "description": "AI conversation · saved answers and team context"})
                if not any(value.startswith("conversation:") for value in defaults):
                    defaults.append(f"conversation:{conversation.conversation_id}")
        for run in bench.workspace.review_runs(matter.matter_id, actor, limit=15):
            if run.state == "succeeded":
                criterion = bench.workspace.review_criterion(matter.matter_id, run.criterion_id)
                choices.append({"value": f"review:{run.run_id}", "title": criterion.title, "description": "Source check · machine screening and human decisions"})
        if selected_from:
            if selected_from not in {item["value"] for item in choices}:
                source, _, identifier = selected_from.partition(":")
                if source == "conversation":
                    record = bench.workspace.get_conversation_any(matter.matter_id, identifier)
                    title = record.title
                elif source == "research":
                    record = bench.workspace.research_job(matter.matter_id, actor, identifier)
                    if is_full_text_synthesis(record.plan, record.result):
                        raise WorkspaceProblem("Export this full-text synthesis directly. Copying it into a Report is not supported yet.")
                    if record.state != "succeeded":
                        raise WorkspaceProblem("That investigation has not finished yet.")
                    title = record.title
                elif source == "note":
                    record = bench.workspace.notebook_item(matter.matter_id, actor, identifier)
                    title = record.title
                elif source == "review":
                    record = bench.workspace.review_run(matter.matter_id, actor, identifier)
                    title = bench.workspace.review_criterion(matter.matter_id, record.criterion_id).title
                else:
                    raise WorkspaceProblem("Choose saved work from this matter.")
                choices.insert(0, {"value": selected_from, "title": title, "description": "The saved work you were reviewing"})
            defaults = [selected_from]
        return choices, defaults

    def render_report_compilation(request, matter, *, job=None, kind="timeline", topic="", selected_from="", selected=None, error="", status_code=200):
        actor = auth_context(request).principal_id
        choices, defaults = report_work_choices(matter, actor, selected_from) if job is None else ([], [])
        return templates.TemplateResponse(request=request, name="workbench_report_compile.html",
            context={**base_context(request, matter), "show_assistant_dock": False,
                     "matter": matter, "active_job": job, "report_labels": report_labels,
                     "kind": kind, "topic": topic, "choices": choices,
                     "selected": defaults if selected is None else selected, "error": error,
                     "request_key": "report-request-" + uuid.uuid4().hex},
            status_code=status_code, headers={"Cache-Control": "no-store"})

    @app.get("/matters/{slug}/reports/new", response_class=HTMLResponse)
    def new_compiled_report(request: Request, slug: str,
                            selected_from: str = Query("", alias="from", max_length=160),
                            job: str = Query("", max_length=100),
                            error: str = Query("", max_length=240)):
        matter = app_context.authorized_matter(request, slug)
        if getattr(request.state, "administrator_matter_override", None) == matter.matter_id:
            raise HTTPException(403, "Report creation requires membership in this matter's review team.")
        actor = app_context.auth_context(request).principal_id
        try:
            active = app_context.bench.report_compilation.jobs.get(matter.matter_id, actor, job) if job else None
            if active and active.state == "succeeded" and active.report_id:
                return RedirectResponse(_query_url(f"/matters/{slug}/reports", report=active.report_id, notice="Your draft is ready to read and refine."), status_code=303)
            return app_context.render_report_compilation(request, matter, job=active, selected_from=selected_from, error=error)
        except KeyError as exc:
            raise HTTPException(404, "Saved review work not found") from exc
        except WorkspaceProblem as exc:
            return app_context.render_report_compilation(request, matter, error=str(exc), status_code=409)

    @app.post("/matters/{slug}/reports/compile", dependencies=[Depends(require_csrf)])
    def compile_matter_report(request: Request, slug: str,
                              kind: str = Form(..., max_length=24), topic: str = Form("", max_length=500),
                              selection: list[str] = Form([]), request_key: str = Form(..., max_length=120)):
        matter = app_context.authorized_matter(request, slug)
        actor = app_context.auth_context(request).principal_id
        try:
            if not 1 <= len(selection) <= 20:
                raise CompilationProblem("Choose between 1 and 20 items of saved work for this draft.")
            job, created = app_context.bench.report_compilation.jobs.queue(matter.matter_id, actor, kind, topic, selection, request_key)
        except (WorkspaceProblem, CompilationProblem) as exc:
            return app_context.render_report_compilation(request, matter, kind=kind, topic=topic, selected=selection, error=str(exc), status_code=409)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        app_context.bench.report_compilation.notify()
        app_context.audit(request, "report.compile", "success", context=app_context.auth_context(request), matter=matter,
              object_type="report_compilation", object_id=job.job_id, details={"state": job.state, "count": len(selection)})
        return RedirectResponse(_query_url(f"/matters/{slug}/reports/new", job=job.job_id), status_code=303)

    @app.get("/matters/{slug}/reports/compile/{job_id}/status")
    def compiled_report_status(request: Request, slug: str, job_id: str):
        matter = app_context.authorized_matter(request, slug)
        try:
            job = app_context.bench.report_compilation.jobs.get(matter.matter_id, app_context.auth_context(request).principal_id, job_id)
        except KeyError as exc:
            raise HTTPException(404, "Report preparation not found") from exc
        target = _query_url(f"/matters/{slug}/reports", report=job.report_id) if job.state == "succeeded" and job.report_id else _query_url(f"/matters/{slug}/reports/new", job=job.job_id)
        message = "This draft was deleted. Retry to create another draft." if job.state == "succeeded" and not job.report_id else job.message
        return JSONResponse({"state": job.state, "message": message,
            "terminal": job.state in {"succeeded", "failed", "cancelled"}, "result_url": target}, headers={"Cache-Control": "no-store"})

    @app.post("/matters/{slug}/reports/compile/{job_id}/cancel", dependencies=[Depends(require_csrf)])
    def cancel_compiled_report(request: Request, slug: str, job_id: str):
        matter = app_context.authorized_matter(request, slug)
        try:
            job = app_context.bench.report_compilation.jobs.cancel(matter.matter_id, app_context.auth_context(request).principal_id, job_id)
        except KeyError as exc:
            raise HTTPException(404, "Report preparation not found") from exc
        app_context.audit(request, "report.cancel", "success", context=app_context.auth_context(request), matter=matter,
              object_type="report_compilation", object_id=job.job_id, details={"state": job.state})
        return RedirectResponse(_query_url(f"/matters/{slug}/reports/new", job=job_id), status_code=303)

    @app.post("/matters/{slug}/reports/compile/{job_id}/retry", dependencies=[Depends(require_csrf)])
    def retry_compiled_report(request: Request, slug: str, job_id: str):
        matter = app_context.authorized_matter(request, slug)
        try:
            job = app_context.bench.report_compilation.jobs.retry(matter.matter_id, app_context.auth_context(request).principal_id, job_id)
        except KeyError as exc:
            raise HTTPException(404, "Report preparation not found") from exc
        except CompilationProblem as exc:
            return app_context.render_report_compilation(request, matter, error=str(exc), status_code=409)
        app_context.audit(request, "report.retry", "success", context=app_context.auth_context(request), matter=matter,
              object_type="report_compilation", object_id=job.job_id, details={"state": job.state})
        app_context.bench.report_compilation.notify()
        return RedirectResponse(_query_url(f"/matters/{slug}/reports/new", job=job_id), status_code=303)

    @app.get("/matters/{slug}/reports", response_class=HTMLResponse)
    def matter_reports(
        request: Request,
        slug: str,
        report: str = Query("", max_length=80),
        edit: bool = Query(False),
        notice: str = Query("", max_length=240),
        error: str = Query("", max_length=240),
    ):
        context = app_context.auth_context(request)
        try:
            matter = app_context.authorized_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            read_actor_id = (
                matter.owner_id if administrator_override else context.principal_id
            )
            reports = app_context.bench.workspace.reports(matter.matter_id, read_actor_id)
            active_report = (
                app_context.bench.workspace.report(matter.matter_id, report)
                if report
                else (reports[0] if reports else None)
            )
            sections = (
                app_context.bench.workspace.report_sections(
                    matter.matter_id, active_report.report_id
                )
                if active_report is not None
                else ()
            )
            citations = {
                section.section_id: app_context.bench.workspace.report_citations(
                    matter.matter_id,
                    active_report.report_id,
                    section.section_id,
                )
                for section in sections
            }
            citation_hrefs = app_context.report_citation_hrefs(
                matter, (citation for values in citations.values() for citation in values)
            )
            store = app_context.bench.source_store(matter)
            notebook_items = app_context.bench.workspace.all_notebook_items(
                matter.matter_id,
                read_actor_id,
                include_dismissed=False,
                limit=250,
            )
            findings = tuple(
                item
                for item in app_context.bench.workspace.review_findings(matter.matter_id)
                if item.status != "dismissed"
            )
            clips: list[dict[str, object]] = []
            for document in store.documents.values():
                if not is_media_type(document.media_type):
                    continue
                for clip in app_context.bench.workspace.media_clips(
                    matter.matter_id, document.document_id, document.version_id
                ):
                    clips.append(
                        {
                            "clip": clip,
                            "source_name": document.display_name,
                            "location": (
                                f"{format_timestamp(clip.start_ms)}–"
                                f"{format_timestamp(clip.end_ms)}"
                            ),
                        }
                    )
            clips.sort(
                key=lambda item: (
                    str(item["source_name"]).casefold(),
                    item["clip"].start_ms,
                )
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter report not found") from exc
        if not administrator_override:
            app_context.bench.workspace.record_matter_activity(
                matter.matter_id,
                context.principal_id,
                "report",
                active_report.report_id if active_report is not None else "reports",
            )
        app_context.audit(
            request,
            "report.open",
            "success",
            context=context,
            matter=matter,
            object_type="report",
            object_id=active_report.report_id if active_report is not None else None,
            details={"count": len(reports)},
        )
        return app_context.templates.TemplateResponse(
            request=request,
            name="workbench_reports.html",
            context={
                **app_context.base_context(request, matter),
                "matter": matter,
                "reports": reports,
                "report": active_report,
                "sections": sections,
                "citations": citations,
                "citation_hrefs": citation_hrefs,
                "notebook_items": notebook_items,
                "findings": findings,
                "clips": tuple(clips[:250]),
                "notice": notice,
                "error": error,
                "report_edit": edit and not administrator_override,
                "report_can_write": not administrator_override,
                "show_assistant_dock": False,
                "report_labels": app_context.report_labels,
                "compilation_jobs": app_context.bench.report_compilation.jobs.list(matter.matter_id, context.principal_id, actionable_only=True) if not administrator_override else (),
            },
        )

    @app.post("/matters/{slug}/reports", dependencies=[Depends(require_csrf)])
    def create_matter_report(
        request: Request,
        slug: str,
        title: str = Form(..., max_length=200),
        purpose: str = Form("", max_length=2_000),
    ):
        context = app_context.auth_context(request)
        try:
            matter = app_context.authorized_matter(request, slug)
            created = app_context.bench.workspace.create_report(
                matter.matter_id, context.principal_id, title, purpose
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/reports", error=str(exc)),
                status_code=303,
            )
        app_context.audit(
            request,
            "report.create",
            "success",
            context=context,
            matter=matter,
            object_type="report",
            object_id=created.report_id,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports",
                report=created.report_id,
                notice="Report draft created",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/reports/{report_id}",
        dependencies=[Depends(require_csrf)],
    )
    def update_matter_report(
        request: Request,
        slug: str,
        report_id: str,
        title: str = Form(..., max_length=200),
        purpose: str = Form("", max_length=2_000),
        status: str = Form("draft", max_length=16),
        expected_updated_at: str = Form("", max_length=64),
    ):
        context = app_context.auth_context(request)
        matter = app_context.authorized_matter(request, slug)
        draft = dict(title=title, purpose=purpose, status=status)
        try:
            updated = app_context.bench.workspace.update_report(
                matter.matter_id,
                report_id,
                context.principal_id,
                expected_updated_at=expected_updated_at,
                title=title,
                purpose=purpose,
                status=status,
            )
        except KeyError:
            return app_context.report_edit_recovery(request, slug, report_id, error="Report unavailable.", mode="header", draft=draft)
        except WorkspaceProblem as exc:
            return app_context.report_edit_recovery(request, slug, report_id, error=str(exc), mode="header", draft=draft,
                                        status_code=409 if isinstance(exc, ReportEditConflict) else 400)
        app_context.audit(
            request,
            "report.update",
            "success",
            context=context,
            matter=matter,
            object_type="report",
            object_id=updated.report_id,
            details={"state": updated.status},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports",
                report=report_id,
                notice="Report details saved",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/reports/{report_id}/sections",
        dependencies=[Depends(require_csrf)],
    )
    def add_manual_report_section(
        request: Request,
        slug: str,
        report_id: str,
        heading: str = Form(..., max_length=200),
        body: str = Form("", max_length=50_000),
        expected_status: str = Form("", max_length=16),
    ):
        context = app_context.auth_context(request)
        matter = app_context.authorized_matter(request, slug)
        draft = dict(heading=heading, body=body)
        try:
            section = app_context.bench.workspace.add_report_section(
                matter.matter_id,
                report_id,
                context.principal_id,
                expected_status=expected_status,
                heading=heading,
                body=body,
            )
        except KeyError:
            return app_context.report_edit_recovery(request, slug, report_id, error="Report unavailable.", mode="append", draft=draft)
        except WorkspaceProblem as exc:
            return app_context.report_edit_recovery(request, slug, report_id, error=str(exc), mode="append", draft=draft,
                                        status_code=409 if isinstance(exc, ReportEditConflict) else 400)
        app_context.audit(
            request,
            "report.section_add",
            "success",
            context=context,
            matter=matter,
            object_type="report_section",
            object_id=section.section_id,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports",
                report=report_id,
                notice="Section added",
            )
            + f"#{section.section_id}",
            status_code=303,
        )

    def add_report_material(
        request: Request,
        slug: str,
        report_id: str,
        origin: str,
        origin_id: str,
        expected_status: str,
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            if origin == "notebook":
                section = bench.add_notebook_item_to_report(
                    matter, context.principal_id, report_id, origin_id, expected_status=expected_status
                )
            elif origin == "finding":
                section = bench.add_finding_to_report(
                    matter, context.principal_id, report_id, origin_id, expected_status=expected_status
                )
            elif origin == "media_clip":
                section = bench.add_media_clip_to_report(
                    matter, context.principal_id, report_id, origin_id, expected_status=expected_status
                )
            else:
                raise KeyError(origin_id)
        except KeyError as exc:
            raise HTTPException(404, "Report material not found") from exc
        except WorkspaceProblem as exc:
            return report_edit_recovery(request, slug, report_id, error=str(exc),
                                        status_code=409 if isinstance(exc, ReportEditConflict) else 400)
        audit(
            request,
            "report.material_add",
            "success",
            context=context,
            matter=matter,
            object_type="report_section",
            object_id=section.section_id,
            details={"type": origin},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports",
                report=report_id,
                notice="Cited material added to report",
            )
            + f"#{section.section_id}",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/reports/{report_id}/from-notebook/{item_id}",
        dependencies=[Depends(require_csrf)],
    )
    def add_notebook_report_material(
        request: Request, slug: str, report_id: str, item_id: str,
        expected_status: str = Form("", max_length=16),
    ):
        return app_context.add_report_material(request, slug, report_id, "notebook", item_id, expected_status)

    @app.post(
        "/matters/{slug}/reports/{report_id}/from-finding/{finding_id}",
        dependencies=[Depends(require_csrf)],
    )
    def add_finding_report_material(
        request: Request, slug: str, report_id: str, finding_id: str,
        expected_status: str = Form("", max_length=16),
    ):
        return app_context.add_report_material(request, slug, report_id, "finding", finding_id, expected_status)

    @app.post(
        "/matters/{slug}/reports/{report_id}/from-clip/{clip_id}",
        dependencies=[Depends(require_csrf)],
    )
    def add_clip_report_material(
        request: Request, slug: str, report_id: str, clip_id: str,
        expected_status: str = Form("", max_length=16),
    ):
        return app_context.add_report_material(request, slug, report_id, "media_clip", clip_id, expected_status)

    @app.post(
        "/matters/{slug}/reports/{report_id}/from-answer/{conversation_id}/{message_id}",
        dependencies=[Depends(require_csrf)],
    )
    def add_answer_report_material(
        request: Request,
        slug: str,
        report_id: str,
        conversation_id: str,
        message_id: str,
        expected_status: str = Form("", max_length=16),
    ):
        context = app_context.auth_context(request)
        try:
            matter = app_context.authorized_matter(request, slug)
            section = app_context.bench.add_answer_to_report(
                matter,
                context.principal_id,
                report_id,
                conversation_id,
                message_id,
                expected_status=expected_status,
            )
        except KeyError as exc:
            raise HTTPException(404, "Answer or report not found") from exc
        except WorkspaceProblem as exc:
            return app_context.report_edit_recovery(request, slug, report_id, error=str(exc),
                                        status_code=409 if isinstance(exc, ReportEditConflict) else 400)
        app_context.audit(
            request,
            "report.answer_add",
            "success",
            context=context,
            matter=matter,
            object_type="report_section",
            object_id=section.section_id,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports",
                report=report_id,
                notice="Answer and its current citations added to report",
            )
            + f"#{section.section_id}",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/reports/{report_id}/sections/{section_id}",
        dependencies=[Depends(require_csrf)],
    )
    def update_matter_report_section(
        request: Request,
        slug: str,
        report_id: str,
        section_id: str,
        heading: str = Form(..., max_length=200),
        body: str = Form("", max_length=50_000),
        expected_updated_at: str = Form("", max_length=64),
        expected_status: str = Form("", max_length=16),
    ):
        context = app_context.auth_context(request)
        matter = app_context.authorized_matter(request, slug)
        draft = dict(heading=heading, body=body)
        try:
            section = app_context.bench.workspace.update_report_section(
                matter.matter_id,
                report_id,
                section_id,
                context.principal_id,
                expected_updated_at=expected_updated_at,
                expected_status=expected_status,
                heading=heading,
                body=body,
            )
        except KeyError:
            return app_context.report_edit_recovery(request, slug, report_id, error="Report unavailable.", mode="section", draft=draft, section_id=section_id)
        except WorkspaceProblem as exc:
            return app_context.report_edit_recovery(request, slug, report_id, error=str(exc), mode="section", draft=draft, section_id=section_id,
                                        status_code=409 if isinstance(exc, ReportEditConflict) else 400)
        app_context.audit(
            request,
            "report.section_update",
            "success",
            context=context,
            matter=matter,
            object_type="report_section",
            object_id=section.section_id,
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports", report=report_id, notice="Section saved"
            )
            + f"#{section.section_id}",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/reports/{report_id}/sections/{section_id}/move",
        dependencies=[Depends(require_csrf)],
    )
    def move_matter_report_section(
        request: Request,
        slug: str,
        report_id: str,
        section_id: str,
        direction: str = Form(..., max_length=8),
        expected_updated_at: str = Form("", max_length=64),
    ):
        context = app_context.auth_context(request)
        try:
            matter = app_context.authorized_matter(request, slug)
            app_context.bench.workspace.move_report_section(
                matter.matter_id,
                report_id,
                section_id,
                context.principal_id,
                direction,
                expected_updated_at=expected_updated_at,
            )
        except KeyError as exc:
            raise HTTPException(404, "Report section not found") from exc
        except WorkspaceProblem as exc:
            return app_context.report_edit_recovery(request, slug, report_id, error=str(exc),
                                        status_code=409 if isinstance(exc, ReportEditConflict) else 400)

        return RedirectResponse(
            _query_url(f"/matters/{slug}/reports", report=report_id)
            + f"#{section_id}",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/reports/{report_id}/sections/{section_id}/delete",
        dependencies=[Depends(require_csrf)],
    )
    def delete_matter_report_section(
        request: Request, slug: str, report_id: str, section_id: str,
        expected_updated_at: str = Form("", max_length=64),
        expected_status: str = Form("", max_length=16),
    ):
        context = app_context.auth_context(request)
        try:
            matter = app_context.authorized_matter(request, slug)
            app_context.bench.workspace.delete_report_section(
                matter.matter_id,
                report_id,
                section_id,
                context.principal_id,
                expected_updated_at=expected_updated_at,
                expected_status=expected_status,
            )
        except KeyError as exc:
            raise HTTPException(404, "Report section not found") from exc
        except WorkspaceProblem as exc:
            return app_context.report_edit_recovery(request, slug, report_id, error=str(exc))
        app_context.audit(
            request,
            "report.section_delete",
            "success",
            context=context,
            matter=matter,
            object_type="report_section",
            object_id=section_id,
            details={"state": "deleted"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/reports",
                report=report_id,
                notice="Section removed",
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/reports/{report_id}/delete",
        dependencies=[Depends(require_csrf)],
    )
    def delete_matter_report(
        request: Request, slug: str, report_id: str,
        expected_updated_at: str = Form("", max_length=64),
    ):
        context = app_context.auth_context(request)
        try:
            matter = app_context.authorized_matter(request, slug)
            deleted = app_context.bench.workspace.delete_report(
                matter.matter_id, report_id, context.principal_id, expected_updated_at=expected_updated_at
            )
        except KeyError as exc:
            raise HTTPException(404, "Report not found") from exc
        except WorkspaceProblem as exc:
            return app_context.report_edit_recovery(request, slug, report_id, error=str(exc))
        app_context.audit(
            request,
            "report.delete",
            "success",
            context=context,
            matter=matter,
            object_type="report",
            object_id=deleted.report_id,
            details={"state": "deleted"},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/reports", notice="Report deleted"),
            status_code=303,
        )

    @app.get(
        "/matters/{slug}/reports/{report_id}/export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def export_matter_report(
        request: Request,
        slug: str,
        report_id: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|markdown)$"),
    ):
        context = app_context.auth_context(request)
        try:
            matter = app_context.response_lease_matter(request, slug)
            report = app_context.bench.workspace.report(matter.matter_id, report_id)
            sections = app_context.bench.workspace.report_sections(matter.matter_id, report_id)
            entries = tuple(
                (
                    section,
                    app_context.bench.workspace.report_citations(
                        matter.matter_id, report_id, section.section_id
                    ),
                )
                for section in sections
            )
            artifact = app_context.bench.export_report_work_product(
                matter, report, entries, format_name
            )
        except KeyError as exc:
            raise HTTPException(404, "Report not found") from exc
        except ExportProblem as exc:
            return PlainTextResponse(str(exc), status_code=400)
        app_context.audit(
            request,
            "report.export",
            "success",
            context=context,
            matter=matter,
            object_type="report",
            object_id=report.report_id,
            details={"format": format_name, "count": len(entries)},
        )
        return app_context.download_response(request, artifact)

    @app.post(
        "/matters/{slug}/notebook/items",
        dependencies=[Depends(require_csrf)],
    )
    def create_notebook_item(
        request: Request,
        slug: str,
        item_type: str = Form("note", max_length=24),
        status: str = Form("needs_review", max_length=24),
        title: str = Form(..., max_length=160),
        body: str = Form("", max_length=20_000),
        date_label: str = Form("", max_length=100),
        pinned: str = Form("", max_length=8),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            item, _created = bench.workspace.create_notebook_item(
                matter.matter_id,
                context.principal_id,
                item_type=item_type,
                status=status,
                title=title,
                body=body,
                date_label=date_label,
                pinned=pinned == "yes",
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/notebook", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "notebook.item_create",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item.item_id,
            details={"state": item.status},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/notebook", notice="Notebook item saved")
            + f"#{item.item_id}",
            status_code=303,
        )

    def notebook_edit_recovery(
        request: Request, slug: str, item_id: str, *, error: str,
        draft: Mapping[str, object] | None = None, status_code: int = 409,
    ):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        try:
            current = bench.workspace.notebook_item(matter.matter_id, context.principal_id, item_id)
            references = bench.workspace.notebook_references(matter.matter_id, context.principal_id, item_id)
        except KeyError:
            # Recheck matter authority before retaining the submitted draft for
            # a deleted note. Never recreate a deleted item or its source links.
            matter = authorized_matter(request, slug)
            current, references = None, ()
            error = "This case note was deleted. Your unsaved text is below." if draft else "This case note is no longer available."
        return templates.TemplateResponse(
            request=request, name="workbench_notebook_edit_recovery.html",
            context={
                **base_context(request, matter), "matter": matter,
                "current_item": current, "edit_item": draft,
                "edit_revision": current.updated_at if current else "",
                "can_save": current is not None, "error": error,
                "notebook_types": NOTEBOOK_TYPES, "notebook_statuses": NOTEBOOK_STATUSES,
                "references": references,
                "available_support_tokens": bench.available_notebook_support_tokens(matter, references),
            },
            status_code=status_code, headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/matters/{slug}/notebook/items/{item_id}",
        dependencies=[Depends(require_csrf)],
    )
    def update_notebook_item(
        request: Request,
        slug: str,
        item_id: str,
        item_type: str = Form(..., max_length=24),
        status: str = Form(..., max_length=24),
        title: str = Form(..., max_length=160),
        body: str = Form("", max_length=20_000),
        date_label: str = Form("", max_length=100),
        pinned: str = Form("", max_length=8),
        expected_updated_at: str = Form("", max_length=64),
    ):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        draft = dict(item_id=item_id, item_type=item_type, status=status, title=title,
                     body=body, date_label=date_label, is_pinned=pinned == "yes")
        try:
            item = bench.workspace.update_notebook_item(
                matter.matter_id,
                context.principal_id,
                item_id,
                item_type=item_type,
                status=status,
                title=title,
                body=body,
                date_label=date_label,
                pinned=pinned == "yes",
                expected_updated_at=expected_updated_at,
            )
        except KeyError:
            return notebook_edit_recovery(request, slug, item_id, error="Case note unavailable.", draft=draft)
        except WorkspaceProblem as exc:
            return notebook_edit_recovery(
                request, slug, item_id, error=str(exc), draft=draft,
                status_code=409 if isinstance(exc, NotebookEditConflict) else 400,
            )
        audit(
            request,
            "notebook.item_update",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item.item_id,
            details={"state": item.status},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/notebook", notice="Notebook item updated")
            + f"#{item.item_id}",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/notebook/items/{item_id}/status",
        dependencies=[Depends(require_csrf)],
    )
    def update_notebook_item_status(
        request: Request,
        slug: str,
        item_id: str,
        status: str = Form(..., max_length=24),
        expected_updated_at: str = Form("", max_length=64),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            item = bench.workspace.set_notebook_item_status(
                matter.matter_id, context.principal_id, item_id, status,
                expected_updated_at=expected_updated_at
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter notebook item not found") from exc
        except WorkspaceProblem as exc:
            return notebook_edit_recovery(
                request, slug, item_id, error=str(exc),
                status_code=409 if isinstance(exc, NotebookEditConflict) else 400,
            )
        audit(
            request,
            "notebook.item_review",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item.item_id,
            details={"state": item.status},
        )
        return RedirectResponse(
            _query_url(f"/matters/{slug}/notebook", notice="Review status updated")
            + f"#{item.item_id}",
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/notebook/items/{item_id}/delete",
        dependencies=[Depends(require_csrf)],
    )
    def delete_notebook_item(
        request: Request, slug: str, item_id: str,
        expected_updated_at: str = Form("", max_length=64),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            item = bench.workspace.delete_notebook_item(
                matter.matter_id, context.principal_id, item_id,
                expected_updated_at=expected_updated_at
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter notebook item not found") from exc
        except WorkspaceProblem as exc:
            return notebook_edit_recovery(request, slug, item_id, error=str(exc))
        audit(
            request,
            "notebook.item_delete",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item.item_id,
            details={"state": "deleted"},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/notebook", notice="Notebook item permanently deleted"
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/notebook/suggestions",
        dependencies=[Depends(require_csrf)],
    )
    def find_notebook_suggestions(
        request: Request,
        slug: str,
        source_set: str = Form("", max_length=80),
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            result = bench.suggest_notebook_references(
                matter,
                context.principal_id,
                source_set_id=source_set or None,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or source set not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}/notebook", error=str(exc)),
                status_code=303,
            )
        audit(
            request,
            "notebook.suggest",
            "success",
            context=context,
            matter=matter,
            object_type="notebook",
            object_id=matter.matter_id,
            details={"count": int(result["created"]), "state": "suggested"},
        )
        notice_value = (
            f"Added {result['created']} new review suggestion(s) from "
            f"{result['scanned_units']} source section(s)"
        )
        if result["capped"]:
            notice_value += "; this bounded pass can be run again after review"
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/notebook", status="suggested", notice=notice_value
            ),
            status_code=303,
        )

    @app.post(
        "/matters/{slug}/notebook/from-support/{token}",
        dependencies=[Depends(require_csrf)],
    )
    def save_support_notebook(request: Request, slug: str, token: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            item, created = bench.save_support_to_notebook(
                matter, context.principal_id, token
            )
        except KeyError as exc:
            raise HTTPException(404, "Supporting source is unavailable") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(f"/matters/{slug}", support=token,
                           error=f"{exc} The source remains available; review it before trying another save."),
                status_code=303,
            )
        audit(
            request,
            "notebook.capture_citation",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item.item_id,
            details={"created": created, "state": item.status},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}/notebook",
                notice="Citation saved to the notebook" if created else "Citation was already in the notebook",
            )
            + f"#{item.item_id}",
            status_code=303,
        )

    @app.get("/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/cited-context/{passage}/{citation_index}",
             dependencies=[Depends(require_matter_response_lease)])
    def answer_cited_context(
        request: Request, slug: str, conversation_id: str, message_id: str,
        passage: str, citation_index: int,
        format_name: str = Query("html", alias="format", pattern="^(html|json)$"),
    ):
        from .answer_cited_context import saved_cited_context

        context = auth_context(request)
        matter = response_lease_matter(request, slug)
        administrator_override = getattr(request.state, "administrator_matter_override", None) == matter.matter_id
        with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
            refresh_context_authority(request, slug, context, administrator_override)
            try:
                comparison = saved_cited_context(bench, matter, conversation_id,
                    message_id, passage, citation_index)
            except KeyError as exc:
                raise HTTPException(404, "Saved answer citation not found") from exc
            if format_name == "json":
                response = JSONResponse(comparison, headers={"Cache-Control": "no-store"})
            else:
                response = templates.TemplateResponse(request=request,
                    name="workbench_cited_context.html", context={
                        **base_context(request, matter, include_assistant=False), "matter": matter,
                        "comparison": comparison,
                        "answer_href": (_query_url(f"/matters/{slug}", conversation=conversation_id)
                            + f"#answer-support-{message_id}"),
                    }, headers={"Cache-Control": "no-store"})
            # HTML navigation also uses the original administrator role, even
            # when target-matter access was granted through ordinary membership.
            refresh_context_authority(request, slug, context, administrator_override,
                require_administrator=format_name == "html" and context.is_administrator)
            return transfer_matter_response_lease(request, response, through_send=True)

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/notebook/claims/{claim_index}",
        dependencies=[Depends(require_csrf)],
    )
    def save_answer_claim_notebook(
        request: Request,
        slug: str,
        conversation_id: str,
        message_id: str,
        claim_index: int,
        return_to: str = Form("", max_length=4000),
    ):
        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        return_path = _source_review_return_href(slug, return_to)

        def return_with_feedback(message: str, *, error: bool = False):
            fallback = _query_url(f"/matters/{slug}", conversation=conversation_id)
            if not error:
                fallback += "#latest"
            parsed = urlparse(return_path or fallback)
            query = parse_qs(parsed.query, keep_blank_values=True)
            query.pop("error", None)
            query.pop("notice", None)
            query["error" if error else "notice"] = [message]
            return RedirectResponse(parsed._replace(query=urlencode(query, doseq=True)).geturl(), status_code=303)
        try:
            matter = authorized_matter(request, slug)
            item, created = bench.save_answer_to_notebook(
                matter,
                context.principal_id,
                conversation_id,
                message_id,
                claim_index,
            )
        except KeyError as exc:
            raise HTTPException(404, "Saved answer passage not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409,
                                    headers={"Cache-Control": "no-store"})
            return return_with_feedback(str(exc), error=True)
        audit(
            request,
            "notebook.capture_answer",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item.item_id,
            details={"created": created, "state": item.status},
        )
        notice = ("Passage saved to case notes for review." if created
                  else "That passage was already saved to case notes.")
        if wants_json:
            return JSONResponse({"message": notice, "created": created,
                                 "item_id": item.item_id, "status": item.status},
                                headers={"Cache-Control": "no-store"})
        return return_with_feedback(notice)

    def _answer_feedback(slug: str, conversation_id: str, return_path: str | None, message: str, *, error: bool = False):
        fallback = _query_url(f"/matters/{slug}", conversation=conversation_id)
        if not error:
            fallback += "#latest"
        parsed = urlparse(return_path or fallback)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query.pop("error", None)
        query.pop("notice", None)
        query["error" if error else "notice"] = [message]
        return RedirectResponse(parsed._replace(query=urlencode(query, doseq=True)).geturl(), status_code=303)

    suggestion_kind_labels = {"person": "Person", "organization": "Organization", "thing": "Thing",
                              "identifier": "Identifier", "date": "Date"}

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/suggestions/claims/{claim_index}/preview",
        dependencies=[Depends(require_csrf)],
    )
    def preview_answer_claim_suggestions(
        request: Request,
        slug: str,
        conversation_id: str,
        message_id: str,
        claim_index: int,
        return_to: str = Form("", max_length=4000),
    ):
        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        return_path = _source_review_return_href(slug, return_to)
        try:
            matter = authorized_matter(request, slug)
            preview = bench.preview_answer_claim_suggestions(
                matter, context.principal_id, conversation_id, message_id, claim_index)
        except KeyError as exc:
            raise HTTPException(404, "Saved answer passage not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"})
            return _answer_feedback(slug, conversation_id, return_path, str(exc), error=True)
        items = [{**item, "kind_label": suggestion_kind_labels.get(item["kind"], item["kind"].title())}
                 for item in preview["items"]]
        count = sum(item["count"] for item in items)
        if count:
            message = f"{count} new suggestion{'' if count == 1 else 's'} found. Nothing is added until you choose Add."
        else:
            message = "No new people, things or dates: they are already suggested, or none were found."
        if preview["failed"]:
            message += " Some passages could not be read; try again."
        save_url = (f"/matters/{slug}/conversations/{conversation_id}/messages/{message_id}"
                    f"/suggestions/claims/{claim_index}")
        if wants_json:
            return JSONResponse({"message": message, "items": items, "count": count,
                                 "passages": preview["passages"], "basis": preview["basis"],
                                 "save_url": save_url}, headers={"Cache-Control": "no-store"})
        if not count:
            return _answer_feedback(slug, conversation_id, return_path, message)
        response = templates.TemplateResponse(
            request=request,
            name="workbench_suggestion_preview.html",
            context={**base_context(request, matter), "matter": matter, "items": items, "message": message,
                     "count": count, "passages": preview["passages"],
                     "basis": preview["basis"], "save_url": save_url, "return_to": return_path or "",
                     "back_url": return_path or _query_url(f"/matters/{slug}", conversation=conversation_id) + "#latest"},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/suggestions/claims/{claim_index}",
        dependencies=[Depends(require_csrf)],
    )
    def suggest_from_answer_claim(
        request: Request,
        slug: str,
        conversation_id: str,
        message_id: str,
        claim_index: int,
        basis: str = Form("", max_length=64),
        return_to: str = Form("", max_length=4000),
    ):
        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        return_path = _source_review_return_href(slug, return_to)
        try:
            if not re.fullmatch(r"[0-9a-f]{64}", basis):
                raise WorkspaceProblem("Review the suggestions before adding them.")
            matter = authorized_matter(request, slug)

            def record(added: int, passages: int) -> None:
                # Inside the suggestion transaction (same connection, re-entrant
                # lock), so the audit event commits or rolls back with the writes.
                bench.workspace._append_audit_event_locked(
                    actor_principal_id=context.principal_id,
                    session_id=context.session.session_id if context.session is not None else None,
                    matter_id=matter.matter_id,
                    request_id=getattr(request.state, "request_id", f"request-{uuid.uuid4().hex}"),
                    action="entity.suggest_from_answer", outcome="success",
                    object_type="message", object_id=message_id,
                    details={"count": int(added), "unit_count": int(passages), "state": "suggested"})
            result = bench.suggest_from_answer_claim(
                matter, context.principal_id, conversation_id, message_id, claim_index, basis, record=record)
        except KeyError as exc:
            raise HTTPException(404, "Saved answer passage not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409,
                                    headers={"Cache-Control": "no-store"})
            return _answer_feedback(slug, conversation_id, return_path, str(exc), error=True)
        added, dates = int(result["added"]), int(result["dates"])
        if added:
            notice = f"Added {added} suggestion{'' if added == 1 else 's'} for review"
            if dates:
                notice += f", including {dates} date{'' if dates == 1 else 's'}"
            notice += "."
        else:
            notice = "No new people, things or dates: they are already suggested, or none were found."
        if result["failed"]:
            notice += " Some passages could not be read; try again."
        if wants_json:
            return JSONResponse({
                "message": notice, "added": added, "dates": dates,
                "inbox_url": f"/matters/{slug}/entities#suggestions",
                "timeline_url": f"/matters/{slug}/chronology#found-dates-heading" if dates else "",
            }, headers={"Cache-Control": "no-store"})
        return _answer_feedback(slug, conversation_id, return_path, notice)

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/notebook/claims/{claim_index}/citations/{citation_index}",
        dependencies=[Depends(require_csrf)],
    )
    def save_answer_citation_notebook(
        request: Request,
        slug: str,
        conversation_id: str,
        message_id: str,
        claim_index: int,
        citation_index: int,
    ):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            item, created = bench.save_answer_to_notebook(
                matter,
                context.principal_id,
                conversation_id,
                message_id,
                claim_index,
                citation_index=citation_index,
            )
        except KeyError as exc:
            raise HTTPException(404, "Saved answer citation not found") from exc
        except WorkspaceProblem as exc:
            return RedirectResponse(
                _query_url(
                    f"/matters/{slug}", conversation=conversation_id, error=str(exc)
                ),
                status_code=303,
            )
        audit(
            request,
            "notebook.capture_citation",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item.item_id,
            details={"created": created, "state": item.status},
        )
        return RedirectResponse(
            _query_url(
                f"/matters/{slug}",
                conversation=conversation_id,
                notice="Citation saved to the notebook" if created else "That citation was already saved",
            )
            + "#latest",
            status_code=303,
        )

    @app.get(
        "/matters/{slug}/notebook/export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def download_notebook(
        request: Request,
        slug: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|markdown|csv)$"),
    ):
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            entries = notebook_export_entries(
                matter,
                context.principal_id,
                administrator_override=administrator_override,
            )
            artifact = export_notebook(matter, entries, format_name)
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        except ExportProblem as exc:
            return PlainTextResponse(str(exc), status_code=409)
        audit(
            request,
            "work_product.export",
            "success",
            context=context,
            matter=matter,
            object_type="notebook",
            object_id=matter.matter_id,
            details={"kind": "notebook", "format": format_name},
        )
        return download_response(request, artifact)

    @app.get(
        "/matters/{slug}/notebook/items/{item_id}/export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def download_notebook_item(
        request: Request,
        slug: str,
        item_id: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|markdown)$"),
    ):
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            entries = notebook_export_entries(
                matter,
                context.principal_id,
                item_id=item_id,
                administrator_override=administrator_override,
            )
            artifact = export_notebook(
                matter,
                entries,
                format_name,
                heading=entries[0][0].title,
                stem=f"{matter.display_name}-{entries[0][0].title}",
            )
        except (KeyError, IndexError) as exc:
            raise HTTPException(404, "Matter notebook item not found") from exc
        except ExportProblem as exc:
            return PlainTextResponse(str(exc), status_code=409)
        audit(
            request,
            "work_product.export",
            "success",
            context=context,
            matter=matter,
            object_type="notebook_item",
            object_id=item_id,
            details={"kind": "notebook_item", "format": format_name},
        )
        return download_response(request, artifact)

    @app.get(
        "/matters/{slug}/conversations/{conversation_id}/export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def download_conversation(
        request: Request,
        slug: str,
        conversation_id: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|markdown)$"),
    ):
        try:
            matter = response_lease_matter(request, slug)
            conversation = bench.workspace.get_conversation_any(
                matter.matter_id, conversation_id
            )
            messages = bench.workspace.messages(matter.matter_id, conversation_id)
            artifact = export_conversation(
                matter, conversation, messages, format_name,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        except ExportProblem as exc:
            return PlainTextResponse(str(exc), status_code=409)
        audit(
            request,
            "work_product.export",
            "success",
            matter=matter,
            object_type="conversation",
            object_id=conversation.conversation_id,
            details={"kind": "conversation", "format": format_name},
        )
        return download_response(request, artifact)

    @app.get(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/export",
        dependencies=[Depends(require_matter_response_lease)],
    )
    def download_answer(
        request: Request,
        slug: str,
        conversation_id: str,
        message_id: str,
        format_name: str = Query("docx", alias="format", pattern="^(docx|markdown)$"),
    ):
        try:
            matter = response_lease_matter(request, slug)
            conversation = bench.workspace.get_conversation_any(
                matter.matter_id, conversation_id
            )
            messages = bench.workspace.messages(matter.matter_id, conversation_id)
            answer_index = next(
                index
                for index, message in enumerate(messages)
                if message.message_id == message_id and message.role == "assistant"
            )
            answer = messages[answer_index]
            question = next(
                (
                    message
                    for message in reversed(messages[:answer_index])
                    if message.role == "user"
                ),
                None,
            )
            artifact = export_answer(
                matter, conversation, answer, question, format_name,
            )
        except (KeyError, StopIteration) as exc:
            raise HTTPException(404, "Saved answer not found") from exc
        except ExportProblem as exc:
            return PlainTextResponse(str(exc), status_code=409)
        audit(
            request,
            "work_product.export",
            "success",
            matter=matter,
            object_type="message",
            object_id=answer.message_id,
            details={"kind": "answer", "format": format_name},
        )
        return download_response(request, artifact)

    def prepare_matter_work_product(
        request: Request, slug: str, *, report_issues: list[dict[str, str]] | None = None,
    ) -> ExportArtifact:
        context = auth_context(request)
        try:
            matter = response_lease_matter(request, slug)
            administrator_override = (
                getattr(request.state, "administrator_matter_override", None)
                == matter.matter_id
            )
            read_actor_id = context.principal_id
            source_catalog = bench.workspace.source_catalog_for_export(
                matter.matter_id,
                read_actor_id,
                administrator_override=administrator_override,
            )
            lifecycle = bench.workspace.matter_lifecycle(matter.matter_id)
            frozen_source_catalog: tuple[SourceCatalogRecord, ...] | None = None
            if lifecycle.state == "purge_failed":
                bench.assert_frozen_final_bundle_source_state(
                    matter, lifecycle, source_catalog
                )
                frozen_source_catalog = source_catalog
            conversations = tuple(
                (
                    conversation,
                    bench.workspace.messages(
                        matter.matter_id, conversation.conversation_id
                    ),
                )
                for conversation in bench.workspace.conversations(matter.matter_id)
            )
            additional_work_product: list[dict[str, object]] = []
            additional_work_product_bytes = 0
            saved_report_files: list[dict[str, object]] = []
            exported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            def add_work_product(
                *, kind: str, path: str, artifact: ExportArtifact
            ) -> None:
                nonlocal additional_work_product_bytes
                projected = additional_work_product_bytes + len(artifact.body)
                if projected > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                    raise ExportProblem(
                        "This bundle is too large to prepare at once. Export completed work individually."
                    )
                additional_work_product.append(
                    {"kind": kind, "path": path, "body": artifact.body}
                )
                additional_work_product_bytes = projected

            saved_reports = bench.workspace.reports_for_final_bundle(
                matter.matter_id,
                read_actor_id,
                maximum=MAX_FINAL_BUNDLE_REPORTS,
                maximum_rows=MAX_FINAL_BUNDLE_REPORT_ROWS,
                maximum_bytes=MAX_FINAL_BUNDLE_REPORT_BYTES,
                administrator_override=administrator_override,
            )
            for report, sections in saved_reports:
                files: dict[str, object] = {
                    "matter_id": matter.matter_id,
                    "title": report.title,
                    "status": report.status,
                    "section_count": len(sections),
                    "updated_at": report.updated_at,
                }
                formats = ("markdown", "docx")
                try:
                    report_artifacts = bench.export_report_work_products(
                        matter, report, sections, formats,
                        frozen_source_catalog=frozen_source_catalog,
                        exported_at=exported_at,
                    )
                except ExportProblem as exc:
                    if report_issues is None:
                        raise
                    report_issues.append({
                        "title": report.title,
                        "url": "" if frozen_source_catalog is not None else _query_url(
                            f"/matters/{matter.slug}/reports", report=report.report_id,
                            error="This Report needs attention before export. Review its source support and saved work."),
                        "message": str(exc),
                    })
                    continue
                for format_name, report_artifact in zip(formats, report_artifacts, strict=True):
                    additional_work_product_bytes += len(report_artifact.body)
                    if additional_work_product_bytes > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                        raise ExportProblem(
                            "No complete bundle was created because it is too large. "
                            "Download Reports individually before closing this matter."
                        )
                    files[format_name] = report_artifact.body
                saved_report_files.append(files)

            if report_issues:
                raise ExportProblem("Review the listed Reports, then check the complete bundle again.")

            research_jobs = bench.workspace.succeeded_research_jobs_for_final_bundle(
                matter.matter_id,
                read_actor_id,
                maximum=MAX_FINAL_BUNDLE_LEDGER_ITEMS,
                administrator_override=administrator_override,
            )
            if len(research_jobs) > MAX_FINAL_BUNDLE_LEDGER_ITEMS:
                raise ExportProblem(
                    "No final bundle was created because this matter has more than "
                    f"{MAX_FINAL_BUNDLE_LEDGER_ITEMS:,} completed investigation "
                    "ledgers and omitting saved work is not permitted."
                )
            review_runs = bench.workspace.review_runs_for_final_bundle(
                matter.matter_id,
                read_actor_id,
                maximum=MAX_FINAL_BUNDLE_LEDGER_ITEMS,
                administrator_override=administrator_override,
            )
            if len(review_runs) > MAX_FINAL_BUNDLE_LEDGER_ITEMS:
                raise ExportProblem(
                    "No final bundle was created because this matter has more than "
                    f"{MAX_FINAL_BUNDLE_LEDGER_ITEMS:,} every-source check "
                    "ledgers and omitting saved work is not permitted."
                )

            for index, research_job in enumerate(research_jobs, 1):
                for format_name in ("markdown", "json"):
                    research_artifact = bench.export_research_work_product(
                        matter,
                        research_job,
                        format_name,
                        frozen_source_catalog=frozen_source_catalog,
                    )
                    add_work_product(
                        kind="investigation",
                        path=(
                            f"investigations/{index:03d}-"
                            f"{research_artifact.filename}"
                        ),
                        artifact=research_artifact,
                    )
            for index, review_run in enumerate(review_runs, 1):
                with review_export_snapshot(bench.workspace, matter.matter_id, read_actor_id,
                        review_run.run_id, administrator_override=administrator_override) as snapshot:
                    review_run = bench.workspace.review_run(matter.matter_id, read_actor_id,
                        review_run.run_id, administrator_override=administrator_override, _snapshot=snapshot)
                    if FullTextReviewLedger(bench.workspace).enabled(review_run.run_id):
                        text_body = bytearray()
                        with closing(iter_text_export(bench.workspace, matter.matter_id, read_actor_id, review_run.run_id,
                                administrator_override=administrator_override, _snapshot=snapshot)) as text_stream:
                            for fragment in text_stream:
                                if additional_work_product_bytes + len(text_body) + len(fragment) > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                                    raise ExportProblem("No complete bundle was created. Download the full-text ledger separately before closing this matter.")
                                text_body.extend(fragment)
                        add_work_product(kind="full_text_review", path=f"source-checks/{index:03d}-full-text-ledger.json",
                            artifact=ExportArtifact(body=bytes(text_body), media_type="application/json", filename="full-text-ledger.json"))
                    criterion = bench.workspace.review_criterion(
                        matter.matter_id, review_run.criterion_id, _snapshot=snapshot
                    )
                    version = bench.workspace.review_criterion_version(
                        matter.matter_id, review_run.criterion_version_id, _snapshot=snapshot
                    )
                    decisions = bench.workspace.review_decisions_for_export(
                        matter.matter_id,
                        read_actor_id,
                        review_run.run_id,
                        administrator_override=administrator_override, _snapshot=snapshot,
                    )
                    metrics = bench.workspace.review_validation_metrics(
                        matter.matter_id,
                        read_actor_id,
                        review_run.run_id,
                        administrator_override=administrator_override, _snapshot=snapshot,
                    )
                    if frozen_source_catalog is None:
                        decisions = bench._hydrate_full_text_export_decisions(matter, review_run, decisions)
                    elif FullTextReviewLedger(bench.workspace).enabled(review_run.run_id):
                        # Failed-close recovery must not reopen quarantined source state.
                        # Preserve saved locators and labels without claiming live support.
                        decisions = tuple(replace(item,
                            citations=tuple({**value, "excerpt": ""} for value in item.citations),
                            error_message=" ".join(filter(None, (item.error_message,
                                "Supporting text is unavailable because sources are quarantined after a failed close. "
                                "Saved locations remain in this export and the complete full-text ledger."))))
                            if item.citations else item for item in decisions)
                    for format_name in ("csv", "json"):
                        review_artifact = export_full_review(
                            matter,
                            criterion,
                            version,
                            review_run,
                            decisions,
                            metrics,
                            format_name,
                            frozen_text_sources=frozen_source_catalog if FullTextReviewLedger(bench.workspace).enabled(review_run.run_id) else None,
                        )
                        add_work_product(
                            kind="source_check",
                            path=(
                                f"source-checks/{index:03d}-"
                                f"{review_artifact.filename}"
                            ),
                            artifact=review_artifact,
                        )
            for index, receipt in enumerate(IntakeReceipts(bench.workspace).export(
                matter.matter_id, read_actor_id, administrator_override=administrator_override
            ), 1):
                for format_name in ("markdown", "csv", "json"):
                    receipt_artifact = export_intake_receipt(receipt, format_name)
                    add_work_product(kind="intake_receipt", path=f"intake/{index:03d}-{receipt_artifact.filename}", artifact=receipt_artifact)
            repository = bench.workspace.entity_repository(export_read=True, administrator_override=administrator_override)
            with repository.transaction(matter.matter_id, read_actor_id):
                add_work_product(kind="entity_discovery", path="entities/discovery.json",
                    artifact=ExportArtifact(body=json.dumps(repository.discovery_export(matter.matter_id), indent=2).encode("utf-8"),
                        media_type="application/json", filename="discovery.json"))
                for index, record in enumerate(repository.export_records(matter.matter_id), 1):
                    add_work_product(kind="entity", path=f"entities/{index:03d}-entity.json",
                        artifact=ExportArtifact(body=json.dumps(record, indent=2).encode("utf-8"),
                            media_type="application/json", filename="entity.json"))
            repository = bench.workspace.assertion_repository(export_read=True, administrator_override=administrator_override)
            with repository.transaction(matter.matter_id, read_actor_id):
                for index, record in enumerate(repository.export_records(matter.matter_id), 1):
                    add_work_product(kind="assertion", path=f"assertions/{index:04d}-assertion.json",
                        artifact=ExportArtifact(body=json.dumps(record, indent=2).encode("utf-8"),
                            media_type="application/json", filename="assertion.json"))
            from .matter_context_repository import MatterContextRepository
            with repository.transaction(matter.matter_id, read_actor_id):
                manifest = MatterContextRepository(repository).export(matter.matter_id)
                add_work_product(kind="context_selection", path="context/selections.json",
                    artifact=ExportArtifact(body=json.dumps(manifest, indent=2).encode("utf-8"),
                        media_type="application/json", filename="selections.json"))
            from .answer_context import AnswerContextRepository
            with repository.transaction(matter.matter_id, read_actor_id):
                context_jobs = bench.workspace.connection.execute(
                    'SELECT job_id FROM workbench_answer_context WHERE matter_id=? ORDER BY job_id LIMIT 1001',
                    (matter.matter_id,)).fetchall()
                if len(context_jobs) > 1000:
                    raise WorkspaceProblem('Too many context receipts for a complete export.')
                context_bytes = bench.workspace.connection.execute(
                    'SELECT coalesce(sum(length(CAST(c.snapshot_json AS BLOB)) + '
                    '(SELECT coalesce(sum(length(CAST(a.manifest_json AS BLOB))),0) FROM workbench_answer_context_attempt a WHERE a.job_id=c.job_id)),0) '
                    'FROM workbench_answer_context c WHERE c.matter_id=?', (matter.matter_id,)).fetchone()[0]
                if context_bytes > 8 * 1024 * 1024:
                    raise WorkspaceProblem('Context receipts exceed the complete export bound. Export individual receipts.')
                for row in context_jobs:
                    receipt = AnswerContextRepository(bench.workspace).receipt(matter.matter_id, row[0])
                    add_work_product(kind='answer_context', path=f'context/{row[0]}.json',
                        artifact=ExportArtifact(body=json.dumps(receipt, indent=2).encode('utf-8'),
                            media_type='application/json', filename=row[0] + '.json'))
            artifact = export_matter_bundle(
                matter,
                conversations,
                export_source_inventory(source_catalog),
                notebook_export_entries(
                    matter,
                    read_actor_id,
                    administrator_override=administrator_override,
                ),
                export_media_work_product(matter, source_catalog),
                additional_work_product,
                saved_reports=saved_report_files,
                exported_at=exported_at,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        return artifact

    @app.get(
        "/matters/{slug}/export-readiness",
        dependencies=[Depends(require_matter_bundle_response_lease)],
    )
    def check_matter_export(request: Request, slug: str):
        context = auth_context(request)
        matter = response_lease_matter(request, slug)
        inspected_at = datetime.now(timezone.utc)
        report_issues: list[dict[str, str]] = []
        ready = False
        problem = ""
        try:
            # Exercise the same bounded formatting and packaging checks. The
            # temporary in-memory artifact is discarded, never saved or sent.
            prepare_matter_work_product(request, slug, report_issues=report_issues)
            ready = True
        except (ExportProblem, WorkspaceProblem) as exc:
            problem = str(exc)
        try:
            bench.workspace._authorize_export_read(
                matter.matter_id, context.principal_id,
                administrator_override=(
                    getattr(request.state, "administrator_matter_override", None) == matter.matter_id
                ),
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        result = {
            "ready": ready,
            "inspected_at": inspected_at.isoformat().replace("+00:00", "Z"),
            "problem": problem,
            "reports": report_issues,
            "download_url": f"/matters/{matter.slug}/export",
        }
        audit(request, "work_product.export_check", "success", context=context, matter=matter,
            details={"state": "complete" if ready else "attention", "count": len(report_issues)})
        if "application/json" in request.headers.get("accept", ""):
            response = JSONResponse(result, headers={"Cache-Control": "no-store"})
        else:
            recovering_close = bench.workspace.matter_lifecycle(matter.matter_id).state == "purge_failed"
            response = templates.TemplateResponse(request=request, name="workbench_export_readiness.html",
                context={**base_context(request, matter), "matter": matter, "check": result,
                    "inspected_at_label": inspected_at.strftime("%b %d, %Y at %H:%M UTC"),
                    "recovering_close": recovering_close,
                    "return_url": f"/matters/{matter.slug}/" + ("close" if recovering_close else "work-product"),
                    "return_label": "Return to close matter" if recovering_close else "Case file"},
                headers={"Cache-Control": "no-store"})
        return transfer_matter_response_lease(request, response)

    @app.get(
        "/matters/{slug}/export",
        dependencies=[Depends(require_matter_bundle_response_lease)],
    )
    def download_matter_work_product(request: Request, slug: str):
        context = auth_context(request)
        matter = response_lease_matter(request, slug)
        administrator_override = (
            getattr(request.state, "administrator_matter_override", None) == matter.matter_id
        )
        try:
            artifact = prepare_matter_work_product(request, slug)
        except (ExportProblem, WorkspaceProblem) as exc:
            return PlainTextResponse(str(exc), status_code=409)
        audit(
            request,
            "work_product.export",
            "success",
            context=context,
            matter=matter,
            object_type="matter",
            object_id=matter.matter_id,
            details={
                "kind": "matter",
                "format": "zip",
                **({"role": "administrator"} if administrator_override else {}),
            },
        )
        return download_response(request, artifact)

    @app.get('/matters/{slug}/answer-jobs/{job_id}/context')
    def answer_context_receipt(request: Request, slug: str, job_id: str, format_name: str = Query("html", alias="format", pattern="^(html|json)$")):
        from .answer_context import AnswerContextRepository
        from .matter_context import MatterContextService
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            administrator_override = getattr(request.state, 'administrator_matter_override', None) == matter.matter_id
            read_actor = matter.owner_id if administrator_override else context.principal_id
            with bench.source_store(matter).mutation_guard(), bench.workspace._lock:
                job_row = bench.workspace.connection.execute(
                    'SELECT conversation_id FROM workbench_answer_job WHERE job_id=? AND matter_id=?',
                    (job_id, matter.matter_id)).fetchone()
                if job_row is None:
                    raise KeyError(job_id)
                bench.workspace.get_conversation_any(matter.matter_id, job_row[0])
                receipt = AnswerContextRepository(bench.workspace).receipt(matter.matter_id, job_id)
                if receipt is None:
                    raise KeyError(job_id)
                service = MatterContextService(bench.assertion_service(matter))
                budget = service.budget()
                receipt['current_state'] = [dict(kind=row['entry']['kind'], object_id=row['entry']['object_id'],
                    state=service._row(matter.matter_id, read_actor, row['entry'], budget)['state'])
                    for row in receipt['snapshot']['records']]
                receipt['selection_now'] = service.repository.selection(matter.matter_id, receipt['snapshot']['actor_id'])['revision']
                from .answer_context import references
                refs = references(receipt['snapshot'])
                indexes = service.entities.validate_references(refs)
                available = {refs[i]['support_token'] for i in indexes}
                response = (JSONResponse(receipt, headers={'Cache-Control': 'no-store'}) if format_name == 'json' else
                    templates.TemplateResponse(request=request, name='workbench_answer_context.html',
                        context={**base_context(request, matter), 'matter': matter, 'receipt': receipt,
                                 'available_support_tokens': available}, headers={'Cache-Control':'no-store'}))
                refresh_context_authority(request, slug, context, administrator_override)
                return response
        except KeyError as exc:
            raise HTTPException(404, 'Answer context is unavailable') from exc

    @app.post(
        "/matters/{slug}/ask",
        dependencies=[Depends(require_csrf)],
    )
    def ask_question(
        request: Request,
        slug: str,
        question: str = Form(..., max_length=MAX_QUESTION_CHARS),
        conversation: str = Form("", max_length=80),
        request_key: str = Form("", max_length=47),
        source_set: str = Form("", max_length=80),
        notebook_mode: str = Form("", max_length=24),
        notebook_item: list[str] = Form(default=[]),
        use_saved_context: bool = Form(False),
        expected_selection_revision: int | None = Form(None),
        review_task: str = Form("answer", pattern="^(answer|research)$"),
    ):
        wants_json = "application/json" in request.headers.get("accept", "")
        context = auth_context(request)
        origin = _source_review_return_href(slug, request.query_params.get("entity_return_to", ""))
        try:
            matter = authorized_matter(request, slug)
            if use_saved_context and (notebook_mode or notebook_item or review_task != 'answer'):
                raise WorkspaceProblem('Saved context is available only for focused answers and cannot be combined with another context mode.')
            if notebook_item and notebook_mode not in ('', 'selected'):
                raise WorkspaceProblem('Choose one legacy notebook context mode.')
            if source_set:
                try:
                    bench.workspace.source_set(matter.matter_id, source_set)
                except KeyError as exc:
                    raise WorkspaceProblem(
                        "That source set is empty or no longer available."
                    ) from exc
            readiness = bench.workspace.matter_readiness(matter.matter_id)
            if not readiness.can_query:
                if readiness.state == "empty":
                    message = "Add at least one source before asking a question."
                elif readiness.state == "preparing":
                    message = (
                        "Matter preparation is still active. Wait for uploading, OCR, "
                        "transcription and source preparation to finish, then send this question."
                    )
                else:
                    message = (
                        "No source is searchable yet. Retry or remove an affected "
                        "source, then send this question."
                    )
                raise WorkspaceProblem(message)
            active = (
                bench.workspace.get_conversation(matter.matter_id, conversation)
                if conversation.strip()
                else None
            )
            key = request_key or f"answer-request-{uuid.uuid4().hex}"
            if review_task == "research":
                if not bench.generator.available:
                    raise WorkspaceProblem(
                        "Broader investigation is unavailable while local answering is offline."
                    )
                research_conversation = active or bench.workspace.get_conversation(
                    matter.matter_id
                )
                match = re.fullmatch(r"answer-request-([0-9a-f]{32})", key)
                research_key = (
                    f"research-request-{match.group(1)}"
                    if match
                    else f"research-request-{uuid.uuid4().hex}"
                )
                research_title = WorkspaceStore._automatic_conversation_title(
                    question, maximum=120
                )
                research, created = bench.workspace.queue_research_job(
                    matter.matter_id,
                    context.principal_id,
                    question,
                    research_title,
                    research_key,
                    source_set or None,
                    conversation_id=research_conversation.conversation_id,
                )
                if bench.research is not None:
                    bench.research.notify()
                audit(
                    request,
                    "research.create",
                    "success",
                    context=context,
                    matter=matter,
                    object_type="research_job",
                    object_id=research.job_id,
                    details={"created": created, "state": research.state},
                )
                workspace_url = _query_url(
                    f"/matters/{slug}",
                    conversation=research_conversation.conversation_id,
                    notice="Broader investigation saved in this conversation",
                    entity_return_to=origin,
                )
                if wants_json:
                    return JSONResponse(
                        {
                            **research_status_projection(matter, research, origin=origin),
                            "workspace_url": workspace_url,
                        },
                        status_code=202 if created else 200,
                    )
                return RedirectResponse(workspace_url, status_code=303)
            if active is not None and any(
                item.conversation_id == active.conversation_id
                and item.state in {"queued", "running"}
                for item in bench.workspace.research_jobs(
                    matter.matter_id, context.principal_id
                )
            ):
                raise WorkspaceProblem(
                    "A broader investigation is still working in this conversation. "
                    "Wait for it to return before asking a follow-up."
                )
            selected_notebook_items = tuple(
                item_id for item_id in notebook_item if item_id.strip()
            )
            selected_notebook_mode = (
                "selected" if selected_notebook_items else notebook_mode or None
            )
            job, created = bench.queue_answer(
                matter,
                active,
                question,
                key,
                context.principal_id,
                source_set or None,
                selected_notebook_mode,
                selected_notebook_items,
                use_saved_context=use_saved_context,
                expected_selection_revision=expected_selection_revision,
                check_authority=lambda: refresh_context_authority(request, slug, context, False),
            )
            projected = answer_projection(request, matter, job)
            if active is None and created:
                audit(
                    request,
                    "conversation.create",
                    "success",
                    context=context,
                    matter=matter,
                    object_type="conversation",
                    object_id=job.conversation_id,
                    details={"state": "active"},
                )
            audit(
                request,
                "answer.submit",
                "success",
                context=context,
                matter=matter,
                object_type="answer_job",
                object_id=job.job_id,
                details={
                    "created": created,
                    "state": job.state,
                    "count": len(selected_notebook_items),
                },
            )
            if wants_json:
                return JSONResponse(projected, status_code=202 if created else 200)
            return RedirectResponse(projected["workspace_url"], status_code=303)
        except KeyError as exc:
            raise HTTPException(404, "Matter or conversation not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409)
            return RedirectResponse(
                _query_url(f"/matters/{slug}", conversation=conversation, error=str(exc), entity_return_to=origin),
                status_code=303,
            )

    @app.get("/matters/{slug}/answer-jobs/{job_id}")
    def answer_job_status(request: Request, slug: str, job_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            job = bench.workspace.get_answer_job(
                matter.matter_id, context.principal_id, job_id
            )
        except KeyError as exc:
            raise HTTPException(404, "Answer request not found") from exc
        return answer_projection(request, matter, job)

    @app.get("/matters/{slug}/answer-jobs/{job_id}/result")
    def answer_job_result(request: Request, slug: str, job_id: str):
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            job = bench.workspace.get_answer_job(
                matter.matter_id, context.principal_id, job_id
            )
            projected = answer_projection(request, matter, job)
        except KeyError as exc:
            raise HTTPException(404, "Answer request not found") from exc
        if job.state != "succeeded":
            return JSONResponse(
                {
                    "message": "This answer is not ready yet.",
                    "state": job.state,
                    "status_url": projected["status_url"],
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        return RedirectResponse(
            projected["workspace_url"],
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/matters/{slug}/answer-jobs/{job_id}/cancel",
        dependencies=[Depends(require_csrf)],
    )
    def cancel_answer_job(request: Request, slug: str, job_id: str):
        wants_json = "application/json" in request.headers.get("accept", "")
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            job = bench.workspace.cancel_answer_job(
                matter.matter_id, context.principal_id, job_id
            )
            projected = answer_projection(request, matter, job)
        except KeyError as exc:
            raise HTTPException(404, "Answer request not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409)
            return RedirectResponse(
                _query_url(f"/matters/{slug}", error=str(exc)), status_code=303
            )
        audit(
            request,
            "answer.cancel",
            "success",
            context=context,
            matter=matter,
            object_type="answer_job",
            object_id=job.job_id,
            details={"state": job.state},
        )
        if wants_json:
            return JSONResponse(projected)
        return RedirectResponse(projected["workspace_url"], status_code=303)
    @app.post(
        "/matters/{slug}/answer-jobs/{job_id}/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_answer_job(request: Request, slug: str, job_id: str):
        wants_json = "application/json" in request.headers.get("accept", "")
        context = auth_context(request)
        try:
            matter = authorized_matter(request, slug)
            readiness = bench.workspace.matter_readiness(matter.matter_id)
            if not readiness.can_query:
                if readiness.state == "preparing":
                    raise WorkspaceProblem(
                        "Matter preparation is active. Wait for uploading, OCR, "
                        "transcription and source preparation to finish, then retry this answer."
                    )
                raise WorkspaceProblem(
                    "No source is searchable yet. Retry or remove an affected "
                    "source before retrying this answer."
                )
            job = bench.workspace.retry_answer_job(
                matter.matter_id, context.principal_id, job_id
            )
            if bench.answers is not None:
                bench.answers.notify()
            projected = answer_projection(request, matter, job)
        except KeyError as exc:
            raise HTTPException(404, "Answer request not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409)
            return RedirectResponse(
                _query_url(f"/matters/{slug}", error=str(exc)), status_code=303
            )
        audit(
            request,
            "answer.retry",
            "success",
            context=context,
            matter=matter,
            object_type="answer_job",
            object_id=job.job_id,
            details={"state": job.state},
        )
        if wants_json:
            return JSONResponse(projected)
        return RedirectResponse(projected["workspace_url"], status_code=303)

    app_context = AppContext(
        bench=bench, identity=identity, templates=templates,
        auth_context=auth_context, authorized_matter=authorized_matter,
        require_csrf=require_csrf, require_csrf_header=require_csrf_header,
        audit=audit, requested_path=requested_path, base_context=base_context,
        present_value=present_value, download_response=download_response,
        require_matter_response_lease=require_matter_response_lease,
        require_matter_bundle_response_lease=require_matter_bundle_response_lease,
        response_lease_matter=response_lease_matter, transfer_matter_response_lease=transfer_matter_response_lease,
        recording_decision_limit=recording_decision_limit, recording_decision_capacity=recording_decision_capacity,
        recording_decisions_pending=recording_decisions_pending,
        report_labels=report_labels, report_citation_hrefs=report_citation_hrefs,
        report_edit_recovery=report_edit_recovery, report_work_choices=report_work_choices,
        render_report_compilation=render_report_compilation, add_report_material=add_report_material,
    )
    app.state.app_context = app_context
    from .one_box_routes import install_one_box_routes
    install_one_box_routes(app, authorized_matter=authorized_matter, require_csrf=require_csrf,
                           templates=templates, ask=ask_question, readiness_for=bench.workspace.matter_readiness, home=matter_home)
    from .upload_review import install_upload_review
    install_upload_review(app, bench=bench, authorized_matter=authorized_matter,
                          auth_context=auth_context, templates=templates, base_context=base_context)
    from .deeper_investigation import install_deeper_investigation
    install_deeper_investigation(templates, bench=bench, auth_context=auth_context)
    from .briefing_home import install_briefing_home
    install_briefing_home(templates, bench=bench, auth_context=auth_context, readiness_for=matter_readiness_projection)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Run the Milestone A {PRODUCT_NAME} workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8786)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    args = parser.parse_args()
    if args.host != "127.0.0.1" and not (
        args.host == "0.0.0.0"
        and env("RECORDBENCH_ALLOW_CONTAINER_BIND", "") == "1"
    ):
        parser.error(
            "non-loopback binding is allowed only in the isolated container profile"
        )
    try:
        selected_auth_mode = resolve_auth_mode()
    except RuntimeError as exc:
        parser.error(str(exc))
    uvicorn.run(
        create_workbench_app(args.runtime, auth_mode=selected_auth_mode),
        host=args.host,
        port=args.port,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
