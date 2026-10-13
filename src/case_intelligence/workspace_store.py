"""Persistent development matters and conversations for Milestone A."""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .contracts import validate_relative_path
from .derived_text import presentation_text
from .source_locations import SourcePreflight
from .store.knowledge_services import KnowledgeServicesMixin
from .store.answer_scope import AnswerScopeMixin
from .store import (
    MAX_REPORT_CITATION_EXCERPT_CHARS as MAX_REPORT_CITATION_EXCERPT_CHARS,
    MAX_REPORT_SECTION_CITATIONS as MAX_REPORT_SECTION_CITATIONS,
    NOTEBOOK_ORIGINS as NOTEBOOK_ORIGINS,
    NOTEBOOK_STATUSES as NOTEBOOK_STATUSES,
    NOTEBOOK_TYPES as NOTEBOOK_TYPES,
    _IDENTIFIER,
    _MEDIA_CLIP,
    _NOTEBOOK_ITEM,
    _REPORT,
    _REPORT_SECTION,
    _SOURCE_DOCUMENT,
)
from .store.notebook import NotebookStoreMixin
from .store.reports import ReportsStoreMixin
# Re-export record types, exceptions, and the shared helper for existing importers.
from .store_records import (
    AnalysisRunRecord as AnalysisRunRecord,
    AnswerEventRecord as AnswerEventRecord,
    AnswerJobRecord as AnswerJobRecord,
    AnswerNotebookContextRecord as AnswerNotebookContextRecord,
    AuditEventRecord as AuditEventRecord,
    ConversationDeletionRecord as ConversationDeletionRecord,
    ConversationOrganizationRecord as ConversationOrganizationRecord,
    ConversationRecord as ConversationRecord,
    ConversationSummaryRecord as ConversationSummaryRecord,
    IngestJobRecord as IngestJobRecord,
    IngestPlanItemRecord as IngestPlanItemRecord,
    IngestPlanRecord as IngestPlanRecord,
    MatterActivityRecord as MatterActivityRecord,
    MatterLifecycleRecord as MatterLifecycleRecord,
    MatterMembershipRecord as MatterMembershipRecord,
    MatterNameConflict as MatterNameConflict,
    MatterReadinessRecord as MatterReadinessRecord,
    MatterRecord as MatterRecord,
    MatterRetentionRecord as MatterRetentionRecord,
    MediaClipRecord as MediaClipRecord,
    MediaJobRecord as MediaJobRecord,
    MediaSummaryRecord as MediaSummaryRecord,
    MediaTranscriptRecord as MediaTranscriptRecord,
    MessageRecord as MessageRecord,
    NotebookEditConflict as NotebookEditConflict,
    NotebookItemRecord as NotebookItemRecord,
    NotebookPageRecord as NotebookPageRecord,
    NotebookReferenceRecord as NotebookReferenceRecord,
    PrincipalPreferenceRecord as PrincipalPreferenceRecord,
    PrincipalRecord as PrincipalRecord,
    ReportCitationRecord as ReportCitationRecord,
    ReportEditConflict as ReportEditConflict,
    ReportRecord as ReportRecord,
    ReportSectionRecord as ReportSectionRecord,
    ResearchEventRecord as ResearchEventRecord,
    ResearchJobRecord as ResearchJobRecord,
    ReviewCriterionRecord as ReviewCriterionRecord,
    ReviewCriterionVersionRecord as ReviewCriterionVersionRecord,
    ReviewDecisionConflict as ReviewDecisionConflict,
    ReviewDecisionPageRecord as ReviewDecisionPageRecord,
    ReviewDecisionRecord as ReviewDecisionRecord,
    ReviewFindingRecord as ReviewFindingRecord,
    ReviewFindingReferenceRecord as ReviewFindingReferenceRecord,
    ReviewRunRecord as ReviewRunRecord,
    SessionRecord as SessionRecord,
    SourceCatalogPageRecord as SourceCatalogPageRecord,
    SourceCatalogRecord as SourceCatalogRecord,
    SourceCollectionRecord as SourceCollectionRecord,
    SourceFolderPageRecord as SourceFolderPageRecord,
    SourceFolderRecord as SourceFolderRecord,
    SourceOrganizationRecord as SourceOrganizationRecord,
    SourceSetRecord as SourceSetRecord,
    SpeakerMappingRecord as SpeakerMappingRecord,
    TranscriptSegmentRecord as TranscriptSegmentRecord,
    UploadItemRecord as UploadItemRecord,
    UploadSessionRecord as UploadSessionRecord,
    WorkspaceProblem as WorkspaceProblem,
    is_full_text_synthesis as is_full_text_synthesis,
)

_SLUG = re.compile(r"^m-[0-9a-f]{12}$")
_ANSWER_JOB = re.compile(r"^answer-job-[0-9a-f]{32}$")
_ANSWER_REQUEST = re.compile(r"^answer-request-[0-9a-f]{32}$")
_SOURCE_COLLECTION = re.compile(r"^source-collection-[0-9a-f]{32}$")
_SOURCE_SET = re.compile(r"^source-set-[0-9a-f]{32}$")
_UPLOAD_SESSION = re.compile(r"^upload-session-[0-9a-f]{32}$")
_UPLOAD_ITEM = re.compile(r"^upload-item-[0-9a-f]{32}$")
_MEDIA_JOB = re.compile(r"^media-job-[0-9a-f]{32}$")
AUTOMATIC_DISCOVERY_PRINCIPAL = "system-automatic-discovery"
_TRANSCRIPT = re.compile(r"^transcript-[0-9a-f]{32}$")
_TRANSCRIPT_SEGMENT = re.compile(r"^media-segment-[0-9a-f]{32}$")
_NOTEBOOK_REFERENCE = re.compile(r"^notebook-reference-[0-9a-f]{32}$")
_RESEARCH_JOB = re.compile(r"^research-job-[0-9a-f]{32}$")
_RESEARCH_REQUEST = re.compile(r"^research-request-[0-9a-f]{32}$")
_REVIEW_CRITERION = re.compile(r"^review-criterion-[0-9a-f]{32}$")
_REVIEW_CRITERION_VERSION = re.compile(r"^review-version-[0-9a-f]{32}$")
_REVIEW_RUN = re.compile(r"^review-run-[0-9a-f]{32}$")
_ANSWER_ACTIVE_STAGES = {"retrieving", "reranking", "generating", "verifying"}
_PRINCIPAL_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_PRINCIPAL_ID = re.compile(r"^[a-z][a-z0-9-]{7,99}$")
_AUDIT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{2,80}$")
_AUDIT_DETAIL_STATES = {
    "active",
    "attention",
    "archived",
    "cancelled",
    "complete",
    "completed",
    "deleted",
    "draft",
    "empty",
    "failed",
    "final",
    "flagged",
    "included",
    "excluded",
    "open",
    "partial",
    "preparing",
    "purge_failed",
    "purging",
    "queued",
    "ready",
    "review",
    "reviewed",
    "suggested",
    "confirmed",
    "disputed",
    "needs_review",
    "dismissed",
    "revoked",
    "running",
    "succeeded",
    "scheduled",
    "unreviewed",
    "uploaded",
}
PREFERENCE_THEMES = ("dark", "light", "dusk", "cyberpunk", "retro")
MATTER_ACTIVITY_KINDS = ("source", "conversation", "notebook", "analysis", "report")
MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS = 3
_ANALYSIS_RESTART_MESSAGE = (
    "The review map refresh was interrupted by an application restart. "
    "Select Refresh review map to retry. Existing review decisions are unchanged."
)


class WorkspaceStore(NotebookStoreMixin, ReportsStoreMixin, AnswerScopeMixin, KnowledgeServicesMixin):
    """Small SQLite control store; source bytes remain in per-matter stores."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        principal_enabled: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise RuntimeError("workspace database path is unsafe")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise RuntimeError("workspace database directory is unsafe")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        # In-process ledger readers share admission/deletion serialization.
        self._text_review_export_leases: dict[str, int] = {}
        self._matter_readiness_cache: dict[
            str, tuple[tuple[int, int], MatterReadinessRecord]
        ] = {}
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level="IMMEDIATE",
        )
        self.principal_enabled = principal_enabled or (lambda provider, subject: provider != "local")
        self.connection.create_function(
            "recordbench_principal_enabled", 2,
            lambda provider, subject: int(self.principal_enabled(provider, subject)),
        )
        self.connection.create_function("recordbench_case_lifecycle_writer", 0, lambda: 2)
        self._case_cleanup_matter = None
        self.connection.create_function("recordbench_case_content_cleanup", 1,
            lambda matter_id: int(self._case_cleanup_matter == matter_id))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA foreign_keys = ON")
        case_policy_exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workbench_case_policy'"
        ).fetchone() is not None
        if case_policy_exists:
            # Upgrade writer fences before old idempotent backfills encounter
            # the previous writer version. No workers or recovery have started.
            from .case_lifecycle_migration import migrate_case_lifecycle
            migrate_case_lifecycle(self.connection)
        for name in (
            "migrations/sqlite/0002_milestone_a_workbench.sql",
            "migrations/sqlite/0003_scalable_ingestion.sql",
            "migrations/sqlite/0004_durable_answer_jobs.sql",
            "migrations/sqlite/0005_identity_membership_audit.sql",
            "migrations/sqlite/0006_oidc_auth_transactions.sql",
            "migrations/sqlite/0007_matter_lifecycle.sql",
            "migrations/sqlite/0008_conversation_organization.sql",
            "migrations/sqlite/0009_source_library.sql",
            "migrations/sqlite/0010_matter_notebook.sql",
            "migrations/sqlite/0011_matter_media.sql",
            "migrations/sqlite/0012_matter_media_purge_repair.sql",
            "migrations/sqlite/0013_media_transcript_summary.sql",
            "migrations/sqlite/0014_source_catalog.sql",
            "migrations/sqlite/0015_matter_review_analysis.sql",
            "migrations/sqlite/0016_report_builder.sql",
            "migrations/sqlite/0017_matter_retention_and_preferences.sql",
            "migrations/sqlite/0018_review_cockpit.sql",
            "migrations/sqlite/0019_fair_ingest_scheduling.sql",
            "migrations/sqlite/0020_processing_awareness.sql",
            "migrations/sqlite/0021_research_and_full_review.sql",
            "migrations/sqlite/0022_session_application_roles.sql",
            "migrations/sqlite/0023_review_conversation_continuity.sql",
            "migrations/sqlite/0024_review_source_content_basis.sql",
            "migrations/sqlite/0025_intake_receipts.sql",
            "migrations/sqlite/0026_source_byte_matches.sql",
            "migrations/sqlite/0027_report_compilation.sql",
            "migrations/sqlite/0028_full_text_review.sql",
            "migrations/sqlite/0029_report_compilation_basis.sql",
            "migrations/sqlite/0030_full_text_review_limits.sql",
            "migrations/sqlite/0031_team_groups.sql",
            "migrations/sqlite/0032_entity_workspace.sql",
        ):
            # The original organization backfill uses INSERT OR IGNORE, which
            # runs BEFORE INSERT fences even for an already existing row.
            if case_policy_exists and name.endswith("0008_conversation_organization.sql"):
                continue
            migration = resources.files("case_intelligence").joinpath(name).read_text(encoding="utf-8")
            self.connection.executescript(migration)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            report_section_columns = {
                str(row[1]) for row in self.connection.execute("PRAGMA table_info(workbench_report_section)")
            }
            if "compilation_basis" not in report_section_columns:
                self.connection.execute(
                    "ALTER TABLE workbench_report_section ADD COLUMN compilation_basis TEXT NOT NULL DEFAULT ''"
                )
        if 'extractor_version' not in {row[1] for row in self.connection.execute('PRAGMA table_info(workbench_entity)')}:
            migration = resources.files("case_intelligence").joinpath("migrations/sqlite/0033_entity_discovery.sql").read_text(encoding="utf-8")
            self.connection.executescript('BEGIN IMMEDIATE;\n' + migration + '\nCOMMIT;')
        for name in ('0034_evidence_assertions.sql', '0035_matter_context_selection.sql',
                     '0036_answer_context.sql', '0037_automatic_entity_discovery.sql', '0038_matter_purpose.sql'):
            migration = resources.files("case_intelligence").joinpath("migrations/sqlite/" + name).read_text(encoding="utf-8")
            self.connection.executescript('BEGIN IMMEDIATE;\n' + migration + '\nCOMMIT;')
        from .source_roles import migrate_source_roles
        migrate_source_roles(self.connection)
        from .case_lifecycle_migration import migrate_case_lifecycle
        from .case_lifecycle import CaseLifecycleRepository
        migrate_case_lifecycle(self.connection)
        self.case_lifecycle = CaseLifecycleRepository(self)
        from .full_text_review_budget import backfill_legacy_ledgers
        backfill_legacy_ledgers(self)
        session_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(workbench_session)")
        }
        if "application_roles" not in session_columns:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE workbench_session ADD COLUMN application_roles "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
        research_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(workbench_research_job)"
            )
        }
        with self.connection:
            if "conversation_id" not in research_columns:
                self.connection.execute(
                    "ALTER TABLE workbench_research_job ADD COLUMN conversation_id TEXT"
                )
            if "result_message_id" not in research_columns:
                self.connection.execute(
                    "ALTER TABLE workbench_research_job ADD COLUMN result_message_id TEXT"
                )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS workbench_research_job_conversation_idx "
                "ON workbench_research_job(matter_id,conversation_id,created_at DESC,job_id)"
            )
        catalog_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(workbench_source_catalog)"
            )
        }
        decision_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(workbench_review_decision)"
            )
        }
        with self.connection:
            if "content_basis_digest" not in catalog_columns:
                self.connection.execute(
                    "ALTER TABLE workbench_source_catalog ADD COLUMN "
                    "content_basis_digest TEXT NOT NULL DEFAULT '' "
                    "CHECK (content_basis_digest='' OR length(content_basis_digest)=64)"
                )
            if "source_basis_digest" not in decision_columns:
                self.connection.execute(
                    "ALTER TABLE workbench_review_decision ADD COLUMN "
                    "source_basis_digest TEXT NOT NULL DEFAULT '' "
                    "CHECK (source_basis_digest='' OR length(source_basis_digest)=64)"
                )
            # Any queued or running snapshot without a trustworthy same-version
            # content basis must not resume. Fail it durably and require a fresh
            # frozen population. Completed historical runs remain available.
            migration_now = self._now()
            legacy_active = self.connection.execute(
                "SELECT run.run_id,run.reviewed_count,run.snapshot_count "
                "FROM workbench_review_run run WHERE run.state IN ('queued','running') "
                "AND EXISTS (SELECT 1 FROM workbench_review_decision decision "
                "WHERE decision.run_id=run.run_id "
                "AND decision.source_basis_digest='') AND run.matter_id IN ("
                "SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active')"
            ).fetchall()
            legacy_message = (
                "This saved source check predates exact source-content tracking. "
                "Start a new source check to freeze the current sources."
            )
            for legacy in legacy_active:
                self.connection.execute(
                    "UPDATE workbench_review_run SET state='failed',stage='failed',"
                    "message=?,worker_id=NULL,finished_at=?,updated_at=? "
                    "WHERE run_id=? AND state IN ('queued','running')",
                    (
                        legacy_message,
                        migration_now,
                        migration_now,
                        legacy["run_id"],
                    ),
                )
                self._append_review_event_locked(
                    str(legacy["run_id"]),
                    state="failed",
                    stage="failed",
                    message=legacy_message,
                    reviewed_count=int(legacy["reviewed_count"]),
                    snapshot_count=int(legacy["snapshot_count"]),
                    created_at=migration_now,
                )

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("workspace clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def current_time(self) -> datetime:
        """Return the injected UTC clock without exposing mutable store state."""

        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("workspace clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_text(
        value: str,
        *,
        label: str,
        maximum: int,
        required: bool = True,
        multiline: bool = False,
    ) -> str:
        prepared = (value or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = unicodedata.normalize("NFC", prepared).strip()
        if required and not normalized:
            raise WorkspaceProblem(f"{label} is required.")
        if len(normalized) > maximum:
            raise WorkspaceProblem(f"{label} is too long.")
        if any(
            unicodedata.category(character) in {"Cc", "Cf"}
            and not (multiline and character in {"\n", "\t"})
            for character in normalized
        ):
            raise WorkspaceProblem(f"{label} contains unsupported characters.")
        return normalized

    @staticmethod
    def _derived_text(
        value: str, *, label: str, maximum: int, required: bool = True,
        multiline: bool = False,
    ) -> str:
        """Bound prose before projecting controls; never use for source identity."""
        value = value or ""
        if len(value) > maximum:
            raise WorkspaceProblem(f"{label} is too long. Shorten the text and save again; existing work is retained.")
        prepared = presentation_text(value).replace("\r\n", "\n").replace("\r", "\n")
        normalized = unicodedata.normalize("NFC", prepared).strip()
        if not multiline:
            normalized = normalized.replace("\n", " ").replace("\t", " ")
        if required and not normalized:
            raise WorkspaceProblem(f"{label} is required. Add readable text and save again; existing work is retained.")
        if len(normalized) > maximum:
            raise WorkspaceProblem(f"{label} is too long.")
        return normalized

    @staticmethod
    def _source_text(
        value: str, *, label: str, maximum: int, required: bool = True,
        multiline: bool = True,
    ) -> str:
        """Keep bounded citation snapshots exact, including controls and NFC basis."""
        value = value or ""
        if len(value) > maximum:
            raise WorkspaceProblem(f"{label} is too long. Open the original source and save a shorter supported passage.")
        if required and not value.strip():
            raise WorkspaceProblem(f"{label} is required.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WorkspaceProblem(f"{label} contains invalid Unicode. Open the original source for review; existing work is retained.") from exc
        return value

    @staticmethod
    def _matter(row: sqlite3.Row) -> MatterRecord:
        return MatterRecord(**dict(row))

    @staticmethod
    def _principal(row: sqlite3.Row) -> PrincipalRecord:
        return PrincipalRecord(**dict(row))

    @staticmethod
    def _lifecycle(row: sqlite3.Row) -> MatterLifecycleRecord:
        return MatterLifecycleRecord(**dict(row))

    @staticmethod
    def _retention(row: sqlite3.Row) -> MatterRetentionRecord:
        return MatterRetentionRecord(**dict(row))

    @staticmethod
    def _preference(row: sqlite3.Row) -> PrincipalPreferenceRecord:
        return PrincipalPreferenceRecord(**dict(row))

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(**dict(row))

    @staticmethod
    def _membership(row: sqlite3.Row) -> MatterMembershipRecord:
        return MatterMembershipRecord(**dict(row))

    @staticmethod
    def _audit_event(row: sqlite3.Row) -> AuditEventRecord:
        values = dict(row)
        try:
            details = json.loads(values.pop("details_json"))
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("stored audit metadata is invalid") from exc
        if not isinstance(details, dict):
            raise RuntimeError("stored audit metadata is invalid")
        return AuditEventRecord(details=details, **values)

    @staticmethod
    def _conversation(row: sqlite3.Row) -> ConversationRecord:
        return ConversationRecord(**dict(row))

    @staticmethod
    def _conversation_organization(
        row: sqlite3.Row,
    ) -> ConversationOrganizationRecord:
        return ConversationOrganizationRecord(**dict(row))

    @staticmethod
    def _plan(row: sqlite3.Row) -> IngestPlanRecord:
        return IngestPlanRecord(**dict(row))

    @staticmethod
    def _plan_item(row: sqlite3.Row) -> IngestPlanItemRecord:
        return IngestPlanItemRecord(**dict(row))

    @staticmethod
    def _job(row: sqlite3.Row) -> IngestJobRecord:
        return IngestJobRecord(**dict(row))

    @staticmethod
    def _decoded_json(value: object, expected: type, label: str):
        try:
            decoded = json.loads(str(value))
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"stored {label} metadata is invalid") from exc
        if not isinstance(decoded, expected):
            raise RuntimeError(f"stored {label} metadata is invalid")
        return decoded

    @classmethod
    def _media_job(cls, row: sqlite3.Row) -> MediaJobRecord:
        values = dict(row)
        values["progress"] = float(values["progress"])
        values["degraded"] = int(values["degraded"])
        values["warnings"] = tuple(
            cls._decoded_json(values.pop("warnings_json"), list, "media warning")
        )
        values["quality"] = cls._decoded_json(
            values.pop("quality_json"), dict, "media quality"
        )
        values["provenance"] = cls._decoded_json(
            values.pop("provenance_json"), dict, "media provenance"
        )
        return MediaJobRecord(**values)

    @classmethod
    def _media_transcript(cls, row: sqlite3.Row) -> MediaTranscriptRecord:
        values = dict(row)
        values["warnings"] = tuple(
            cls._decoded_json(values.pop("warnings_json"), list, "transcript warning")
        )
        values["quality"] = cls._decoded_json(
            values.pop("quality_json"), dict, "transcript quality"
        )
        values["provenance"] = cls._decoded_json(
            values.pop("provenance_json"), dict, "transcript provenance"
        )
        return MediaTranscriptRecord(**values)

    @classmethod
    def _media_summary(cls, row: sqlite3.Row) -> MediaSummaryRecord:
        values = dict(row)
        values["attempts"] = int(values["attempts"])
        values["covered_segment_count"] = int(values["covered_segment_count"])
        values["total_segment_count"] = int(values["total_segment_count"])
        values["payload"] = cls._decoded_json(
            values.pop("payload_json"), dict, "media summary"
        )
        return MediaSummaryRecord(**values)

    @staticmethod
    def _transcript_segment(row: sqlite3.Row) -> TranscriptSegmentRecord:
        values = dict(row)
        values["ordinal"] = int(values["ordinal"])
        values["start_ms"] = int(values["start_ms"])
        values["end_ms"] = int(values["end_ms"])
        values["current_revision"] = int(values["current_revision"])
        values["speaker_revision"] = int(values["speaker_revision"])
        values["low_confidence"] = int(values["low_confidence"])
        values["overlap"] = int(values["overlap"])
        if values["confidence"] is not None:
            values["confidence"] = float(values["confidence"])
        return TranscriptSegmentRecord(**values)

    @staticmethod
    def _speaker_mapping(row: sqlite3.Row) -> SpeakerMappingRecord:
        values = dict(row)
        values["revision"] = int(values["revision"])
        values["segment_count"] = int(values["segment_count"])
        return SpeakerMappingRecord(**values)

    @staticmethod
    def _media_clip(row: sqlite3.Row) -> MediaClipRecord:
        values = dict(row)
        values["start_ms"] = int(values["start_ms"])
        values["end_ms"] = int(values["end_ms"])
        return MediaClipRecord(**values)

    @staticmethod
    def _source_collection(row: sqlite3.Row) -> SourceCollectionRecord:
        values = dict(row)
        values.pop("name_key", None)
        values["source_count"] = int(values.get("source_count", 0))
        return SourceCollectionRecord(**values)

    @staticmethod
    def _source_organization(row: sqlite3.Row) -> SourceOrganizationRecord:
        return SourceOrganizationRecord(**dict(row))

    @staticmethod
    def _source_catalog(row: sqlite3.Row) -> SourceCatalogRecord:
        values = dict(row)
        values.pop("display_name_key", None)
        values.pop("search_key", None)
        values.pop("cataloged_at", None)
        values.pop("updated_at", None)
        for name in (
            "completed_units",
            "total_units",
            "page_count",
            "duration_ms",
            "byte_size",
            "retryable",
            "removable",
            "has_video",
        ):
            values[name] = int(values[name])
        return SourceCatalogRecord(**values)

    @staticmethod
    def _source_set(row: sqlite3.Row) -> SourceSetRecord:
        values = dict(row)
        values.pop("name_key", None)
        values["source_count"] = int(values.get("source_count", 0))
        return SourceSetRecord(**values)

    @staticmethod
    def _upload_session(row: sqlite3.Row) -> UploadSessionRecord:
        return UploadSessionRecord(**dict(row))

    @staticmethod
    def _upload_item(row: sqlite3.Row) -> UploadItemRecord:
        return UploadItemRecord(**dict(row))

    @staticmethod
    def _answer_job(row: sqlite3.Row) -> AnswerJobRecord:
        return AnswerJobRecord(**dict(row))

    @staticmethod
    def _answer_event(row: sqlite3.Row) -> AnswerEventRecord:
        return AnswerEventRecord(**dict(row))

    @staticmethod
    def _research_job(row: sqlite3.Row) -> ResearchJobRecord:
        values = dict(row)
        values["plan"] = json.loads(values.pop("plan_json") or "{}")
        values["result"] = json.loads(values.pop("result_json") or "{}")
        for name in (
            "attempts", "cancellation_requested", "total_steps", "completed_steps",
            "candidate_count", "evidence_count",
        ):
            values[name] = int(values[name])
        return ResearchJobRecord(**values)

    @staticmethod
    def _research_event(row: sqlite3.Row) -> ResearchEventRecord:
        values = dict(row)
        for name in ("ordinal", "completed_steps", "total_steps"):
            values[name] = int(values[name])
        return ResearchEventRecord(**values)

    @staticmethod
    def _review_criterion(row: sqlite3.Row) -> ReviewCriterionRecord:
        values = dict(row)
        for name in ("version_count", "current_version_number"):
            values[name] = int(values.get(name, 0))
        values["current_version_id"] = values.get("current_version_id") or ""
        return ReviewCriterionRecord(**values)

    @staticmethod
    def _review_criterion_version(row: sqlite3.Row) -> ReviewCriterionVersionRecord:
        values = dict(row)
        values["version_number"] = int(values["version_number"])
        return ReviewCriterionVersionRecord(**values)

    @staticmethod
    def _review_run(row: sqlite3.Row) -> ReviewRunRecord:
        values = dict(row)
        for name in (
            "attempts", "cancellation_requested", "snapshot_count", "reviewed_count",
            "included_count", "excluded_count", "attention_count",
        ):
            values[name] = int(values[name])
        return ReviewRunRecord(**values)

    @staticmethod
    def _review_decision(row: sqlite3.Row) -> ReviewDecisionRecord:
        values = dict(row)
        raw_citations = json.loads(values.pop("citations_json") or "[]")
        values["citations"] = tuple(
            dict(item) for item in raw_citations if isinstance(item, dict)
        )
        for name in ("ordinal", "validation_sample"):
            values[name] = int(values[name])
        return ReviewDecisionRecord(**values)

    @staticmethod
    def _analysis_run(row: sqlite3.Row) -> AnalysisRunRecord:
        values = dict(row)
        for name in (
            "source_count",
            "unit_count",
            "entity_count",
            "finding_count",
            "capped",
        ):
            values[name] = int(values[name])
        return AnalysisRunRecord(**values)

    @staticmethod
    def _review_finding(row: sqlite3.Row) -> ReviewFindingRecord:
        values = dict(row)
        values["active"] = int(values["active"])
        return ReviewFindingRecord(**values)

    @staticmethod
    def _review_finding_reference(
        row: sqlite3.Row,
    ) -> ReviewFindingReferenceRecord:
        values = dict(row)
        values["ordinal"] = int(values["ordinal"])
        values["unit_number"] = int(values["unit_number"])
        return ReviewFindingReferenceRecord(**values)

    @staticmethod
    def _matter_activity(row: sqlite3.Row) -> MatterActivityRecord:
        values = dict(row)
        values["locator"] = int(values["locator"])
        return MatterActivityRecord(**values)

    def upsert_principal(
        self,
        provider: str,
        provider_subject: str,
        display_name: str,
        login_name: str,
        *,
        preferred_principal_id: str | None = None,
    ) -> PrincipalRecord:
        provider_value = self._safe_text(provider, label="Identity provider", maximum=64)
        if not _PRINCIPAL_PROVIDER.fullmatch(provider_value) or provider_value == "system":
            # "system" is reserved for internal, never-signed-in principals.
            raise WorkspaceProblem("Identity provider is invalid.")
        subject = self._safe_text(
            provider_subject, label="Provider subject", maximum=512
        )
        name = self._safe_text(display_name, label="Display name", maximum=160)
        login = self._safe_text(login_name, label="Login name", maximum=255)
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT principal_id,provider,provider_subject,display_name,login_name,"
                "active,created_at,last_seen_at FROM workbench_principal "
                "WHERE provider=? AND provider_subject=?",
                (provider_value, subject),
            ).fetchone()
            if row is None:
                principal_id = preferred_principal_id or f"principal-{uuid.uuid4().hex}"
                if not _PRINCIPAL_ID.fullmatch(principal_id) or principal_id == AUTOMATIC_DISCOVERY_PRINCIPAL:
                    raise WorkspaceProblem("Principal identity is invalid.")
                self.connection.execute(
                    "INSERT INTO workbench_principal("
                    "principal_id,provider,provider_subject,display_name,login_name,"
                    "active,created_at,last_seen_at) VALUES (?,?,?,?,?,1,?,?)",
                    (principal_id, provider_value, subject, name, login, now, now),
                )
            else:
                principal_id = row["principal_id"]
                self.connection.execute(
                    "UPDATE workbench_principal SET display_name=?,login_name=?,last_seen_at=? "
                    "WHERE principal_id=?",
                    (name, login, now, principal_id),
                )
            current = self.connection.execute(
                "SELECT principal_id,provider,provider_subject,display_name,login_name,"
                "active,created_at,last_seen_at FROM workbench_principal WHERE principal_id=?",
                (principal_id,),
            ).fetchone()
        if current is None:
            raise RuntimeError("principal upsert did not persist")
        return self._principal(current)

    def refresh_principal_display_name(
        self, provider: str, provider_subject: str, display_name: str, *,
        expected_display_name: str | None = None,
    ) -> None:
        """Refresh an existing identity without recording a sign-in or enabling it."""
        provider_value = self._safe_text(provider, label="Identity provider", maximum=64)
        if not _PRINCIPAL_PROVIDER.fullmatch(provider_value) or provider_value == "system":
            # "system" is reserved for internal, never-signed-in principals.
            raise WorkspaceProblem("Identity provider is invalid.")
        subject = self._safe_text(provider_subject, label="Provider subject", maximum=512)
        name = self._safe_text(display_name, label="Display name", maximum=160)
        values = (name, provider_value, subject)
        condition = ""
        if expected_display_name is not None:
            expected = self._safe_text(expected_display_name, label="Expected display name", maximum=160)
            condition = " AND display_name=?"
            values = (*values, expected)
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE workbench_principal SET display_name=? "
                "WHERE provider=? AND provider_subject=?" + condition,
                values,
            )

    def get_principal(self, principal_id: str) -> PrincipalRecord:
        if not _PRINCIPAL_ID.fullmatch(principal_id):
            raise KeyError(principal_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT principal_id,provider,provider_subject,display_name,login_name,"
                "active,created_at,last_seen_at FROM workbench_principal WHERE principal_id=?",
                (principal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(principal_id)
        return self._principal(row)

    def active_principals(self) -> tuple[PrincipalRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT principal_id,provider,provider_subject,display_name,login_name,"
                "active,created_at,last_seen_at FROM workbench_principal "
                "WHERE active=1 ORDER BY display_name,principal_id"
            ).fetchall()
        return tuple(self._principal(row) for row in rows)

    def principal_preference(self, principal_id: str) -> PrincipalPreferenceRecord:
        principal = self.get_principal(principal_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT principal_id,theme,updated_at FROM workbench_principal_preference "
                "WHERE principal_id=?",
                (principal.principal_id,),
            ).fetchone()
        if row is None:
            return PrincipalPreferenceRecord(principal.principal_id, "dark", principal.created_at)
        return self._preference(row)

    def set_principal_theme(
        self, principal_id: str, theme: str
    ) -> PrincipalPreferenceRecord:
        principal = self.get_principal(principal_id)
        selected = (theme or "").strip().casefold()
        if selected not in PREFERENCE_THEMES:
            raise WorkspaceProblem("Choose an available appearance theme.")
        now = self._now()
        with self._lock, self.connection:
            if selected == "dark":
                # Dark is the default. The preference check constraint stores
                # light, dusk, and the unreleased directions, so choosing dark
                # clears a saved override instead of inserting a new value.
                self.connection.execute(
                    "DELETE FROM workbench_principal_preference WHERE principal_id=?",
                    (principal.principal_id,),
                )
                return PrincipalPreferenceRecord(principal.principal_id, "dark", now)
            self.connection.execute(
                "INSERT INTO workbench_principal_preference(principal_id,theme,updated_at) "
                "VALUES (?,?,?) ON CONFLICT(principal_id) DO UPDATE SET "
                "theme=excluded.theme,updated_at=excluded.updated_at",
                (principal.principal_id, selected, now),
            )
            row = self.connection.execute(
                "SELECT principal_id,theme,updated_at FROM workbench_principal_preference "
                "WHERE principal_id=?",
                (principal.principal_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("appearance preference did not persist")
        return self._preference(row)

    def record_matter_activity(
        self,
        matter_id: str,
        principal_id: str,
        activity_kind: str,
        object_id: str,
        *,
        locator: int = 0,
    ) -> MatterActivityRecord:
        """Remember a safe per-user resume target without retaining case content."""

        actor = self.membership(matter_id, principal_id).principal_id
        kind = (activity_kind or "").strip().casefold()
        if kind not in MATTER_ACTIVITY_KINDS:
            raise WorkspaceProblem("That review activity type is unavailable.")
        object_value = self._safe_text(
            object_id,
            label="Review activity object",
            maximum=100,
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", object_value):
            raise WorkspaceProblem("That review activity target is unavailable.")
        if isinstance(locator, bool) or not isinstance(locator, int) or not 0 <= locator <= 43_200_000:
            raise WorkspaceProblem("That review position is unavailable.")
        now = self._now()
        activity_id = f"matter-activity-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO workbench_matter_activity("
                "activity_id,matter_id,principal_id,activity_kind,object_id,locator,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(matter_id,principal_id,activity_kind,object_id) DO UPDATE SET "
                "locator=excluded.locator,updated_at=excluded.updated_at",
                (
                    activity_id,
                    matter_id,
                    actor,
                    kind,
                    object_value,
                    locator,
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_matter_activity WHERE matter_id=? "
                "AND principal_id=? AND activity_kind=? AND object_id=?",
                (matter_id, actor, kind, object_value),
            ).fetchone()
        if row is None:
            raise RuntimeError("review activity did not persist")
        return self._matter_activity(row)

    def recent_matter_activities(
        self, matter_id: str, principal_id: str, *, limit: int = 12
    ) -> tuple[MatterActivityRecord, ...]:
        actor = self.membership(matter_id, principal_id).principal_id
        bounded = min(max(int(limit), 1), 40)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_matter_activity WHERE matter_id=? "
                "AND principal_id=? ORDER BY updated_at DESC,activity_id DESC LIMIT ?",
                (matter_id, actor, bounded),
            ).fetchall()
        return tuple(self._matter_activity(row) for row in rows)

    def remove_matter_activity_object(
        self, matter_id: str, activity_kind: str, object_id: str
    ) -> None:
        kind = (activity_kind or "").strip().casefold()
        if kind not in MATTER_ACTIVITY_KINDS:
            raise ValueError("invalid review activity type")
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM workbench_matter_activity WHERE matter_id=? "
                "AND activity_kind=? AND object_id=?",
                (matter_id, kind, object_id),
            )

    def create_session(
        self,
        principal_id: str,
        token_digest: str,
        auth_method: str,
        idle_expires_at: str,
        absolute_expires_at: str,
        application_roles: frozenset[str] = frozenset(),
        *, binding_digest: str | None = None,
    ) -> SessionRecord:
        principal = self.get_principal(principal_id)
        if not principal.active:
            raise WorkspaceProblem("This identity is not active.")
        if not re.fullmatch(r"[0-9a-f]{64}", token_digest):
            raise ValueError("invalid session token digest")
        method = self._safe_text(auth_method, label="Authentication method", maximum=64)
        if binding_digest is not None and (method != 'local' or not re.fullmatch(r'[0-9a-f]{64}', binding_digest)):
            raise ValueError('invalid lifecycle session binding')
        if not _PRINCIPAL_PROVIDER.fullmatch(method):
            raise ValueError("invalid authentication method")
        if not isinstance(application_roles, frozenset) or not application_roles.issubset(
            {"administrator"}
        ):
            raise ValueError("invalid session application roles")
        encoded_roles = json.dumps(sorted(application_roles), separators=(",", ":"))
        now = self._now()
        session_id = f"session-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO workbench_session("
                "session_id,token_digest,principal_id,auth_method,application_roles,created_at,last_seen_at,"
                "idle_expires_at,absolute_expires_at,revoked_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                (
                    session_id,
                    token_digest,
                    principal_id,
                    method,
                    encoded_roles,
                    now,
                    now,
                    idle_expires_at,
                    absolute_expires_at,
                ),
            )
            if binding_digest is not None:
                self.connection.execute('INSERT INTO workbench_case_session_binding VALUES (?,?)',
                                        (session_id, binding_digest))
            row = self.connection.execute(
                "SELECT session_id,token_digest,principal_id,auth_method,application_roles,created_at,last_seen_at,"
                "idle_expires_at,absolute_expires_at,revoked_at FROM workbench_session "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("session creation did not persist")
        return self._session(row)

    def resolve_session(
        self, token_digest: str, *, now: str, next_idle_expires_at: str, read_only: bool = False
    ) -> tuple[SessionRecord, PrincipalRecord] | None:
        if not re.fullmatch(r"[0-9a-f]{64}", token_digest):
            return None
        # Read-only authority checks may run inside a caller-owned transaction.
        # They must neither touch session state nor commit/roll back that unit.
        with self._lock, (nullcontext() if read_only else self.connection):
            row = self.connection.execute(
                "SELECT s.session_id,s.token_digest,s.principal_id,s.auth_method,s.application_roles,s.created_at,"
                "s.last_seen_at,s.idle_expires_at,s.absolute_expires_at,s.revoked_at,"
                "p.provider,p.provider_subject,p.display_name,p.login_name,p.active,"
                "p.created_at AS principal_created_at,p.last_seen_at AS principal_last_seen_at "
                "FROM workbench_session s JOIN workbench_principal p "
                "ON p.principal_id=s.principal_id WHERE s.token_digest=?",
                (token_digest,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None or not row["active"]:
                return None
            if row["idle_expires_at"] <= now or row["absolute_expires_at"] <= now:
                if not read_only:
                    self.connection.execute(
                        "UPDATE workbench_session SET revoked_at=? "
                        "WHERE session_id=? AND revoked_at IS NULL",
                        (now, row["session_id"]),
                    )
                return None
            idle = min(next_idle_expires_at, row["absolute_expires_at"])
            if not read_only:
                self.connection.execute(
                    "UPDATE workbench_session SET last_seen_at=?,idle_expires_at=? "
                    "WHERE session_id=? AND revoked_at IS NULL",
                    (now, idle, row["session_id"]),
                )
            session_row = self.connection.execute(
                "SELECT session_id,token_digest,principal_id,auth_method,application_roles,created_at,last_seen_at,"
                "idle_expires_at,absolute_expires_at,revoked_at FROM workbench_session "
                "WHERE session_id=?",
                (row["session_id"],),
            ).fetchone()
            principal_row = self.connection.execute(
                "SELECT principal_id,provider,provider_subject,display_name,login_name,"
                "active,created_at,last_seen_at FROM workbench_principal WHERE principal_id=?",
                (row["principal_id"],),
            ).fetchone()
        if session_row is None or principal_row is None:
            return None
        return self._session(session_row), self._principal(principal_row)

    def revoke_session(self, session_id: str) -> bool:
        if not _IDENTIFIER.fullmatch(session_id):
            return False
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_session SET revoked_at=? "
                "WHERE session_id=? AND revoked_at IS NULL",
                (now, session_id),
            ).rowcount
        return changed == 1

    def session_by_id(self, session_id: str) -> SessionRecord:
        if not _IDENTIFIER.fullmatch(session_id):
            raise KeyError(session_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT session_id,token_digest,principal_id,auth_method,application_roles,created_at,last_seen_at,"
                "idle_expires_at,absolute_expires_at,revoked_at FROM workbench_session "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self._session(row)

    def create_oidc_transaction(
        self,
        state_digest: str,
        next_path: str,
        expires_at: str,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", state_digest):
            raise ValueError("invalid OIDC state digest")
        destination = self._safe_text(
            next_path,
            label="Post-login destination",
            maximum=2_048,
        )
        if not destination.startswith("/") or destination.startswith("//"):
            raise ValueError("invalid post-login destination")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM workbench_oidc_transaction "
                "WHERE expires_at<=? OR consumed_at IS NOT NULL",
                (now,),
            )
            self.connection.execute(
                "INSERT INTO workbench_oidc_transaction("
                "state_digest,next_path,created_at,expires_at,consumed_at) "
                "VALUES (?,?,?,?,NULL)",
                (state_digest, destination, now, expires_at),
            )

    def consume_oidc_transaction(self, state_digest: str, *, now: str) -> str | None:
        """Atomically consume one unexpired authorization transaction."""

        if not re.fullmatch(r"[0-9a-f]{64}", state_digest):
            return None
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT next_path,expires_at,consumed_at "
                "FROM workbench_oidc_transaction WHERE state_digest=?",
                (state_digest,),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] <= now
            ):
                return None
            changed = self.connection.execute(
                "UPDATE workbench_oidc_transaction SET consumed_at=? "
                "WHERE state_digest=? AND consumed_at IS NULL AND expires_at>?",
                (now, state_digest, now),
            ).rowcount
        return row["next_path"] if changed == 1 else None

    def team_groups(self) -> tuple[dict, ...]:
        with self._lock:
            return tuple(dict(row) for row in self.connection.execute(
                "SELECT g.*,count(m.principal_id) AS member_count "
                "FROM workbench_team_group g LEFT JOIN workbench_team_group_member m "
                "ON m.group_id=g.group_id GROUP BY g.group_id ORDER BY g.name,g.group_id"
            ).fetchall())

    def team_group_members(self, group_id: str) -> tuple[PrincipalRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT p.* FROM workbench_principal p JOIN workbench_team_group_member m "
                "ON m.principal_id=p.principal_id WHERE m.group_id=? ORDER BY p.display_name,p.principal_id",
                (group_id,),
            ).fetchall()
        return tuple(self._principal(row) for row in rows)

    def matter_group_grants(self, matter_id: str) -> tuple[dict, ...]:
        with self._lock:
            return tuple(dict(row) for row in self.connection.execute(
                "SELECT g.group_id,g.name,mg.granted_by,mg.created_at "
                "FROM workbench_matter_group_grant mg JOIN workbench_team_group g "
                "ON g.group_id=mg.group_id WHERE mg.matter_id=? ORDER BY g.name,g.group_id",
                (matter_id,),
            ).fetchall())

    def access_reasons(self, matter_id: str, principal_id: str) -> tuple[str, ...]:
        with self._lock:
            self.membership(matter_id, principal_id)
            direct = self.connection.execute(
                "SELECT role FROM workbench_matter_membership WHERE matter_id=? "
                "AND principal_id=? AND state='active'", (matter_id, principal_id),
            ).fetchone()
            groups = self.connection.execute(
                "SELECT g.name FROM workbench_team_group g "
                "JOIN workbench_matter_group_grant mg ON mg.group_id=g.group_id "
                "JOIN workbench_team_group_member gm ON gm.group_id=g.group_id "
                "WHERE mg.matter_id=? AND gm.principal_id=? ORDER BY g.name,g.group_id",
                (matter_id, principal_id),
            ).fetchall()
            return (("Owner" if direct["role"] == "owner" else "Direct member",) if direct else ()) + tuple(
                f"Group: {row['name']}" for row in groups
            )

    def _team_actor(self, actor_id: str, administrator_override: bool) -> None:
        # The authenticated service supplies administrator authority, just as
        # for direct grants. A system role itself grants no matter membership.
        actor = self.get_principal(actor_id)
        if not (administrator_override and actor.active and
                self.principal_enabled(actor.provider, actor.provider_subject)):
            raise WorkspaceProblem("Administrator access is required to manage reusable groups.")

    def create_team_group(self, name: str, actor_id: str, *, administrator_override: bool = False,
                          session_id: str | None = None, request_id: str = "team-group") -> str:
        name = self._safe_text(name, label="Group name", maximum=100)
        group_id = f"group-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self._team_actor(actor_id, administrator_override)
            try:
                self.connection.execute(
                    "INSERT INTO workbench_team_group VALUES (?,?,?,?)",
                    (group_id, name, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkspaceProblem("Choose a unique group name.") from exc
            self._append_audit_event_locked(
                actor_principal_id=actor_id, session_id=session_id, matter_id=None,
                request_id=request_id, action="team_group.create", outcome="success",
                object_type="team_group", object_id=group_id,
            )
        return group_id

    def set_team_group_member(self, group_id: str, principal_id: str, actor_id: str, *,
                              present: bool, administrator_override: bool = False,
                              session_id: str | None = None, request_id: str = "team-group") -> None:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self._team_actor(actor_id, administrator_override)
            if self.connection.execute("SELECT 1 FROM workbench_team_group WHERE group_id=?", (group_id,)).fetchone() is None:
                raise KeyError(group_id)
            target = self.get_principal(principal_id)
            if present:
                if not target.active or not self.principal_enabled(target.provider, target.provider_subject):
                    raise WorkspaceProblem("That identity is not active.")
                changed = self.connection.execute(
                    "INSERT OR IGNORE INTO workbench_team_group_member VALUES (?,?,?,?)",
                    (group_id, principal_id, actor_id, self._now()),
                ).rowcount
            else:
                changed = self.connection.execute(
                    "DELETE FROM workbench_team_group_member WHERE group_id=? AND principal_id=?",
                    (group_id, principal_id),
                ).rowcount
            if changed:
                self._append_audit_event_locked(
                    actor_principal_id=actor_id, session_id=session_id, matter_id=None,
                    request_id=request_id, action="team_group.member_add" if present else "team_group.member_remove",
                    outcome="success", object_type="team_group", object_id=group_id,
                    details={"principal_id": principal_id},
                )

    def set_matter_group_grant(self, matter_id: str, group_id: str, actor_id: str, *,
                               present: bool, administrator_override: bool = False,
                               session_id: str | None = None, request_id: str = "team-group") -> None:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if administrator_override:
                self._team_actor(actor_id, True)
            elif self.membership(matter_id, actor_id).role != "owner":
                raise WorkspaceProblem("Only a matter owner can change the case team.")
            if self.connection.execute("SELECT 1 FROM workbench_matter_lifecycle WHERE matter_id=? AND state='active'", (matter_id,)).fetchone() is None:
                raise KeyError(matter_id)
            if self.connection.execute("SELECT 1 FROM workbench_team_group WHERE group_id=?", (group_id,)).fetchone() is None:
                raise KeyError(group_id)
            if present:
                changed = self.connection.execute(
                    "INSERT OR IGNORE INTO workbench_matter_group_grant VALUES (?,?,?,?)",
                    (matter_id, group_id, actor_id, self._now()),
                ).rowcount
            else:
                changed = self.connection.execute(
                    "DELETE FROM workbench_matter_group_grant WHERE matter_id=? AND group_id=?", (matter_id, group_id),
                ).rowcount
            if changed:
                self._append_audit_event_locked(
                    actor_principal_id=actor_id, session_id=session_id, matter_id=matter_id,
                    request_id=request_id, action="membership.group_add" if present else "membership.group_remove",
                    outcome="success", object_type="team_group", object_id=group_id,
                )

    def membership(self, matter_id: str, principal_id: str) -> MatterMembershipRecord:
        with self._lock:
            row = self.connection.execute(
                "SELECT mm.matter_id,mm.principal_id,mm.role,mm.state,mm.granted_by,"
                "mm.created_at,mm.updated_at,mm.revoked_at,p.display_name,p.login_name "
                "FROM workbench_effective_membership mm JOIN workbench_principal p "
                "ON p.principal_id=mm.principal_id "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=mm.matter_id "
                "WHERE mm.matter_id=? AND mm.principal_id=? AND mm.state='active' "
                "AND p.active=1 AND ml.state='active'",
                (matter_id, principal_id),
            ).fetchone()
        if row is None:
            raise KeyError(matter_id)
        return self._membership(row)

    def _authorize_export_read(
        self,
        matter_id: str,
        actor_id: str,
        *,
        administrator_override: bool = False,
    ) -> None:
        """Validate a matter-scoped export reader without impersonating its owner."""

        if not administrator_override:
            try:
                self.membership(matter_id, actor_id)
                return
            except KeyError:
                actor = self._safe_text(
                    actor_id, label="Actor identity", maximum=100
                )
                with self._lock:
                    row = self.connection.execute(
                        "SELECT 1 FROM workbench_matter matter "
                        "JOIN workbench_matter_lifecycle lifecycle "
                        "ON lifecycle.matter_id=matter.matter_id "
                        "JOIN workbench_principal principal "
                        "ON principal.principal_id=matter.owner_id "
                        "WHERE matter.matter_id=? AND matter.owner_id=? "
                        "AND principal.active=1 AND recordbench_principal_enabled(principal.provider,principal.provider_subject)=1 "
                        "AND lifecycle.state='purge_failed'",
                        (matter_id, actor),
                    ).fetchone()
                if row is None:
                    raise
                return
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        principal = self.get_principal(actor)
        if not principal.active or not self.principal_enabled(principal.provider, principal.provider_subject):
            raise KeyError(matter_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM workbench_matter matter "
                "JOIN workbench_matter_lifecycle lifecycle "
                "ON lifecycle.matter_id=matter.matter_id "
                "WHERE matter.matter_id=? AND lifecycle.state<>'deleted'",
                (matter_id,),
            ).fetchone()
        if row is None:
            raise KeyError(matter_id)

    def source_catalog_for_export(
        self,
        matter_id: str,
        actor_id: str,
        *,
        administrator_override: bool = False,
    ) -> tuple[SourceCatalogRecord, ...]:
        """Read the durable inventory without reconciling a closing matter."""

        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        with self._lock:
            rows = self.connection.execute(
                "SELECT c.*,COALESCE(o.collection_id,'') AS collection_id,"
                "COALESCE(sc.name,'Unfiled') AS collection_name,"
                "COALESCE(o.review_state,'unreviewed') AS review_state,COALESCE(o.source_role,'unclassified') AS source_role,"
                "COALESCE(o.added_at,c.cataloged_at) AS added_at "
                "FROM workbench_source_catalog c "
                "LEFT JOIN workbench_source_organization o "
                "ON o.matter_id=c.matter_id AND o.document_id=c.document_id "
                "LEFT JOIN workbench_source_collection sc "
                "ON sc.matter_id=o.matter_id AND sc.collection_id=o.collection_id "
                "WHERE c.matter_id=? ORDER BY c.cataloged_at,c.document_id",
                (matter_id,),
            ).fetchall()
        return tuple(self._source_catalog(row) for row in rows)

    def members(self, matter_id: str) -> tuple[MatterMembershipRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT mm.matter_id,mm.principal_id,mm.role,mm.state,mm.granted_by,"
                "mm.created_at,mm.updated_at,mm.revoked_at,p.display_name,p.login_name "
                "FROM workbench_effective_membership mm JOIN workbench_principal p "
                "ON p.principal_id=mm.principal_id "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=mm.matter_id "
                "WHERE mm.matter_id=? AND mm.state='active' AND p.active=1 "
                "AND ml.state='active' "
                "ORDER BY CASE mm.role WHEN 'owner' THEN 0 ELSE 1 END,p.display_name,p.principal_id",
                (matter_id,),
            ).fetchall()
        return tuple(self._membership(row) for row in rows)

    def direct_members(self, matter_id: str) -> tuple[MatterMembershipRecord, ...]:
        """Retained direct grants, including disabled people, for grant management."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT mm.matter_id,mm.principal_id,mm.role,mm.state,mm.granted_by,"
                "mm.created_at,mm.updated_at,mm.revoked_at,p.display_name,p.login_name "
                "FROM workbench_matter_membership mm JOIN workbench_principal p "
                "ON p.principal_id=mm.principal_id WHERE mm.matter_id=? AND mm.state='active' "
                "ORDER BY p.display_name,p.principal_id", (matter_id,),
            ).fetchall()
        return tuple(self._membership(row) for row in rows)

    def add_member(
        self,
        matter_id: str,
        target_principal_id: str,
        granted_by: str,
        *,
        administrator_override: bool = False,
    ) -> MatterMembershipRecord:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if administrator_override:
                grantor_principal = self.get_principal(granted_by)
                if not grantor_principal.active:
                    raise WorkspaceProblem("The administrator identity is not active.")
            else:
                grantor = self.membership(matter_id, granted_by)
                if grantor.role != "owner":
                    raise WorkspaceProblem("Only a matter owner can change the case team.")
            target = self.get_principal(target_principal_id)
            if not target.active or not self.principal_enabled(target.provider, target.provider_subject):
                raise WorkspaceProblem("That identity is not active.")
            now = self._now()
            existing = self.connection.execute(
                "SELECT role FROM workbench_matter_membership "
                "WHERE matter_id=? AND principal_id=?",
                (matter_id, target_principal_id),
            ).fetchone()
            if existing is not None and existing["role"] == "owner":
                return self.membership(matter_id, target_principal_id)
            self.connection.execute(
                "INSERT INTO workbench_matter_membership("
                "matter_id,principal_id,role,state,granted_by,created_at,updated_at,revoked_at) "
                "VALUES (?,?,'member','active',?,?,?,NULL) "
                "ON CONFLICT(matter_id,principal_id) DO UPDATE SET "
                "role='member',state='active',granted_by=excluded.granted_by,"
                "updated_at=excluded.updated_at,revoked_at=NULL",
                (matter_id, target_principal_id, granted_by, now, now),
            )
            return self.membership(matter_id, target_principal_id)

    def revoke_member(
        self,
        matter_id: str,
        target_principal_id: str,
        revoked_by: str,
        *,
        administrator_override: bool = False,
    ) -> None:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if administrator_override:
                grantor_principal = self.get_principal(revoked_by)
                if not grantor_principal.active:
                    raise WorkspaceProblem("The administrator identity is not active.")
            else:
                grantor = self.membership(matter_id, revoked_by)
                if grantor.role != "owner":
                    raise WorkspaceProblem("Only a matter owner can change the case team.")
            target = self.connection.execute(
                "SELECT role FROM workbench_matter_membership WHERE matter_id=? AND principal_id=? AND state='active'",
                (matter_id, target_principal_id),
            ).fetchone()
            if target is None:
                raise KeyError(target_principal_id)
            if target["role"] == "owner":
                raise WorkspaceProblem("The matter owner cannot be removed.")
            now = self._now()
            changed = self.connection.execute(
                "UPDATE workbench_matter_membership SET state='revoked',updated_at=?,revoked_at=? "
                "WHERE matter_id=? AND principal_id=? AND state='active' AND role='member'",
                (now, now, matter_id, target_principal_id),
            ).rowcount
            if changed != 1:
                raise KeyError(target_principal_id)

    @staticmethod
    def _audit_details(details: Mapping[str, object] | None) -> str:
        normalized: dict[str, object] = {}
        for key, value in dict(details or {}).items():
            if key in {
                "count",
                "result_count",
                "source_count",
                "unit_count",
                "section_count",
            }:
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
                    raise ValueError("invalid audit count")
            elif key in {"created", "capped"}:
                if not isinstance(value, bool):
                    raise ValueError("invalid audit flag")
            elif key == "type":
                if value not in {"notebook", "finding", "media_clip", "answer", "manual"}:
                    raise ValueError("invalid audit material type")
            elif key == "principal_id":
                if not isinstance(value, str) or not _PRINCIPAL_ID.fullmatch(value):
                    raise ValueError("invalid audit principal")
            elif key == "role":
                if value not in {"owner", "member", "administrator"}:
                    raise ValueError("invalid audit role")
            elif key == "state":
                if value not in _AUDIT_DETAIL_STATES:
                    raise ValueError("invalid audit state")
            elif key == "auth_method":
                if value not in {"preview", "test", "local", "oidc", "spnego"}:
                    raise ValueError("invalid audit authentication method")
            elif key == "format":
                if value not in {
                    "markdown", "docx", "csv", "zip", "txt", "srt", "vtt",
                    "json", "mp4", "wav",
                }:
                    raise ValueError("invalid audit export format")
            elif key == "kind":
                if value not in {
                    "answer", "conversation", "matter", "notebook", "notebook_item",
                    "transcript", "transcript_summary", "media_clip", "intake_receipt",
                }:
                    raise ValueError("invalid audit export kind")
            else:
                raise ValueError("unsupported audit metadata")
            normalized[key] = value
        return json.dumps(normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    def append_audit_event(
        self,
        *,
        actor_principal_id: str | None,
        session_id: str | None,
        matter_id: str | None,
        request_id: str,
        action: str,
        outcome: str,
        object_type: str | None = None,
        object_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> AuditEventRecord:
        with self._lock, self.connection:
            return self._append_audit_event_locked(
                actor_principal_id=actor_principal_id, session_id=session_id,
                matter_id=matter_id, request_id=request_id, action=action,
                outcome=outcome, object_type=object_type, object_id=object_id,
                details=details,
            )

    def _append_audit_event_locked(
        self,
        *,
        actor_principal_id: str | None,
        session_id: str | None,
        matter_id: str | None,
        request_id: str,
        action: str,
        outcome: str,
        object_type: str | None = None,
        object_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> AuditEventRecord:
        request = self._safe_text(request_id, label="Audit request", maximum=96)
        action_value = self._safe_text(action, label="Audit action", maximum=80)
        if not _AUDIT_NAME.fullmatch(action_value):
            raise ValueError("invalid audit action")
        if outcome not in {"success", "denied", "failure"}:
            raise ValueError("invalid audit outcome")
        kind = None
        identifier = None
        if object_type is not None:
            kind = self._safe_text(object_type, label="Audit object type", maximum=80)
            if not _AUDIT_NAME.fullmatch(kind):
                raise ValueError("invalid audit object type")
        if object_id is not None:
            identifier = self._safe_text(object_id, label="Audit object", maximum=100)
        encoded = self._audit_details(details)
        event_id = f"audit-{uuid.uuid4().hex}"
        occurred_at = self._now()
        self.connection.execute(
            "INSERT INTO workbench_audit_event("
            "event_id,occurred_at,actor_principal_id,session_id,matter_id,request_id,"
            "action,outcome,object_type,object_id,details_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                occurred_at,
                actor_principal_id,
                session_id,
                matter_id,
                request,
                action_value,
                outcome,
                kind,
                identifier,
                encoded,
            ),
        )
        row = self.connection.execute(
            "SELECT event_id,occurred_at,actor_principal_id,session_id,matter_id,"
            "request_id,action,outcome,object_type,object_id,details_json "
            "FROM workbench_audit_event WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("audit event did not persist")
        return self._audit_event(row)

    def audit_events(self, matter_id: str | None = None) -> tuple[AuditEventRecord, ...]:
        query = (
            "SELECT event_id,occurred_at,actor_principal_id,session_id,matter_id,"
            "request_id,action,outcome,object_type,object_id,details_json "
            "FROM workbench_audit_event"
        )
        parameters: tuple[object, ...] = ()
        if matter_id is not None:
            query += " WHERE matter_id=?"
            parameters = (matter_id,)
        query += " ORDER BY occurred_at,event_id"
        with self._lock:
            rows = self.connection.execute(query, parameters).fetchall()
        return tuple(self._audit_event(row) for row in rows)

    def member_access_seen(self, matter_id: str, principal_id: str, *, since: str) -> bool:
        """Whether a current member opened this matter after the current grant."""
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM workbench_audit_event WHERE matter_id=? "
                "AND actor_principal_id=? AND occurred_at>? AND outcome='success' "
                "AND action IN ('matter.home','matter.open') LIMIT 1",
                (matter_id, principal_id, since),
            ).fetchone()
        return row is not None

    def create_matter(
        self,
        display_name: str,
        descriptor: str,
        owner_id: str,
        *,
        retention_days: int | None = None,
    ) -> MatterRecord:
        name = self._safe_text(display_name, label="Matter name", maximum=140)
        detail = self._safe_text(
            descriptor, label="Matter description", maximum=240, required=False
        )
        owner = self._safe_text(owner_id, label="Owner identity", maximum=100)
        principal = self.get_principal(owner)
        if not principal.active:
            raise WorkspaceProblem("The owner identity is not active.")
        if retention_days is not None and (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 1 <= retention_days <= 365
        ):
            raise WorkspaceProblem("Choose a review period between 1 and 365 days.")
        now_value = self.current_time()
        now = self._timestamp(now_value)
        matter_id = f"ci-matter-{uuid.uuid4().hex}"
        slug = f"m-{uuid.uuid4().hex[:12]}"
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO workbench_matter(matter_id,slug,display_name,descriptor,owner_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (matter_id, slug, name, detail, owner, now, now),
            )
            self.connection.execute(
                "INSERT INTO workbench_matter_lifecycle(matter_id,state,updated_at) "
                "VALUES (?,'active',?)",
                (matter_id, now),
            )
            # Legacy callers may supply their old review period; it is only a
            # check-in reminder now and never establishes a deletion deadline.
            next_check_in = self._timestamp(now_value + timedelta(days=retention_days or 90))
            self.connection.execute(
                "INSERT INTO workbench_case_policy(matter_id,mode,next_check_in_at,selected_by,created_at,updated_at) "
                "VALUES (?,'ongoing',?,?,?,?)", (matter_id, next_check_in, owner, now, now),
            )
            self._create_conversation_locked(
                matter_id,
                "Case review",
                now=now,
                actor_id=owner,
            )
            self.connection.execute(
                "INSERT INTO workbench_matter_membership("
                "matter_id,principal_id,role,state,granted_by,created_at,updated_at,revoked_at) "
                "VALUES (?,?,'owner','active',?,?,?,NULL)",
                (matter_id, owner, owner, now, now),
            )
        return MatterRecord(matter_id, slug, name, detail, owner, now, now)

    def rename_matter(
        self, slug: str, actor_id: str, display_name: str, *,
        expected_name: str, request_id: str, session_id: str | None = None,
        administrator_override: bool = False,
    ) -> MatterRecord:
        """Rename under already validated administrator authority, if needed.

        Serialize authorization, current-name comparison, mutation, and audit
        across connections. No source or derived-work identity changes.
        """
        if not _SLUG.fullmatch(slug):
            raise KeyError(slug)
        name = self._safe_text(display_name, label="Matter name", maximum=140)
        expected = self._safe_text(expected_name, label="Previous matter name", maximum=140)
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                "m.created_at,m.updated_at FROM workbench_matter m "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                "JOIN workbench_principal p ON p.principal_id=? AND p.active=1 "
                "WHERE m.slug=? AND ml.state='active' AND (?=1 OR (m.owner_id=? "
                "AND EXISTS (SELECT 1 FROM workbench_effective_membership mm "
                "WHERE mm.matter_id=m.matter_id AND mm.principal_id=p.principal_id "
                "AND mm.role='owner' AND mm.state='active')))",
                (actor_id, slug, int(administrator_override), actor_id),
            ).fetchone()
            if row is None:
                raise KeyError(slug)
            matter = self._matter(row)
            if matter.display_name != expected:
                raise MatterNameConflict(
                    "The matter name changed while you were editing. Your proposed name is below. "
                    "Review the current name before saving again."
                )
            if name == matter.display_name:
                return matter
            self.connection.execute(
                "UPDATE workbench_matter SET display_name=?,updated_at=? WHERE matter_id=?",
                (name, self._now(), matter.matter_id),
            )
            self._append_audit_event_locked(
                actor_principal_id=actor_id, session_id=session_id,
                matter_id=matter.matter_id, request_id=request_id,
                action="matter.rename", outcome="success", object_type="matter",
                object_id=matter.matter_id,
                details={"role": "owner" if matter.owner_id == actor_id else "administrator"},
            )
            return self.get_active_matter(slug)

    def list_matters(self, principal_id: str) -> tuple[MatterRecord, ...]:
        principal = self._safe_text(principal_id, label="Principal identity", maximum=100)
        with self._lock:
            rows = self.connection.execute(
                "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                "m.created_at,m.updated_at FROM workbench_matter m "
                "JOIN workbench_effective_membership mm ON mm.matter_id=m.matter_id "
                "JOIN workbench_principal p ON p.principal_id=mm.principal_id "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                "WHERE mm.principal_id=? AND mm.state='active' AND p.active=1 "
                "AND ml.state='active' "
                "ORDER BY m.updated_at DESC,m.display_name",
                (principal,),
            ).fetchall()
        return tuple(self._matter(row) for row in rows)

    def all_matters(self) -> tuple[MatterRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                "m.created_at,m.updated_at FROM workbench_matter m "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                "WHERE ml.state='active' ORDER BY m.updated_at DESC,m.display_name"
            ).fetchall()
        return tuple(self._matter(row) for row in rows)

    def closure_recovery_matters(
        self,
        actor_id: str,
        *,
        administrator_override: bool = False,
    ) -> tuple[MatterRecord, ...]:
        """List failed closures without restoring ordinary matter access."""

        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        principal = self.get_principal(actor)
        if not principal.active:
            return ()
        owner_clause = "" if administrator_override else "AND m.owner_id=? "
        parameters = () if administrator_override else (actor,)
        with self._lock:
            rows = self.connection.execute(
                "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                "m.created_at,m.updated_at FROM workbench_matter m "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                "WHERE ml.state='purge_failed' "
                + owner_clause
                + "ORDER BY ml.updated_at DESC,m.display_name",
                parameters,
            ).fetchall()
        return tuple(self._matter(row) for row in rows)

    def get_matter(self, slug: str, principal_id: str) -> MatterRecord:
        if not _SLUG.fullmatch(slug):
            raise KeyError(slug)
        with self._lock:
            row = self.connection.execute(
                "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                "m.created_at,m.updated_at FROM workbench_matter m "
                "JOIN workbench_effective_membership mm ON mm.matter_id=m.matter_id "
                "JOIN workbench_principal p ON p.principal_id=mm.principal_id "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                "WHERE m.slug=? AND mm.principal_id=? AND mm.state='active' AND p.active=1 "
                "AND ml.state='active'",
                (slug, principal_id),
            ).fetchone()
        if row is None:
            raise KeyError(slug)
        return self._matter(row)

    def get_active_matter(self, slug: str) -> MatterRecord:
        if not _SLUG.fullmatch(slug):
            raise KeyError(slug)
        with self._lock:
            row = self.connection.execute(
                "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                "m.created_at,m.updated_at FROM workbench_matter m "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                "WHERE m.slug=? AND ml.state='active'",
                (slug,),
            ).fetchone()
        if row is None:
            raise KeyError(slug)
        return self._matter(row)

    def active_matter_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(row[0] for row in self.connection.execute(
                "SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active' ORDER BY matter_id"
            ))

    def get_matter_by_id(self, matter_id: str, principal_id: str | None = None) -> MatterRecord:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        with self._lock:
            if principal_id is None:
                row = self.connection.execute(
                    "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                    "m.created_at,m.updated_at FROM workbench_matter m "
                    "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                    "WHERE m.matter_id=? AND ml.state='active'",
                    (matter_id,),
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                    "m.created_at,m.updated_at FROM workbench_matter m "
                    "JOIN workbench_effective_membership mm ON mm.matter_id=m.matter_id "
                    "JOIN workbench_principal p ON p.principal_id=mm.principal_id "
                    "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                    "WHERE m.matter_id=? AND mm.principal_id=? "
                    "AND mm.state='active' AND p.active=1 AND ml.state='active'",
                    (matter_id, principal_id),
                ).fetchone()
        if row is None:
            raise KeyError(matter_id)
        return self._matter(row)

    def matter_lifecycle(self, matter_id: str) -> MatterLifecycleRecord:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT matter_id,state,purge_id,requested_by,requested_at,finished_at,"
                "error_code,source_count,conversation_count,message_count,updated_at "
                "FROM workbench_matter_lifecycle WHERE matter_id=?",
                (matter_id,),
            ).fetchone()
        if row is None:
            raise KeyError(matter_id)
        return self._lifecycle(row)

    def matter_retention(self, matter_id: str) -> MatterRetentionRecord | None:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        with self._lock:
            exists = self.connection.execute(
                "SELECT 1 FROM workbench_matter WHERE matter_id=?", (matter_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(matter_id)
            row = self.connection.execute(
                "SELECT matter_id,expires_at,purge_after,scheduled_by,created_at,updated_at "
                "FROM workbench_matter_retention WHERE matter_id=?",
                (matter_id,),
            ).fetchone()
        return self._retention(row) if row is not None else None

    def set_matter_retention(
        self,
        matter_id: str,
        actor_id: str,
        expires_at: datetime,
        *,
        administrator_override: bool = False,
    ) -> MatterRetentionRecord:
        raise WorkspaceProblem("End dates no longer schedule deletion. Use the matter check-in controls.")

    def due_matter_retentions(self) -> tuple[MatterRetentionRecord, ...]:
        """Compatibility inventory: no date is authorization for deletion."""
        return ()

    def matter_activity_at(self, matter_id: str) -> str:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT MAX(activity_at) FROM ("
                "SELECT updated_at AS activity_at FROM workbench_matter WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_conversation WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_upload_session WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_ingest_job WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_answer_job WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_research_job WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_review_run WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_media_job WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_notebook_item WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_report WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_source_catalog WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_matter_activity WHERE matter_id=? "
                "UNION ALL SELECT updated_at FROM workbench_intake_receipt WHERE matter_id=?"
                ")",
                (matter_id,) * 13,
            ).fetchone()
        if row is None or row[0] is None:
            raise KeyError(matter_id)
        return str(row[0])

    def matter_for_closure(
        self,
        slug: str,
        actor_id: str,
        *,
        administrator_override: bool = False,
    ) -> tuple[MatterRecord, MatterLifecycleRecord]:
        if not _SLUG.fullmatch(slug):
            raise KeyError(slug)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        principal = self.get_principal(actor)
        if not principal.active:
            if administrator_override:
                raise WorkspaceProblem("The administrator identity is not active.")
            raise KeyError(slug)
        owner_clause = "" if administrator_override else "AND m.owner_id=? "
        parameters = (slug,) if administrator_override else (slug, actor)
        with self._lock:
            row = self.connection.execute(
                "SELECT m.matter_id,m.slug,m.display_name,m.descriptor,m.owner_id,"
                "m.created_at,m.updated_at FROM workbench_matter m "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
                "WHERE m.slug=? "
                + owner_clause
                + "AND ml.state IN ('active','closing','purging','purge_failed')",
                parameters,
            ).fetchone()
        if row is None:
            raise KeyError(slug)
        matter = self._matter(row)
        return matter, self.matter_lifecycle(matter.matter_id)

    def _active_matter_work_counts_locked(self, matter_id: str) -> dict[str, int]:
        """Count every durable worker that must finish before a matter purge.

        Callers hold ``self._lock``. Keeping this as one fail-closed inventory
        prevents the confirmation page and the transactional purge claim from
        drifting as new optional processing queues are added.
        """

        queries = {
            "ingestion": (
                "workbench_ingest_job",
                "state IN ('queued','running')",
            ),
            "answers": (
                "workbench_answer_job",
                "state IN ('queued','running')",
            ),
            "research": (
                "workbench_research_job",
                "state IN ('queued','running')",
            ),
            "reports": (
                "workbench_report_compilation_job",
                "state IN ('queued','running')",
            ),
            "reviews": (
                "workbench_review_run",
                "state IN ('queued','running')",
            ),
            "uploads": (
                "workbench_upload_item",
                "state IN ('pending','uploading','uploaded')",
            ),
            "media": (
                "workbench_media_job",
                "state IN ('queued','running')",
            ),
            "overviews": (
                "workbench_media_summary",
                "state IN ('queued','running','stale')",
            ),
            "analysis": (
                "workbench_analysis_run",
                "state='running'",
            ),
        }
        return {
            name: int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE matter_id=? AND {predicate}",
                    (matter_id,),
                ).fetchone()[0]
            )
            for name, (table, predicate) in queries.items()
        }

    def active_matter_work_counts(self, matter_id: str) -> dict[str, int]:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        with self._lock:
            return self._active_matter_work_counts_locked(matter_id)

    def matter_content_counts(self, matter_id: str) -> dict[str, int]:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        with self._lock:
            exists = self.connection.execute(
                "SELECT 1 FROM workbench_matter WHERE matter_id=?", (matter_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(matter_id)
            conversations = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_conversation WHERE matter_id=?",
                    (matter_id,),
                ).fetchone()[0]
            )
            messages = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_message msg "
                    "JOIN workbench_conversation c ON c.conversation_id=msg.conversation_id "
                    "WHERE c.matter_id=?",
                    (matter_id,),
                ).fetchone()[0]
            )
            notebook_items = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_notebook_item WHERE matter_id=?",
                    (matter_id,),
                ).fetchone()[0]
            )
        return {
            "conversations": conversations,
            "messages": messages,
            "notebook_items": notebook_items,
        }

    def recover_interrupted_matter_purges(self) -> int:
        """Make an interrupted synchronous purge explicitly retryable on startup."""

        now = self._now()
        with self._lock, self.connection:
            return self.connection.execute(
                "UPDATE workbench_matter_lifecycle SET state='purge_failed',"
                "error_code='unknown',updated_at=? WHERE state='purging'",
                (now,),
            ).rowcount

    def recover_running_analysis_runs(self) -> int:
        """Make abandoned synchronous review-map work retryable on startup."""

        now = self._now()
        with self._lock, self.connection:
            return self.connection.execute(
                "UPDATE workbench_analysis_run SET state='failed',message=?,finished_at=? "
                "WHERE state='running' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active')",
                (_ANALYSIS_RESTART_MESSAGE, now),
            ).rowcount

    def begin_matter_purge(
        self, slug: str, actor_id: str, confirmed_name: str, *, source_count: int,
        administrator_override: bool = False, token: str | None = None,
        session_id: str | None = None, export_id: str | None = None,
        export_acknowledged: bool = False, current_snapshot_digest: str | None = None,
        mode: str = 'export_exit', no_backup_acknowledged: bool = False,
    ) -> tuple[MatterRecord, MatterLifecycleRecord]:
        """Compatibility entry point; raw manual deletion cannot bypass exit gates."""
        return self.case_lifecycle.claim_purge(
            slug, actor_id, confirmed_name, source_count=source_count, token=token,
            session_id=session_id, export_id=export_id, export_acknowledged=export_acknowledged,
            mode=mode, no_backup_acknowledged=no_backup_acknowledged,
            current_snapshot_digest=current_snapshot_digest, administrator_override=administrator_override,
        )

    def begin_due_matter_purge(
        self, matter_id: str, *, source_count: int
    ) -> tuple[MatterRecord, MatterLifecycleRecord] | None:
        """Dates never claim deletion, including retained legacy schedules."""
        return None

    def fail_matter_purge(
        self, matter_id: str, purge_id: str, error_code: str
    ) -> MatterLifecycleRecord:
        if error_code not in {
            "projection",
            "storage",
            "control_store",
            "media_playback",
            "media_processing",
            "clip_exports",
            "authority",
            "unknown",
        }:
            raise ValueError("invalid purge failure code")
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_matter_lifecycle SET state='purge_failed',error_code=?,"
                "updated_at=? WHERE matter_id=? AND purge_id=? AND state='purging'",
                (error_code, now, matter_id, purge_id),
            ).rowcount
        if changed != 1:
            raise KeyError(purge_id)
        return self.matter_lifecycle(matter_id)

    def _authorize_automatic_discovery(self, matter_id: str, actor_id: str, *, create: bool = True) -> None:
        """Allow only the inactive system principal, and only on an active matter.

        create=False (reads) never writes: before first use there is no principal
        row, hence no grant to refuse, and the read may proceed.
        """

        if actor_id != AUTOMATIC_DISCOVERY_PRINCIPAL:
            raise KeyError(matter_id)
        row = self.connection.execute(
            "SELECT 1 FROM workbench_matter_lifecycle WHERE matter_id=? AND state='active'",
            (matter_id,),
        ).fetchone()
        if row is None:
            raise KeyError(matter_id)
        # Never trust the identifier alone: a pre-existing row must be exactly the
        # inactive internal principal and must hold no matter access.
        lookup = "SELECT provider,provider_subject,active FROM workbench_principal WHERE principal_id=?"
        principal = self.connection.execute(lookup, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone()
        if principal is None and not create:
            return
        if principal is None:
            # Created on first use only, so later authorizations (including the
            # read-only readiness poll) execute nothing but SELECTs. Inactive, so it
            # can never sign in or hold membership.
            self.connection.execute(
                "INSERT OR IGNORE INTO workbench_principal(principal_id,provider,provider_subject,"
                "display_name,login_name,active,created_at,last_seen_at) VALUES (?,?,?,?,?,0,?,?)",
                (AUTOMATIC_DISCOVERY_PRINCIPAL, "system", "automatic-discovery", "Automatic discovery",
                 "automatic-discovery", "1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"),
            )
            principal = self.connection.execute(lookup, (AUTOMATIC_DISCOVERY_PRINCIPAL,)).fetchone()
        # Check raw grants: the effective-membership view hides inactive
        # principals, which is exactly what this one is.
        member = self.connection.execute(
            "SELECT 1 FROM workbench_matter_membership WHERE principal_id=? "
            "UNION ALL SELECT 1 FROM workbench_team_group_member WHERE principal_id=? LIMIT 1",
            (AUTOMATIC_DISCOVERY_PRINCIPAL, AUTOMATIC_DISCOVERY_PRINCIPAL),
        ).fetchone()
        if principal is None or tuple(principal) != ("system", "automatic-discovery", 0) or member:
            raise KeyError(matter_id)

    def complete_matter_purge(
        self, matter_id: str, purge_id: str
    ) -> MatterLifecycleRecord:
        now = self._now()
        with self._lock, self.connection, self.case_lifecycle.purge_cleanup(matter_id, purge_id):
            self.connection.execute("BEGIN IMMEDIATE")
            self.case_lifecycle.validate_purge(matter_id, purge_id)
            lifecycle = self.connection.execute(
                "SELECT state FROM workbench_matter_lifecycle "
                "WHERE matter_id=? AND purge_id=?",
                (matter_id, purge_id),
            ).fetchone()
            if lifecycle is None or lifecycle["state"] != "purging":
                raise KeyError(purge_id)
            self.connection.execute(
                "DELETE FROM workbench_report_compilation_job WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_answer_job WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_research_job WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_review_run WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_review_criterion WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_media_clip WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_media_job WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_report WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_review_finding WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_analysis_run WHERE matter_id=?", (matter_id,)
            )
            for table in ('workbench_entity_discovery_seen', 'workbench_entity_discovery_unit',
                          'workbench_entity_auto_discovery_unit', 'workbench_entity_reconciliation'):
                self.connection.execute(f"DELETE FROM {table} WHERE matter_id=?", (matter_id,))
            self.connection.execute('DELETE FROM workbench_matter_purpose WHERE matter_id=?', (matter_id,))
            self.connection.execute('DELETE FROM workbench_context_selection WHERE matter_id=?', (matter_id,))
            self.connection.execute('DELETE FROM workbench_assertion WHERE matter_id=?', (matter_id,))
            self.connection.execute(
                "DELETE FROM workbench_entity WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_notebook_item WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_conversation WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_ingest_job WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_ingest_plan WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_intake_receipt WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_upload_session WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_source_set WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_source_organization WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_source_catalog WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_source_collection WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_job_matter_schedule WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_matter_activity WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_matter_group_grant WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "DELETE FROM workbench_matter_membership WHERE matter_id=?", (matter_id,)
            )
            self.connection.execute(
                "UPDATE workbench_matter SET display_name='Deleted matter',descriptor='',"
                "updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
            self.connection.execute(
                "DELETE FROM workbench_case_confirmation WHERE exit_id IN ("
                "SELECT exit_id FROM workbench_case_exit WHERE matter_id=?)", (matter_id,)
            )
            from .case_oidc import purge_records
            purge_records(self, matter_id)
            preserved_tables = {
                "workbench_audit_event",
                "workbench_matter",
                "workbench_matter_lifecycle",
                "workbench_matter_retention",
                "workbench_case_policy",
                "workbench_case_exit",
            }
            tables = self.connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'workbench_%'"
            ).fetchall()
            for table_row in tables:
                table = str(table_row[0])
                if not re.fullmatch(r"workbench_[a-z0-9_]+", table):
                    raise RuntimeError("workbench content table identity is invalid")
                columns = {
                    str(column[1])
                    for column in self.connection.execute(
                        f'PRAGMA table_info("{table}")'
                    )
                }
                if "matter_id" not in columns or table in preserved_tables:
                    continue
                remaining = int(
                    self.connection.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE matter_id=?',
                        (matter_id,),
                    ).fetchone()[0]
                )
                if remaining:
                    raise RuntimeError("matter purge left workbench content behind")
            changed = self.connection.execute(
                "UPDATE workbench_matter_lifecycle SET state='deleted',finished_at=?,"
                "error_code=NULL,updated_at=? WHERE matter_id=? AND purge_id=? "
                "AND state='purging'",
                (now, now, matter_id, purge_id),
            ).rowcount
            self.connection.execute(
                "UPDATE workbench_case_exit SET state='deleted',updated_at=? "
                "WHERE matter_id=? AND purge_id=? AND state='confirmed'", (now, matter_id, purge_id)
            )
            if changed != 1:
                raise RuntimeError("matter purge state changed unexpectedly")
        return self.matter_lifecycle(matter_id)

    def conversations(
        self, matter_id: str, *, include_archived: bool = True
    ) -> tuple[ConversationRecord, ...]:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        state_clause = "" if include_archived else " AND organization.state='active'"
        with self._lock:
            rows = self.connection.execute(
                "SELECT conversation.conversation_id,conversation.matter_id,"
                "conversation.title,conversation.created_at,conversation.updated_at "
                "FROM workbench_conversation conversation "
                "JOIN workbench_conversation_organization organization "
                "ON organization.conversation_id=conversation.conversation_id "
                "WHERE conversation.matter_id=?" + state_clause + " "
                "ORDER BY CASE organization.state WHEN 'active' THEN 0 ELSE 1 END,"
                "organization.is_pinned DESC,conversation.updated_at DESC,"
                "conversation.conversation_id DESC",
                (matter_id,),
            ).fetchall()
        return tuple(self._conversation(row) for row in rows)

    def conversation_summaries(
        self, matter_id: str
    ) -> tuple[ConversationSummaryRecord, ...]:
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT c.conversation_id,c.matter_id,c.title,c.created_at,c.updated_at,"
                "COUNT(m.message_id) AS message_count,o.state,o.is_pinned,o.archived_at "
                "FROM workbench_conversation c "
                "JOIN workbench_conversation_organization o "
                "ON o.conversation_id=c.conversation_id "
                "LEFT JOIN workbench_message m ON m.conversation_id=c.conversation_id "
                "WHERE c.matter_id=? GROUP BY c.conversation_id,c.matter_id,c.title,"
                "c.created_at,c.updated_at,o.state,o.is_pinned,o.archived_at "
                "ORDER BY CASE o.state WHEN 'active' THEN 0 ELSE 1 END,"
                "o.is_pinned DESC,CASE WHEN o.state='archived' THEN o.archived_at "
                "ELSE c.updated_at END DESC,c.conversation_id DESC",
                (matter_id,),
            ).fetchall()
        return tuple(
            ConversationSummaryRecord(
                row["conversation_id"],
                row["matter_id"],
                row["title"],
                row["created_at"],
                row["updated_at"],
                int(row["message_count"]),
                row["state"],
                int(row["is_pinned"]),
                row["archived_at"],
            )
            for row in rows
        )

    def conversation_summary(
        self, matter_id: str, conversation_id: str
    ) -> ConversationSummaryRecord:
        matched = next(
            (
                item
                for item in self.conversation_summaries(matter_id)
                if item.conversation_id == conversation_id
            ),
            None,
        )
        if matched is None:
            raise KeyError(conversation_id)
        return matched

    def conversation_content_counts(
        self, matter_id: str, conversation_id: str
    ) -> dict[str, int]:
        self.get_conversation_any(matter_id, conversation_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(DISTINCT m.message_id) AS messages,"
                "COUNT(DISTINCT j.job_id) AS answer_jobs,"
                "COUNT(DISTINCT CASE WHEN j.state IN ('queued','running') "
                "THEN j.job_id END) AS active_answers,"
                "COUNT(DISTINCT r.job_id) AS research_jobs,"
                "COUNT(DISTINCT CASE WHEN r.state='succeeded' "
                "THEN r.job_id END) AS succeeded_research,"
                "COUNT(DISTINCT CASE WHEN r.state IN ('queued','running') "
                "THEN r.job_id END) AS active_research "
                "FROM workbench_conversation c "
                "LEFT JOIN workbench_message m ON m.conversation_id=c.conversation_id "
                "LEFT JOIN workbench_answer_job j ON j.conversation_id=c.conversation_id "
                "LEFT JOIN workbench_research_job r ON r.conversation_id=c.conversation_id "
                "AND r.matter_id=c.matter_id "
                "WHERE c.conversation_id=? AND c.matter_id=?",
                (conversation_id, matter_id),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return {
            "messages": int(row["messages"]),
            "answer_jobs": int(row["answer_jobs"]),
            "active_answers": int(row["active_answers"]),
            "research_jobs": int(row["research_jobs"]),
            "succeeded_research": int(row["succeeded_research"]),
            "active_research": int(row["active_research"]),
        }

    def conversation_organization(
        self, conversation_id: str
    ) -> ConversationOrganizationRecord:
        if not _IDENTIFIER.fullmatch(conversation_id):
            raise KeyError(conversation_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT conversation_id,matter_id,state,is_pinned,archived_at,archived_by,"
                "updated_by,updated_at FROM workbench_conversation_organization "
                "WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return self._conversation_organization(row)

    def _create_conversation_locked(
        self,
        matter_id: str,
        title: str,
        *,
        now: str,
        actor_id: str | None,
    ) -> ConversationRecord:
        conversation_id = f"conversation-{uuid.uuid4().hex}"
        self.connection.execute(
            "INSERT INTO workbench_conversation("
            "conversation_id,matter_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (conversation_id, matter_id, title, now, now),
        )
        self.connection.execute(
            "INSERT INTO workbench_conversation_organization("
            "conversation_id,matter_id,state,is_pinned,archived_at,archived_by,"
            "updated_by,updated_at) VALUES (?,?,'active',0,NULL,NULL,?,?)",
            (conversation_id, matter_id, actor_id, now),
        )
        return ConversationRecord(conversation_id, matter_id, title, now, now)

    def create_conversation(
        self,
        matter_id: str,
        title: str = "New conversation",
        *,
        actor_id: str | None = None,
    ) -> ConversationRecord:
        value = self._safe_text(title, label="Conversation title", maximum=120)
        actor = None
        if actor_id is not None:
            actor = self.membership(matter_id, actor_id).principal_id
        now = self._now()
        with self._lock, self.connection:
            exists = self.connection.execute(
                "SELECT 1 FROM workbench_matter m JOIN workbench_matter_lifecycle ml "
                "ON ml.matter_id=m.matter_id WHERE m.matter_id=? AND ml.state='active'",
                (matter_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(matter_id)
            conversation = self._create_conversation_locked(
                matter_id,
                value,
                now=now,
                actor_id=actor,
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (now, matter_id)
            )
        return conversation

    def rename_conversation(
        self, matter_id: str, conversation_id: str, title: str
    ) -> ConversationRecord:
        value = self._safe_text(title, label="Conversation title", maximum=120)
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_conversation SET title=?,updated_at=? "
                "WHERE conversation_id=? AND matter_id=? AND EXISTS ("
                "SELECT 1 FROM workbench_conversation_organization o "
                "WHERE o.conversation_id=workbench_conversation.conversation_id "
                "AND o.state='active')",
                (value, now, conversation_id, matter_id),
            ).rowcount
            if changed != 1:
                raise KeyError(conversation_id)
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
            row = self.connection.execute(
                "SELECT conversation_id,matter_id,title,created_at,updated_at "
                "FROM workbench_conversation WHERE conversation_id=? AND matter_id=?",
                (conversation_id, matter_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("conversation rename did not persist")
        return self._conversation(row)

    @staticmethod
    def _automatic_conversation_title(question: str, maximum: int = 72) -> str:
        value = " ".join(question.split())
        if len(value) <= maximum:
            return value
        shortened = value[: maximum - 1].rstrip()
        if " " in shortened:
            candidate = shortened.rsplit(" ", 1)[0].rstrip(".,;:!?-")
            if len(candidate) >= maximum // 2:
                shortened = candidate
        return shortened + "…"

    def get_conversation(self, matter_id: str, conversation_id: str | None = None) -> ConversationRecord:
        conversations = self.conversations(matter_id, include_archived=False)
        if not conversations:
            return self.create_conversation(matter_id)
        if conversation_id is None:
            return conversations[0]
        matched = next(
            (item for item in conversations if item.conversation_id == conversation_id), None
        )
        if matched is None:
            raise KeyError(conversation_id)
        return matched

    def get_conversation_any(
        self, matter_id: str, conversation_id: str
    ) -> ConversationRecord:
        matched = next(
            (
                item
                for item in self.conversations(matter_id)
                if item.conversation_id == conversation_id
            ),
            None,
        )
        if matched is None:
            raise KeyError(conversation_id)
        return matched

    def set_conversation_pinned(
        self,
        matter_id: str,
        conversation_id: str,
        actor_id: str,
        pinned: bool,
    ) -> ConversationSummaryRecord:
        actor = self.membership(matter_id, actor_id).principal_id
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_conversation_organization SET is_pinned=?,"
                "updated_by=?,updated_at=? WHERE conversation_id=? AND matter_id=? "
                "AND state='active'",
                (1 if pinned else 0, actor, now, conversation_id, matter_id),
            ).rowcount
            if changed != 1:
                raise KeyError(conversation_id)
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
        return self.conversation_summary(matter_id, conversation_id)

    def _conversation_has_active_answer_locked(self, conversation_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM workbench_answer_job WHERE conversation_id=? "
                "AND state IN ('queued','running') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            is not None
        )

    def _conversation_has_active_research_locked(self, conversation_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM workbench_research_job WHERE conversation_id=? "
                "AND state IN ('queued','running') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            is not None
        )

    def _ensure_active_conversation_locked(
        self, matter_id: str, *, actor_id: str, now: str
    ) -> tuple[ConversationRecord, bool]:
        row = self.connection.execute(
            "SELECT c.conversation_id,c.matter_id,c.title,c.created_at,c.updated_at "
            "FROM workbench_conversation c "
            "JOIN workbench_conversation_organization o "
            "ON o.conversation_id=c.conversation_id "
            "WHERE c.matter_id=? AND o.state='active' "
            "ORDER BY o.is_pinned DESC,c.updated_at DESC,c.conversation_id DESC LIMIT 1",
            (matter_id,),
        ).fetchone()
        if row is not None:
            return self._conversation(row), False
        return (
            self._create_conversation_locked(
                matter_id,
                "New conversation",
                now=now,
                actor_id=actor_id,
            ),
            True,
        )

    def archive_conversation(
        self, matter_id: str, conversation_id: str, actor_id: str
    ) -> ConversationRecord:
        actor = self.membership(matter_id, actor_id).principal_id
        now = self._now()
        with self._lock, self.connection:
            # Serialize the final active-work checks with admissions made by
            # other workspace connections before changing organization state.
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT state FROM workbench_conversation_organization "
                "WHERE conversation_id=? AND matter_id=?",
                (conversation_id, matter_id),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if self._conversation_has_active_answer_locked(conversation_id):
                raise WorkspaceProblem(
                    "Wait for the answer in progress to finish or cancel it before archiving this conversation."
                )
            if self._conversation_has_active_research_locked(conversation_id):
                raise WorkspaceProblem(
                    "Wait for the investigation in progress to finish or cancel it before archiving this conversation."
                )
            if row["state"] == "active":
                self.connection.execute(
                    "UPDATE workbench_conversation_organization SET state='archived',"
                    "is_pinned=0,archived_at=?,archived_by=?,updated_by=?,updated_at=? "
                    "WHERE conversation_id=? AND matter_id=? AND state='active'",
                    (now, actor, actor, now, conversation_id, matter_id),
                )
            next_conversation, _ = self._ensure_active_conversation_locked(
                matter_id, actor_id=actor, now=now
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
        return next_conversation

    def restore_conversation(
        self, matter_id: str, conversation_id: str, actor_id: str
    ) -> ConversationRecord:
        actor = self.membership(matter_id, actor_id).principal_id
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT state FROM workbench_conversation_organization "
                "WHERE conversation_id=? AND matter_id=?",
                (conversation_id, matter_id),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if row["state"] == "archived":
                self.connection.execute(
                    "UPDATE workbench_conversation_organization SET state='active',"
                    "is_pinned=0,archived_at=NULL,archived_by=NULL,updated_by=?,updated_at=? "
                    "WHERE conversation_id=? AND matter_id=? AND state='archived'",
                    (actor, now, conversation_id, matter_id),
                )
                self.connection.execute(
                    "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                    (now, matter_id),
                )
            conversation = self.connection.execute(
                "SELECT conversation_id,matter_id,title,created_at,updated_at "
                "FROM workbench_conversation WHERE conversation_id=? AND matter_id=?",
                (conversation_id, matter_id),
            ).fetchone()
        if conversation is None:
            raise RuntimeError("conversation restore did not persist")
        return self._conversation(conversation)

    def delete_conversation(
        self,
        matter_id: str,
        conversation_id: str,
        actor_id: str,
        *,
        confirmed_title: str,
        acknowledged: bool,
    ) -> ConversationDeletionRecord:
        membership = self.membership(matter_id, actor_id)
        if membership.role != "owner":
            raise WorkspaceProblem("Only a matter owner can permanently delete conversations.")
        if not acknowledged:
            raise WorkspaceProblem(
                "Confirm that you understand this deletion cannot be undone."
            )
        confirmation = self._safe_text(
            confirmed_title,
            label="Conversation title confirmation",
            maximum=120,
        )
        now = self._now()
        with self._lock, self.connection:
            # Keep the final active-work check and deletion in the same write
            # transaction across process-local workspace connections.
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT c.title,COUNT(DISTINCT m.message_id) AS message_count,"
                "COUNT(DISTINCT j.job_id) AS answer_job_count,"
                "COUNT(DISTINCT r.job_id) AS research_job_count "
                "FROM workbench_conversation c "
                "JOIN workbench_conversation_organization o "
                "ON o.conversation_id=c.conversation_id "
                "LEFT JOIN workbench_message m ON m.conversation_id=c.conversation_id "
                "LEFT JOIN workbench_answer_job j ON j.conversation_id=c.conversation_id "
                "LEFT JOIN workbench_research_job r ON r.conversation_id=c.conversation_id "
                "AND r.matter_id=c.matter_id "
                "WHERE c.conversation_id=? AND c.matter_id=? GROUP BY c.conversation_id,c.title",
                (conversation_id, matter_id),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if confirmation != row["title"]:
                raise WorkspaceProblem("Type the conversation title exactly to confirm deletion.")
            if self._conversation_has_active_answer_locked(conversation_id):
                raise WorkspaceProblem(
                    "Wait for the answer in progress to finish or cancel it before deleting this conversation."
                )
            if self._conversation_has_active_research_locked(conversation_id):
                raise WorkspaceProblem(
                    "Wait for the investigation in progress to finish or cancel it before deleting this conversation."
                )
            message_count = int(row["message_count"])
            answer_job_count = int(row["answer_job_count"])
            research_job_count = int(row["research_job_count"])
            # Research jobs are bound to a conversation by the compatibility
            # migration rather than a database foreign key, so remove their
            # durable events and result payloads explicitly with the confirmed
            # conversation deletion.
            self.connection.execute(
                "DELETE FROM workbench_research_job WHERE matter_id=? AND conversation_id=?",
                (matter_id, conversation_id),
            )
            changed = self.connection.execute(
                "DELETE FROM workbench_conversation WHERE conversation_id=? AND matter_id=?",
                (conversation_id, matter_id),
            ).rowcount
            if changed != 1:
                raise KeyError(conversation_id)
            self.connection.execute(
                "DELETE FROM workbench_matter_activity WHERE matter_id=? "
                "AND activity_kind='conversation' AND object_id=?",
                (matter_id, conversation_id),
            )
            next_conversation, created = self._ensure_active_conversation_locked(
                matter_id,
                actor_id=membership.principal_id,
                now=now,
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
        return ConversationDeletionRecord(
            conversation_id,
            message_count,
            answer_job_count,
            research_job_count,
            next_conversation.conversation_id,
            created,
        )

    def append_message(
        self,
        matter_id: str,
        conversation_id: str,
        role: str,
        content: str,
        payload: Mapping[str, object] | None = None,
    ) -> MessageRecord:
        if role not in {"user", "assistant"}:
            raise ValueError("invalid message role")
        validator = self._derived_text if role == "assistant" else self._safe_text
        value = validator(
            content, label="Message", maximum=20_000, multiline=True
        )
        encoded = json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 100_000:
            raise WorkspaceProblem(
                "The message and its source details exceed the save limit. "
                "Ask a narrower question or select fewer sources; existing work is retained."
            )
        now = self._now()
        message_id = f"message-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            bound = self.connection.execute(
                "SELECT 1 FROM workbench_conversation c "
                "JOIN workbench_conversation_organization o "
                "ON o.conversation_id=c.conversation_id "
                "WHERE c.conversation_id=? AND c.matter_id=? AND o.state='active'",
                (conversation_id, matter_id),
            ).fetchone()
            if bound is None:
                raise KeyError(conversation_id)
            ordinal = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_message WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0]
            )
            self.connection.execute(
                "INSERT INTO workbench_message(message_id,conversation_id,ordinal,role,content,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (message_id, conversation_id, ordinal, role, value, encoded, now),
            )
            self.connection.execute(
                "UPDATE workbench_conversation SET updated_at=? WHERE conversation_id=? AND matter_id=?",
                (now, conversation_id, matter_id),
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (now, matter_id)
            )
        return MessageRecord(message_id, conversation_id, ordinal, role, value, dict(payload or {}), now)

    def messages(self, matter_id: str, conversation_id: str) -> tuple[MessageRecord, ...]:
        self.get_conversation_any(matter_id, conversation_id)
        with self._lock:
            context_bytes = self.connection.execute(
                'SELECT coalesce(sum(length(CAST(c.snapshot_json AS BLOB)) + '
                '(SELECT coalesce(sum(length(CAST(a.manifest_json AS BLOB))),0) FROM workbench_answer_context_attempt a WHERE a.job_id=c.job_id)),0) '
                'FROM workbench_answer_context c JOIN workbench_answer_job j ON j.job_id=c.job_id '
                'WHERE j.matter_id=? AND j.conversation_id=?', (matter_id, conversation_id)).fetchone()[0]
            if context_bytes > 8 * 1024 * 1024:
                raise WorkspaceProblem('Conversation context exceeds the complete read/export bound. Inspect individual answer receipts.')
            rows = self.connection.execute(
                "SELECT message_id,conversation_id,ordinal,role,content,payload_json,created_at "
                "FROM workbench_message WHERE conversation_id=? ORDER BY ordinal",
                (conversation_id,),
            ).fetchall()
        result: list[MessageRecord] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise RuntimeError("stored conversation payload is invalid") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("stored conversation payload is invalid")
            if payload.get('recorded_context_job'):
                from .answer_context import AnswerContextRepository
                with self._lock:
                    payload['context_supplied'] = AnswerContextRepository(self).receipt(matter_id, payload['recorded_context_job'])
                if payload['context_supplied'] is None:
                    raise WorkspaceProblem('The saved answer context record is unavailable.')
            result.append(
                MessageRecord(
                    row["message_id"],
                    row["conversation_id"],
                    int(row["ordinal"]),
                    row["role"],
                    row["content"],
                    payload,
                    row["created_at"],
                )
            )
        return tuple(result)

    def create_ingest_plan(self, matter_id: str, preflight: SourcePreflight) -> IngestPlanRecord:
        """Persist the exact metadata-only set staff will confirm."""

        now = self._now()
        plan_id = f"ingest-plan-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            if self.connection.execute(
                "SELECT 1 FROM workbench_matter m JOIN workbench_matter_lifecycle ml "
                "ON ml.matter_id=m.matter_id WHERE m.matter_id=? AND ml.state='active'",
                (matter_id,),
            ).fetchone() is None:
                raise KeyError(matter_id)
            self.connection.execute(
                "INSERT INTO workbench_ingest_plan("
                "plan_id,matter_id,source_location_id,source_label,relative_folder,state,"
                "supported_count,unsupported_count,total_bytes,created_at,confirmed_at) "
                "VALUES (?,?,?,?,?,'review',?,?,?,?,NULL)",
                (
                    plan_id,
                    matter_id,
                    preflight.source_location_id,
                    preflight.label,
                    preflight.relative_folder,
                    len(preflight.supported),
                    preflight.unsupported_count,
                    preflight.total_bytes,
                    now,
                ),
            )
            self.connection.executemany(
                "INSERT INTO workbench_ingest_plan_item("
                "plan_id,ordinal,relative_path,display_name,media_type,byte_size,"
                "stable_device,stable_inode,stable_mtime_ns,document_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                [
                    (
                        plan_id,
                        ordinal,
                        item.relative_path,
                        item.display_name,
                        item.media_type,
                        item.byte_size,
                        item.stable_device,
                        item.stable_inode,
                        item.stable_mtime_ns,
                    )
                    for ordinal, item in enumerate(preflight.supported, 1)
                ],
            )
        return self.get_ingest_plan(matter_id, plan_id)

    def get_ingest_plan(self, matter_id: str, plan_id: str) -> IngestPlanRecord:
        if not re.fullmatch(r"ingest-plan-[0-9a-f]{32}", plan_id):
            raise KeyError(plan_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT plan_id,matter_id,source_location_id,source_label,relative_folder,state,"
                "supported_count,unsupported_count,total_bytes,created_at,confirmed_at "
                "FROM workbench_ingest_plan WHERE plan_id=? AND matter_id=?",
                (plan_id, matter_id),
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return self._plan(row)

    def ingest_plan_items(self, matter_id: str, plan_id: str) -> tuple[IngestPlanItemRecord, ...]:
        self.get_ingest_plan(matter_id, plan_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT plan_id,ordinal,relative_path,display_name,media_type,byte_size,"
                "stable_device,stable_inode,stable_mtime_ns,document_id "
                "FROM workbench_ingest_plan_item WHERE plan_id=? ORDER BY ordinal",
                (plan_id,),
            ).fetchall()
        return tuple(self._plan_item(row) for row in rows)

    def cancel_ingest_plan(self, matter_id: str, plan_id: str) -> None:
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_ingest_plan SET state='cancelled' "
                "WHERE plan_id=? AND matter_id=? AND state='review'",
                (plan_id, matter_id),
            ).rowcount
        if changed != 1:
            raise KeyError(plan_id)

    def confirm_ingest_plan(
        self,
        matter_id: str,
        plan_id: str,
        document_ids: Mapping[int, str],
        actor_id: str | None = None,
    ) -> tuple[IngestJobRecord, ...]:
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            plan = self.connection.execute(
                "SELECT ip.source_location_id,ip.source_label,ip.relative_folder,"
                "ip.state,ip.supported_count,m.owner_id "
                "FROM workbench_ingest_plan ip JOIN workbench_matter_lifecycle ml "
                "ON ml.matter_id=ip.matter_id JOIN workbench_matter m "
                "ON m.matter_id=ip.matter_id WHERE ip.plan_id=? AND ip.matter_id=? "
                "AND ml.state='active'",
                (plan_id, matter_id),
            ).fetchone()
            if plan is None or plan["state"] != "review":
                raise KeyError(plan_id)
            actor = actor_id or plan["owner_id"]
            self.membership(matter_id, actor)
            items = self.connection.execute(
                "SELECT ordinal,relative_path FROM workbench_ingest_plan_item "
                "WHERE plan_id=? ORDER BY ordinal",
                (plan_id,),
            ).fetchall()
            expected = {int(item["ordinal"]) for item in items}
            if set(document_ids) != expected or len(expected) != int(plan["supported_count"]):
                raise ValueError("document mapping does not match the reviewed plan")
            collection_label = plan["source_label"]
            if plan["relative_folder"]:
                folder_label = str(plan["relative_folder"]).rstrip("/").rsplit("/", 1)[-1]
                collection_label = f"{collection_label} · {folder_label}"
            collection_label = collection_label[:160]
            collection = self._create_source_collection_locked(
                matter_id,
                collection_label,
                "registered",
                actor,
                now,
            )
            jobs: list[str] = []
            for item in items:
                ordinal = int(item["ordinal"])
                document_id = document_ids[ordinal]
                if not re.fullmatch(r"[0-9a-f]{32}", document_id):
                    raise ValueError("invalid document identifier")
                job_id = f"ingest-job-{uuid.uuid4().hex}"
                jobs.append(job_id)
                self.connection.execute(
                    "UPDATE workbench_ingest_plan_item SET document_id=? "
                    "WHERE plan_id=? AND ordinal=?",
                    (document_id, plan_id, ordinal),
                )
                self.connection.execute(
                    "INSERT INTO workbench_ingest_job("
                    "job_id,matter_id,document_id,plan_id,source_location_id,relative_path,"
                    "state,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,'queued','Queued',?,?)",
                    (
                        job_id,
                        matter_id,
                        document_id,
                        plan_id,
                        plan["source_location_id"],
                        item["relative_path"],
                        now,
                        now,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO workbench_source_organization("
                    "matter_id,document_id,collection_id,relative_path,review_state,"
                    "added_by,added_at,updated_by,updated_at) "
                    "VALUES (?,?,?,?,'unreviewed',?,?,?,?)",
                    (
                        matter_id,
                        document_id,
                        collection.collection_id,
                        item["relative_path"],
                        actor,
                        now,
                        actor,
                        now,
                    ),
                )
            self.connection.execute(
                "UPDATE workbench_ingest_plan SET state='confirmed',confirmed_at=? WHERE plan_id=?",
                (now, plan_id),
            )
            rows = self.connection.execute(
                "SELECT * FROM workbench_ingest_job WHERE job_id IN ("
                + ",".join("?" for _ in jobs)
                + ") ORDER BY created_at,job_id",
                jobs,
            ).fetchall() if jobs else []
        return tuple(self._job(row) for row in rows)

    def queue_upload(self, matter_id: str, document_id: str) -> IngestJobRecord:
        if not re.fullmatch(r"[0-9a-f]{32}", document_id):
            raise ValueError("invalid document identifier")
        job_id = f"ingest-job-{uuid.uuid4().hex}"
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            active = self.connection.execute(
                "SELECT 1 FROM workbench_matter_lifecycle "
                "WHERE matter_id=? AND state='active'",
                (matter_id,),
            ).fetchone()
            if active is None:
                raise KeyError(matter_id)
            self.connection.execute(
                "INSERT INTO workbench_ingest_job("
                "job_id,matter_id,document_id,state,stage,created_at,updated_at) "
                "VALUES (?,?,?,'queued','Queued',?,?)",
                (job_id, matter_id, document_id, now, now),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_ingest_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._job(row)

    def recover_running_ingest_jobs(self) -> int:
        """Return interrupted work to the durable queue after process startup."""

        now = self._now()
        with self._lock, self.connection:
            return self.connection.execute(
                "UPDATE workbench_ingest_job SET state='queued',stage='Queued after restart',"
                "worker_id=NULL,started_at=NULL,updated_at=? WHERE state='running' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active')",
                (now,),
            ).rowcount

    def _advance_matter_schedule_locked(
        self, queue_kind: str, matter_id: str
    ) -> None:
        if queue_kind not in {"ingest", "media", "media_summary"}:
            raise ValueError("invalid matter queue")
        next_sequence = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(claim_sequence),0)+1 "
                "FROM workbench_job_matter_schedule WHERE queue_kind=?",
                (queue_kind,),
            ).fetchone()[0]
        )
        self.connection.execute(
            "INSERT INTO workbench_job_matter_schedule("
            "queue_kind,matter_id,claim_sequence) VALUES (?,?,?) "
            "ON CONFLICT(queue_kind,matter_id) DO UPDATE SET "
            "claim_sequence=excluded.claim_sequence",
            (queue_kind, matter_id, next_sequence),
        )

    def claim_ingest_job(self, worker_id: str) -> IngestJobRecord | None:
        worker = self._safe_text(worker_id, label="Worker identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT j.job_id,j.matter_id FROM workbench_ingest_job j "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=j.matter_id "
                "LEFT JOIN workbench_job_matter_schedule schedule "
                "ON schedule.queue_kind='ingest' AND schedule.matter_id=j.matter_id "
                "WHERE j.state='queued' AND ml.state='active' "
                "ORDER BY COALESCE(schedule.claim_sequence,0),j.created_at,j.job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = self.connection.execute(
                "UPDATE workbench_ingest_job SET state='running',stage='Starting',"
                "attempts=attempts+1,worker_id=?,started_at=?,finished_at=NULL,updated_at=? "
                "WHERE job_id=? AND state='queued'",
                (worker, now, now, row["job_id"]),
            ).rowcount
            if changed != 1:
                return None
            self._advance_matter_schedule_locked("ingest", row["matter_id"])
            claimed = self.connection.execute(
                "SELECT * FROM workbench_ingest_job WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job(claimed)

    def update_ingest_job(
        self,
        job_id: str,
        *,
        stage: str,
        completed_units: int = 0,
        total_units: int = 0,
        message: str = "",
    ) -> None:
        stage_value = self._safe_text(stage, label="Processing stage", maximum=80)
        message_value = self._safe_text(
            message, label="Processing message", maximum=240, required=False
        )
        if completed_units < 0 or total_units < 0 or (total_units and completed_units > total_units):
            raise ValueError("invalid processing progress")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE workbench_ingest_job SET stage=?,completed_units=?,total_units=?,"
                "message=?,updated_at=? WHERE job_id=? AND state='running'",
                (stage_value, completed_units, total_units, message_value, now, job_id),
            )

    def finish_ingest_job(self, job_id: str, *, succeeded: bool, message: str = "") -> None:
        value = self._safe_text(
            message, label="Processing message", maximum=240, required=False
        )
        state = "succeeded" if succeeded else "failed"
        stage = "Complete" if succeeded else "Needs attention"
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_ingest_job SET state=?,stage=?,message=?,worker_id=NULL,"
                "finished_at=?,updated_at=? WHERE job_id=? AND state='running'",
                (state, stage, value, now, now, job_id),
            ).rowcount
        if changed != 1:
            raise KeyError(job_id)

    def retry_ingest_job(self, matter_id: str, document_id: str) -> IngestJobRecord:
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_ingest_job SET state='queued',stage='Queued',message='',"
                "worker_id=NULL,started_at=NULL,finished_at=NULL,updated_at=? "
                "WHERE matter_id=? AND document_id=? AND state='failed' AND EXISTS ("
                "SELECT 1 FROM workbench_matter_lifecycle ml "
                "WHERE ml.matter_id=workbench_ingest_job.matter_id AND ml.state='active')",
                (now, matter_id, document_id),
            ).rowcount
            if changed != 1:
                raise KeyError(document_id)
            row = self.connection.execute(
                "SELECT * FROM workbench_ingest_job WHERE matter_id=? AND document_id=?",
                (matter_id, document_id),
            ).fetchone()
        return self._job(row)

    def cancel_document_ingest_job(self, matter_id: str, document_id: str) -> None:
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE workbench_ingest_job SET state='cancelled',stage='Cancelled',"
                "worker_id=NULL,finished_at=?,updated_at=? "
                "WHERE matter_id=? AND document_id=? AND state IN ('queued','failed')",
                (now, now, matter_id, document_id),
            )

    def ingest_job(self, matter_id: str, document_id: str) -> IngestJobRecord | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_ingest_job WHERE matter_id=? AND document_id=?",
                (matter_id, document_id),
            ).fetchone()
        return self._job(row) if row is not None else None

    def ingest_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM workbench_ingest_job GROUP BY state"
            ).fetchall()
        counts = {state: 0 for state in ("queued", "running", "succeeded", "failed", "cancelled")}
        counts.update({row["state"]: int(row["count"]) for row in rows})
        return counts

    @staticmethod
    def _media_metadata_json(value: object, *, kind: str) -> str:
        expected = list if kind == "warnings" else dict
        if not isinstance(value, expected):
            raise ValueError(f"invalid media {kind}")
        try:
            encoded = json.dumps(
                value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid media {kind}") from exc
        if len(encoded.encode("utf-8")) > 100_000:
            raise ValueError(f"media {kind} is too large")
        return encoded

    def queue_media_job(
        self,
        matter_id: str,
        document_id: str,
        source_version_id: str,
        actor_id: str,
        *,
        source_sha256: str,
        byte_size: int,
        media_type: str,
        duration_ms: int,
        maximum_byte_size: int = 5 * 1024 * 1024 * 1024,
    ) -> MediaJobRecord:
        document = self._source_document_id(document_id)
        version = self._safe_text(
            source_version_id, label="Source version", maximum=100
        )
        if not re.fullmatch(r"[0-9a-f]{32}", version):
            raise ValueError("invalid source version")
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256 or ""):
            raise ValueError("invalid source digest")
        if not 0 < byte_size <= int(maximum_byte_size):
            raise ValueError("invalid media size")
        if not 0 < duration_ms <= 12 * 60 * 60 * 1000:
            raise ValueError("invalid media duration")
        media = self._safe_text(media_type, label="Media type", maximum=100)
        self.membership(matter_id, actor_id)
        now = self._now()
        media_job_id = f"media-job-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor_id)
            self.connection.execute(
                "INSERT INTO workbench_media_job("
                "media_job_id,matter_id,document_id,source_version_id,requested_by,"
                "source_sha256,byte_size,media_type,duration_ms,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    media_job_id,
                    matter_id,
                    document,
                    version,
                    actor_id,
                    source_sha256,
                    byte_size,
                    media,
                    duration_ms,
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?",
                (media_job_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("media job did not persist")
        return self._media_job(row)

    def recover_running_media_jobs(self) -> int:
        """Return interrupted connector work to one durable global queue."""

        now = self._now()
        with self._lock, self.connection:
            return self.connection.execute(
                "UPDATE workbench_media_job SET state='queued',"
                "stage='Resuming after restart',worker_id=NULL,updated_at=? "
                "WHERE state='running' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active')",
                (now,),
            ).rowcount

    def claim_media_job(self, worker_id: str) -> MediaJobRecord | None:
        worker = self._safe_text(worker_id, label="Worker identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT job.media_job_id,job.matter_id FROM workbench_media_job job "
                "JOIN workbench_matter_lifecycle lifecycle "
                "ON lifecycle.matter_id=job.matter_id "
                "LEFT JOIN workbench_job_matter_schedule schedule "
                "ON schedule.queue_kind='media' AND schedule.matter_id=job.matter_id "
                "WHERE job.state='queued' AND lifecycle.state='active' "
                "ORDER BY COALESCE(schedule.claim_sequence,0),"
                "job.created_at,job.media_job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = self.connection.execute(
                "UPDATE workbench_media_job SET state='running',stage='Starting',"
                "progress=0,attempts=attempts+1,worker_id=?,started_at=COALESCE(started_at,?),"
                "finished_at=NULL,message='',updated_at=? "
                "WHERE media_job_id=? AND state='queued'",
                (worker, now, now, row["media_job_id"]),
            ).rowcount
            if changed != 1:
                return None
            self._advance_matter_schedule_locked("media", row["matter_id"])
            claimed = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?",
                (row["media_job_id"],),
            ).fetchone()
        return self._media_job(claimed) if claimed is not None else None

    def update_media_job(
        self,
        media_job_id: str,
        *,
        stage: str,
        progress: float,
        external_job_id: str | None = None,
        message: str = "",
    ) -> MediaJobRecord:
        if not _MEDIA_JOB.fullmatch(media_job_id or ""):
            raise ValueError("invalid media job")
        stage_value = self._safe_text(stage, label="Media stage", maximum=100)
        message_value = self._safe_text(
            message, label="Media message", maximum=240, required=False
        )
        if not isinstance(progress, (int, float)) or isinstance(progress, bool):
            raise ValueError("invalid media progress")
        progress_value = min(max(float(progress), 0.0), 1.0)
        external = None
        if external_job_id is not None:
            external = self._safe_text(
                external_job_id, label="Processor job", maximum=100
            )
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", external):
                raise ValueError("invalid processor job")
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_media_job SET stage=?,progress=?,"
                "external_job_id=COALESCE(?,external_job_id),message=?,updated_at=? "
                "WHERE media_job_id=? AND state='running'",
                (
                    stage_value,
                    progress_value,
                    external,
                    message_value,
                    now,
                    media_job_id,
                ),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?",
                (media_job_id,),
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(media_job_id)
        return self._media_job(row)

    def save_media_preflight(
        self, media_job_id: str, result: Mapping[str, object], *, hold: bool, message: str
    ) -> MediaJobRecord:
        """Persist inspection before any submission; held ASR occupies no worker.

        The existing cancelled queue state means no transcription is scheduled.
        The bound preflight distinguishes playback-only/review from cancellation
        in staff views. No new schema or false transcript-success row is needed.
        """
        metadata = dict(result)
        metadata["inspection_id"] = uuid.uuid4().hex
        metadata["continued"] = False
        encoded = self._media_metadata_json({"preflight": metadata}, kind="quality")
        message = self._safe_text(message, label="Media message", maximum=240)
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_media_job SET quality_json=?,state=?,stage=?,message=?,"
                "worker_id=CASE WHEN ? THEN NULL ELSE worker_id END,updated_at=? "
                "WHERE media_job_id=? AND state='running' AND external_job_id IS NULL",
                (encoded, "cancelled" if hold else "running", "Recording checked",
                 message, int(hold), now, media_job_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?", (media_job_id,)
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(media_job_id)
        return self._media_job(row)

    def validate_media_preflight_decision(
        self, matter_id: str, document_id: str, source_version_id: str,
        actor_id: str, inspection_id: str, *, retry: bool
    ) -> MediaJobRecord:
        """Reject stale or inapplicable decisions before reading recording bytes."""
        with self._lock:
            self.membership(matter_id, actor_id)
            job = self.media_job(matter_id, document_id, source_version_id)
            if (job is None or job.state != "cancelled" or not inspection_id
                    or job.preflight.get("inspection_id") != inspection_id):
                raise KeyError(document_id)
            if not retry and job.preflight.get("outcome") in {"no_audio", "failed"}:
                raise ValueError("Check the recording again before transcription.")
            return job

    def decide_media_preflight(
        self, matter_id: str, document_id: str, source_version_id: str,
        actor_id: str, inspection_id: str, *, retry: bool, request_id: str,
        session_id: str | None = None
    ) -> MediaJobRecord:
        """Bind a version-bound review decision to its currently authorized actor."""
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            job = self.validate_media_preflight_decision(
                matter_id, document_id, source_version_id, actor_id, inspection_id, retry=retry
            )
            metadata = dict(job.preflight)
            metadata["continued"] = not retry
            encoded = self._media_metadata_json(
                {"preflight": metadata}, kind="quality"
            )
            changed = self.connection.execute(
                "UPDATE workbench_media_job SET state='queued',stage=?,message='',"
                "progress=0,quality_json=?,requested_by=?,started_at=NULL,finished_at=NULL,updated_at=? "
                "WHERE media_job_id=? AND state='cancelled'",
                ("Checking recording" if retry else "Queued for transcription",
                 encoded, actor_id, now, job.media_job_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?", (job.media_job_id,)
            ).fetchone()
            if changed != 1 or row is None:
                raise KeyError(document_id)
            self._append_audit_event_locked(
                actor_principal_id=actor_id, session_id=session_id,
                matter_id=matter_id, request_id=request_id,
                action="media.preflight_retry" if retry else "media.preflight_continue",
                outcome="success", details={"state": "queued"},
            )
        return self._media_job(row)

    def finish_media_job(
        self,
        media_job_id: str,
        *,
        degraded: bool,
        message: str,
        warnings: Sequence[object] = (),
        quality: Mapping[str, object] | None = None,
        provenance: Mapping[str, object] | None = None,
    ) -> MediaJobRecord:
        state = "degraded" if degraded else "succeeded"
        warning_json = self._media_metadata_json(list(warnings), kind="warnings")
        quality_values = dict(quality or {})
        quality_values.pop("preflight", None)
        quality_json = self._media_metadata_json(quality_values, kind="quality")
        provenance_json = self._media_metadata_json(
            dict(provenance or {}), kind="provenance"
        )
        message_value = self._safe_text(
            message, label="Media message", maximum=240, required=False
        )
        now = self._now()
        with self._lock, self.connection:
            existing = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?", (media_job_id,)
            ).fetchone()
            if existing is not None:
                preflight = self._media_job(existing).preflight
                if preflight:
                    quality_json = self._media_metadata_json(
                        {**quality_values, "preflight": dict(preflight)}, kind="quality"
                    )
            changed = self.connection.execute(
                "UPDATE workbench_media_job SET state=?,stage='Transcript ready',progress=1,"
                "worker_id=NULL,degraded=?,message=?,warnings_json=?,quality_json=?,"
                "provenance_json=?,finished_at=?,updated_at=? "
                "WHERE media_job_id=? AND state='running'",
                (
                    state,
                    int(degraded),
                    message_value,
                    warning_json,
                    quality_json,
                    provenance_json,
                    now,
                    now,
                    media_job_id,
                ),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?",
                (media_job_id,),
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(media_job_id)
        return self._media_job(row)

    def fail_media_job(self, media_job_id: str, message: str) -> MediaJobRecord:
        value = self._safe_text(
            message or "Transcription did not finish. Choose Try again.",
            label="Media message",
            maximum=240,
        )
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_media_job SET state='failed',stage='Needs attention',"
                "worker_id=NULL,message=?,finished_at=?,updated_at=? "
                "WHERE media_job_id=? AND state='running'",
                (value, now, now, media_job_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?",
                (media_job_id,),
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(media_job_id)
        return self._media_job(row)

    def retry_media_job(
        self, matter_id: str, document_id: str, actor_id: str
    ) -> MediaJobRecord:
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor_id)
            changed = self.connection.execute(
                "UPDATE workbench_media_job SET state='queued',stage='Queued',progress=0,"
                "external_job_id=NULL,worker_id=NULL,degraded=0,message='',warnings_json='[]',"
                "quality_json='{}',provenance_json='{}',requested_by=?,started_at=NULL,finished_at=NULL,updated_at=? "
                "WHERE matter_id=? AND document_id=? AND state='failed'",
                (actor_id, now, matter_id, document_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE matter_id=? AND document_id=?",
                (matter_id, document_id),
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(document_id)
        return self._media_job(row)

    def media_job(
        self, matter_id: str, document_id: str, source_version_id: str | None = None
    ) -> MediaJobRecord | None:
        query = "SELECT * FROM workbench_media_job WHERE matter_id=? AND document_id=?"
        parameters: tuple[object, ...] = (matter_id, document_id)
        if source_version_id is not None:
            query += " AND source_version_id=?"
            parameters += (source_version_id,)
        with self._lock:
            row = self.connection.execute(query, parameters).fetchone()
        return self._media_job(row) if row is not None else None

    def media_job_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM workbench_media_job GROUP BY state"
            ).fetchall()
        counts = {
            state: 0
            for state in ("queued", "running", "succeeded", "degraded", "failed", "cancelled")
        }
        counts.update({str(row["state"]): int(row["count"]) for row in rows})
        return counts

    def media_jobs_for_matter(self, matter_id: str) -> tuple[MediaJobRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE matter_id=? "
                "ORDER BY created_at,media_job_id",
                (matter_id,),
            ).fetchall()
        return tuple(self._media_job(row) for row in rows)

    def media_transcript(
        self, matter_id: str, document_id: str, source_version_id: str
    ) -> MediaTranscriptRecord | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_media_transcript "
                "WHERE matter_id=? AND document_id=? AND source_version_id=?",
                (matter_id, document_id, source_version_id),
            ).fetchone()
        return self._media_transcript(row) if row is not None else None

    def import_media_transcript(
        self,
        media_job_id: str,
        *,
        segments: Sequence[Mapping[str, object]],
        warnings: Sequence[object],
        quality: Mapping[str, object],
        provenance: Mapping[str, object],
    ) -> MediaTranscriptRecord:
        if not 0 < len(segments) <= 100_000:
            raise ValueError("transcript segment count is invalid")
        warning_json = self._media_metadata_json(list(warnings), kind="warnings")
        quality_json = self._media_metadata_json(dict(quality), kind="quality")
        provenance_json = self._media_metadata_json(dict(provenance), kind="provenance")
        with self._lock:
            job_row = self.connection.execute(
                "SELECT * FROM workbench_media_job WHERE media_job_id=?",
                (media_job_id,),
            ).fetchone()
        if job_row is None:
            raise KeyError(media_job_id)
        job = self._media_job(job_row)
        self.membership(job.matter_id, job.requested_by)
        existing = self.media_transcript(
            job.matter_id, job.document_id, job.source_version_id
        )
        if existing is not None:
            return existing
        if job.state != "running":
            raise WorkspaceProblem("This transcription job is not ready to import.")

        normalized: list[dict[str, object]] = []
        seen_external: set[str] = set()
        speakers: dict[str, str] = {}
        total_text = 0
        last_start = -1
        for ordinal, item in enumerate(segments, 1):
            if not isinstance(item, Mapping):
                raise ValueError("transcript segment is invalid")
            external = self._safe_text(
                str(item.get("external_segment_id") or ""),
                label="Processor segment",
                maximum=100,
            )
            if external in seen_external:
                raise ValueError("transcript segment identity is duplicated")
            seen_external.add(external)
            start_ms = int(item.get("start_ms", -1))
            end_ms = int(item.get("end_ms", -1))
            if (
                start_ms < 0
                or end_ms <= start_ms
                or end_ms > job.duration_ms + 2_000
                or start_ms < last_start
            ):
                raise ValueError("transcript timestamps are invalid")
            last_start = start_ms
            model_text = self._safe_text(
                str(item.get("model_text") or ""),
                label="Transcript text",
                maximum=20_000,
                multiline=True,
            )
            total_text += len(model_text)
            if total_text > 20_000_000:
                raise ValueError("transcript text is too large")
            translated_value = item.get("translated_text")
            translated = None
            if translated_value:
                translated = self._safe_text(
                    str(translated_value),
                    label="Translated transcript text",
                    maximum=20_000,
                    multiline=True,
                )
            cluster = self._safe_text(
                str(item.get("speaker_cluster") or "UNKNOWN"),
                label="Speaker cluster",
                maximum=100,
            )
            display = self._safe_text(
                str(item.get("speaker_display_name") or cluster),
                label="Speaker label",
                maximum=120,
            )
            speakers.setdefault(cluster, display)
            confidence_value = item.get("confidence")
            confidence = None
            if confidence_value is not None:
                confidence = float(confidence_value)
                if not 0 <= confidence <= 1:
                    raise ValueError("transcript confidence is invalid")
            normalized.append(
                {
                    "ordinal": ordinal,
                    "external_segment_id": external,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker_cluster": cluster,
                    "model_text": model_text,
                    "translated_text": translated,
                    "confidence": confidence,
                    "low_confidence": int(bool(item.get("low_confidence"))),
                    "overlap": int(bool(item.get("overlap"))),
                }
            )
        digest_payload = json.dumps(
            normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        transcript_digest = hashlib.sha256(digest_payload).hexdigest()
        now = self._now()
        transcript_id = f"transcript-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(job.matter_id, job.requested_by)
            self.connection.execute(
                "INSERT INTO workbench_media_transcript("
                "transcript_id,matter_id,document_id,source_version_id,media_job_id,"
                "duration_ms,segment_count,transcript_digest,warnings_json,quality_json,"
                "provenance_json,imported_by,imported_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    transcript_id,
                    job.matter_id,
                    job.document_id,
                    job.source_version_id,
                    job.media_job_id,
                    job.duration_ms,
                    len(normalized),
                    transcript_digest,
                    warning_json,
                    quality_json,
                    provenance_json,
                    job.requested_by,
                    now,
                    now,
                ),
            )
            for item in normalized:
                self.connection.execute(
                    "INSERT INTO workbench_transcript_segment("
                    "segment_id,transcript_id,matter_id,ordinal,external_segment_id,start_ms,"
                    "end_ms,speaker_cluster,model_text,translated_text,confidence,low_confidence,"
                    "overlap,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"media-segment-{uuid.uuid4().hex}",
                        transcript_id,
                        job.matter_id,
                        item["ordinal"],
                        item["external_segment_id"],
                        item["start_ms"],
                        item["end_ms"],
                        item["speaker_cluster"],
                        item["model_text"],
                        item["translated_text"],
                        item["confidence"],
                        item["low_confidence"],
                        item["overlap"],
                        now,
                    ),
                )
            for cluster, display in sorted(speakers.items()):
                self.connection.execute(
                    "INSERT INTO workbench_speaker_mapping_revision("
                    "mapping_revision_id,transcript_id,matter_id,speaker_cluster,revision,"
                    "display_name,identity_state,edited_by,edited_at) "
                    "VALUES (?,?,?,?,0,?,'cluster',?,?)",
                    (
                        f"speaker-mapping-{uuid.uuid4().hex}",
                        transcript_id,
                        job.matter_id,
                        cluster,
                        display,
                        job.requested_by,
                        now,
                    ),
                )
            self.connection.execute(
                "INSERT INTO workbench_media_summary("
                "transcript_id,matter_id,document_id,source_version_id,"
                "total_segment_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    transcript_id,
                    job.matter_id,
                    job.document_id,
                    job.source_version_id,
                    len(normalized),
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_media_transcript WHERE transcript_id=?",
                (transcript_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("transcript did not persist")
        return self._media_transcript(row)

    def media_summary(
        self, matter_id: str, document_id: str, source_version_id: str
    ) -> MediaSummaryRecord | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_media_summary WHERE matter_id=? "
                "AND document_id=? AND source_version_id=?",
                (matter_id, document_id, source_version_id),
            ).fetchone()
        return self._media_summary(row) if row is not None else None

    def ensure_media_summary_queue(self) -> int:
        """Queue missing overviews and bounded startup recovery for failures.

        This is intentionally idempotent and content-free.  It is run when the
        media coordinator starts so transcripts created before the overview
        feature, or committed immediately before an interrupted process exit,
        receive the same durable automatic treatment as new transcripts. A
        failed automatic overview receives at most two restart retries (three
        total attempts); staff can still explicitly retry after that bound.
        """

        now = self._now()
        with self._lock, self.connection:
            inserted = self.connection.execute(
                "INSERT INTO workbench_media_summary("
                "transcript_id,matter_id,document_id,source_version_id,"
                "total_segment_count,created_at,updated_at) "
                "SELECT transcript.transcript_id,transcript.matter_id,"
                "transcript.document_id,transcript.source_version_id,"
                "transcript.segment_count,?,? "
                "FROM workbench_media_transcript transcript "
                "JOIN workbench_matter_lifecycle lifecycle "
                "ON lifecycle.matter_id=transcript.matter_id "
                "LEFT JOIN workbench_media_summary summary "
                "ON summary.transcript_id=transcript.transcript_id "
                "WHERE lifecycle.state='active' AND summary.transcript_id IS NULL",
                (now, now),
            ).rowcount
            retried = self.connection.execute(
                "UPDATE workbench_media_summary SET state='queued',payload_json='{}',"
                "basis_digest='',covered_segment_count=0,"
                "message='Retrying the automatic overview after restart.',"
                "started_at=NULL,finished_at=NULL,updated_at=? "
                "WHERE state='failed' AND attempts<? AND EXISTS ("
                "SELECT 1 FROM workbench_matter_lifecycle lifecycle "
                "WHERE lifecycle.matter_id=workbench_media_summary.matter_id "
                "AND lifecycle.state='active')",
                (now, MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS),
            ).rowcount
        return int(inserted) + int(retried)

    def claim_media_summary(self) -> MediaSummaryRecord | None:
        """Claim the oldest new or stale overview from active matter state."""

        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT summary.transcript_id,summary.matter_id "
                "FROM workbench_media_summary summary "
                "JOIN workbench_matter_lifecycle lifecycle "
                "ON lifecycle.matter_id=summary.matter_id "
                "LEFT JOIN workbench_job_matter_schedule schedule "
                "ON schedule.queue_kind='media_summary' "
                "AND schedule.matter_id=summary.matter_id "
                "WHERE summary.state IN ('queued','stale') "
                "AND lifecycle.state='active' "
                "ORDER BY COALESCE(schedule.claim_sequence,0),"
                "CASE WHEN summary.state='queued' THEN summary.created_at "
                "ELSE summary.updated_at END,summary.transcript_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = self.connection.execute(
                "UPDATE workbench_media_summary SET state='running',"
                "attempts=attempts+1,payload_json='{}',basis_digest='',"
                "covered_segment_count=0,"
                "message='Creating an orientation from the machine transcript.',"
                "started_at=?,finished_at=NULL,updated_at=? "
                "WHERE transcript_id=? AND state IN ('queued','stale')",
                (now, now, row["transcript_id"]),
            ).rowcount
            if changed != 1:
                return None
            self._advance_matter_schedule_locked(
                "media_summary", row["matter_id"]
            )
            claimed = self.connection.execute(
                "SELECT * FROM workbench_media_summary WHERE transcript_id=?",
                (row["transcript_id"],),
            ).fetchone()
        return self._media_summary(claimed) if claimed is not None else None

    def media_summary_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM workbench_media_summary GROUP BY state"
            ).fetchall()
        counts = {state: 0 for state in ("queued", "running", "ready", "stale", "failed")}
        counts.update({str(row["state"]): int(row["count"]) for row in rows})
        return counts

    def media_summaries_for_matter(
        self, matter_id: str
    ) -> tuple[MediaSummaryRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_media_summary WHERE matter_id=? "
                "ORDER BY updated_at,transcript_id",
                (matter_id,),
            ).fetchall()
        return tuple(self._media_summary(row) for row in rows)

    def recover_running_media_summaries(self) -> int:
        now = self._now()
        with self._lock, self.connection:
            return self.connection.execute(
                "UPDATE workbench_media_summary SET "
                "state=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,"
                "message=CASE WHEN attempts>=? "
                "THEN 'The automatic overview recovery limit was reached.' "
                "ELSE 'Resuming overview after restart.' END,updated_at=? "
                "WHERE state='running' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active')",
                (
                    MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS,
                    MAX_AUTOMATIC_MEDIA_SUMMARY_ATTEMPTS,
                    now,
                ),
            ).rowcount

    def start_media_summary(self, transcript_id: str) -> MediaSummaryRecord:
        if not _TRANSCRIPT.fullmatch(transcript_id or ""):
            raise KeyError(transcript_id)
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_media_summary SET state='running',attempts=attempts+1,"
                "message='Creating an orientation from the machine transcript.',"
                "started_at=COALESCE(started_at,?),finished_at=NULL,updated_at=? "
                "WHERE transcript_id=? AND state='queued'",
                (now, now, transcript_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_summary WHERE transcript_id=?",
                (transcript_id,),
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(transcript_id)
        return self._media_summary(row)

    def finish_media_summary(
        self,
        transcript_id: str,
        *,
        payload: Mapping[str, object],
        basis_digest: str,
        covered_segment_count: int,
        total_segment_count: int,
    ) -> MediaSummaryRecord:
        if not _TRANSCRIPT.fullmatch(transcript_id or ""):
            raise KeyError(transcript_id)
        if not re.fullmatch(r"[0-9a-f]{64}", basis_digest or ""):
            raise ValueError("invalid media summary basis")
        if not 0 <= covered_segment_count <= total_segment_count <= 100_000:
            raise ValueError("invalid media summary coverage")
        payload_json = self._media_metadata_json(dict(payload), kind="summary")
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_media_summary SET state='ready',payload_json=?,"
                "basis_digest=?,covered_segment_count=?,total_segment_count=?,"
                "message='',finished_at=?,updated_at=? "
                "WHERE transcript_id=? AND state='running'",
                (
                    payload_json,
                    basis_digest,
                    covered_segment_count,
                    total_segment_count,
                    now,
                    now,
                    transcript_id,
                ),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_summary WHERE transcript_id=?",
                (transcript_id,),
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(transcript_id)
        return self._media_summary(row)

    def fail_media_summary(self, transcript_id: str, message: str) -> MediaSummaryRecord:
        if not _TRANSCRIPT.fullmatch(transcript_id or ""):
            raise KeyError(transcript_id)
        value = self._safe_text(
            message or "The transcript overview could not be created. Try again.",
            label="Media summary message",
            maximum=240,
        )
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_media_summary SET state='failed',message=?,"
                "finished_at=?,updated_at=? WHERE transcript_id=? AND state='running'",
                (value, now, now, transcript_id),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM workbench_media_summary WHERE transcript_id=?",
                (transcript_id,),
            ).fetchone()
        if changed != 1 or row is None:
            raise KeyError(transcript_id)
        return self._media_summary(row)

    def queue_media_summary(
        self, matter_id: str, document_id: str, source_version_id: str, actor_id: str
    ) -> MediaSummaryRecord:
        self.membership(matter_id, actor_id)
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor_id)
            transcript = self.connection.execute(
                "SELECT transcript_id,segment_count FROM workbench_media_transcript "
                "WHERE matter_id=? AND document_id=? AND source_version_id=?",
                (matter_id, document_id, source_version_id),
            ).fetchone()
            if transcript is None:
                raise KeyError(document_id)
            existing = self.connection.execute(
                "SELECT state FROM workbench_media_summary WHERE transcript_id=?",
                (transcript["transcript_id"],),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO workbench_media_summary("
                    "transcript_id,matter_id,document_id,source_version_id,"
                    "total_segment_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        transcript["transcript_id"],
                        matter_id,
                        document_id,
                        source_version_id,
                        int(transcript["segment_count"]),
                        now,
                        now,
                    ),
                )
            elif existing["state"] in {"failed", "stale"}:
                self.connection.execute(
                    "UPDATE workbench_media_summary SET state='queued',payload_json='{}',"
                    "basis_digest='',covered_segment_count=0,message='',"
                    "started_at=NULL,finished_at=NULL,updated_at=? WHERE transcript_id=?",
                    (now, transcript["transcript_id"]),
                )
            else:
                raise WorkspaceProblem("This transcript overview is already current or in progress.")
            row = self.connection.execute(
                "SELECT * FROM workbench_media_summary WHERE transcript_id=?",
                (transcript["transcript_id"],),
            ).fetchone()
        if row is None:
            raise RuntimeError("media summary did not persist")
        return self._media_summary(row)

    def transcript_segments(
        self,
        matter_id: str,
        document_id: str,
        source_version_id: str,
    ) -> tuple[TranscriptSegmentRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "WITH text_revision AS ("
                " SELECT revision.segment_id,revision.text,revision.revision "
                " FROM workbench_transcript_segment_revision revision "
                " JOIN (SELECT segment_id,MAX(revision) AS revision "
                "       FROM workbench_transcript_segment_revision GROUP BY segment_id) latest "
                " ON latest.segment_id=revision.segment_id AND latest.revision=revision.revision"
                "), speaker_revision AS ("
                " SELECT mapping.transcript_id,mapping.speaker_cluster,mapping.display_name,"
                "        mapping.identity_state,mapping.revision "
                " FROM workbench_speaker_mapping_revision mapping "
                " JOIN (SELECT transcript_id,speaker_cluster,MAX(revision) AS revision "
                "       FROM workbench_speaker_mapping_revision "
                "       GROUP BY transcript_id,speaker_cluster) latest "
                " ON latest.transcript_id=mapping.transcript_id "
                " AND latest.speaker_cluster=mapping.speaker_cluster "
                " AND latest.revision=mapping.revision"
                ") SELECT segment.segment_id,segment.transcript_id,segment.matter_id,"
                "segment.ordinal,segment.external_segment_id,segment.start_ms,segment.end_ms,"
                "segment.speaker_cluster,segment.model_text,segment.translated_text,"
                "segment.confidence,segment.low_confidence,segment.overlap,"
                "COALESCE(text_revision.text,segment.model_text) AS current_text,"
                "COALESCE(text_revision.revision,0) AS current_revision,"
                "speaker_revision.display_name AS speaker_display_name,"
                "speaker_revision.identity_state AS speaker_identity_state,"
                "speaker_revision.revision AS speaker_revision,segment.created_at "
                "FROM workbench_transcript_segment segment "
                "JOIN workbench_media_transcript transcript "
                "ON transcript.transcript_id=segment.transcript_id "
                "LEFT JOIN text_revision ON text_revision.segment_id=segment.segment_id "
                "JOIN speaker_revision ON speaker_revision.transcript_id=segment.transcript_id "
                "AND speaker_revision.speaker_cluster=segment.speaker_cluster "
                "WHERE transcript.matter_id=? AND transcript.document_id=? "
                "AND transcript.source_version_id=? ORDER BY segment.ordinal",
                (matter_id, document_id, source_version_id),
            ).fetchall()
        return tuple(self._transcript_segment(row) for row in rows)

    def transcript_speakers(
        self, matter_id: str, document_id: str, source_version_id: str
    ) -> tuple[SpeakerMappingRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "WITH latest AS ("
                " SELECT mapping.transcript_id,mapping.speaker_cluster,MAX(mapping.revision) revision "
                " FROM workbench_speaker_mapping_revision mapping "
                " GROUP BY mapping.transcript_id,mapping.speaker_cluster"
                ") SELECT mapping.transcript_id,mapping.matter_id,mapping.speaker_cluster,"
                "mapping.display_name,mapping.identity_state,mapping.revision,"
                "COUNT(segment.segment_id) AS segment_count,mapping.edited_by,mapping.edited_at "
                "FROM workbench_speaker_mapping_revision mapping "
                "JOIN latest ON latest.transcript_id=mapping.transcript_id "
                "AND latest.speaker_cluster=mapping.speaker_cluster "
                "AND latest.revision=mapping.revision "
                "JOIN workbench_media_transcript transcript "
                "ON transcript.transcript_id=mapping.transcript_id "
                "LEFT JOIN workbench_transcript_segment segment "
                "ON segment.transcript_id=mapping.transcript_id "
                "AND segment.speaker_cluster=mapping.speaker_cluster "
                "WHERE transcript.matter_id=? AND transcript.document_id=? "
                "AND transcript.source_version_id=? "
                "GROUP BY mapping.mapping_revision_id "
                "ORDER BY mapping.speaker_cluster",
                (matter_id, document_id, source_version_id),
            ).fetchall()
        return tuple(self._speaker_mapping(row) for row in rows)

    def transcript_support_history(
        self,
        matter_id: str,
        document_id: str,
        source_version_id: str,
    ) -> dict[int, tuple[str, ...]]:
        """Return actual prior transcript excerpts for legacy citation lookup.

        Text and speaker-label revisions are merged in their saved order, so
        lookup remains linear in real edits rather than generating combinations
        that never appeared in the reviewed transcript.
        """

        with self._lock:
            segments = self.connection.execute(
                "SELECT segment.segment_id,segment.ordinal,segment.speaker_cluster,"
                "segment.model_text FROM workbench_transcript_segment segment "
                "JOIN workbench_media_transcript transcript "
                "ON transcript.transcript_id=segment.transcript_id "
                "WHERE transcript.matter_id=? AND transcript.document_id=? "
                "AND transcript.source_version_id=? ORDER BY segment.ordinal",
                (matter_id, document_id, source_version_id),
            ).fetchall()
            text_rows = self.connection.execute(
                "SELECT revision.segment_id,revision.revision,revision.text,revision.edited_at "
                "FROM workbench_transcript_segment_revision revision "
                "JOIN workbench_transcript_segment segment "
                "ON segment.segment_id=revision.segment_id "
                "JOIN workbench_media_transcript transcript "
                "ON transcript.transcript_id=segment.transcript_id "
                "WHERE transcript.matter_id=? AND transcript.document_id=? "
                "AND transcript.source_version_id=? "
                "ORDER BY revision.edited_at,revision.revision",
                (matter_id, document_id, source_version_id),
            ).fetchall()
            speaker_rows = self.connection.execute(
                "SELECT mapping.speaker_cluster,mapping.revision,mapping.display_name,"
                "mapping.edited_at FROM workbench_speaker_mapping_revision mapping "
                "JOIN workbench_media_transcript transcript "
                "ON transcript.transcript_id=mapping.transcript_id "
                "WHERE transcript.matter_id=? AND transcript.document_id=? "
                "AND transcript.source_version_id=? "
                "ORDER BY mapping.edited_at,mapping.revision",
                (matter_id, document_id, source_version_id),
            ).fetchall()
        text_events: dict[str, list[tuple[str, int, str]]] = {}
        for row in text_rows:
            text_events.setdefault(str(row["segment_id"]), []).append(
                (str(row["edited_at"]), int(row["revision"]), str(row["text"]))
            )
        speaker_initial: dict[str, str] = {}
        speaker_events: dict[str, list[tuple[str, int, str]]] = {}
        for row in speaker_rows:
            cluster = str(row["speaker_cluster"])
            revision = int(row["revision"])
            if revision == 0:
                speaker_initial[cluster] = str(row["display_name"])
            else:
                speaker_events.setdefault(cluster, []).append(
                    (str(row["edited_at"]), revision, str(row["display_name"]))
                )
        staff_initial = {
            cluster: f"Speaker {ordinal}"
            for ordinal, cluster in enumerate(sorted(speaker_initial), 1)
        }
        history: dict[int, tuple[str, ...]] = {}
        for row in segments:
            segment_id = str(row["segment_id"])
            cluster = str(row["speaker_cluster"])
            text = str(row["model_text"])
            labels = {
                speaker_initial.get(cluster, cluster),
                staff_initial.get(cluster, cluster),
            }
            excerpts = [f"{label}: {text}" for label in sorted(labels)]
            events = [
                (when, revision, "text", value)
                for when, revision, value in text_events.get(segment_id, ())
            ] + [
                (when, revision, "speaker", value)
                for when, revision, value in speaker_events.get(cluster, ())
            ]
            for _when, _revision, kind, value in sorted(events):
                if kind == "text":
                    text = value
                else:
                    labels = {value}
                for label in sorted(labels):
                    excerpt = f"{label}: {text}"
                    if excerpt not in excerpts:
                        excerpts.append(excerpt)
            history[int(row["ordinal"])] = tuple(excerpts)
        return history

    def revise_transcript_segment(
        self,
        matter_id: str,
        document_id: str,
        source_version_id: str,
        segment_id: str,
        *,
        expected_revision: int,
        text: str,
        actor_id: str,
    ) -> TranscriptSegmentRecord:
        if not _TRANSCRIPT_SEGMENT.fullmatch(segment_id or ""):
            raise KeyError(segment_id)
        self.membership(matter_id, actor_id)
        value = self._safe_text(
            text, label="Transcript text", maximum=20_000, multiline=True
        )
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT segment.segment_id,segment.model_text,transcript.transcript_id "
                "FROM workbench_transcript_segment segment "
                "JOIN workbench_media_transcript transcript "
                "ON transcript.transcript_id=segment.transcript_id "
                "WHERE segment.segment_id=? AND transcript.matter_id=? "
                "AND transcript.document_id=? AND transcript.source_version_id=?",
                (segment_id, matter_id, document_id, source_version_id),
            ).fetchone()
            if row is None:
                raise KeyError(segment_id)
            revision_row = self.connection.execute(
                "SELECT revision,text FROM workbench_transcript_segment_revision "
                "WHERE segment_id=? ORDER BY revision DESC LIMIT 1",
                (segment_id,),
            ).fetchone()
            current_revision = int(revision_row["revision"]) if revision_row else 0
            current_text = str(revision_row["text"]) if revision_row else str(row["model_text"])
            if expected_revision != current_revision:
                raise WorkspaceProblem(
                    "This transcript passage changed in another session. Refresh before editing it."
                )
            if value != current_text:
                self.connection.execute(
                    "INSERT INTO workbench_transcript_segment_revision("
                    "revision_id,segment_id,matter_id,revision,text,edited_by,edited_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        f"segment-revision-{uuid.uuid4().hex}",
                        segment_id,
                        matter_id,
                        current_revision + 1,
                        value,
                        actor_id,
                        now,
                    ),
                )
                self.connection.execute(
                    "UPDATE workbench_media_transcript SET review_state='human_reviewed',"
                    "updated_at=? WHERE transcript_id=?",
                    (now, row["transcript_id"]),
                )
                self.connection.execute(
                    "UPDATE workbench_media_summary SET state='stale',"
                    "message='The transcript changed after this overview was created.',"
                    "updated_at=? WHERE transcript_id=? AND state IN ('queued','running','ready')",
                    (now, row["transcript_id"]),
                )
        return next(
            segment
            for segment in self.transcript_segments(
                matter_id, document_id, source_version_id
            )
            if segment.segment_id == segment_id
        )

    def revise_speaker_mapping(
        self,
        matter_id: str,
        document_id: str,
        source_version_id: str,
        speaker_cluster: str,
        *,
        expected_revision: int,
        display_name: str,
        identity_state: str,
        actor_id: str,
    ) -> SpeakerMappingRecord:
        self.membership(matter_id, actor_id)
        cluster = self._safe_text(
            speaker_cluster, label="Speaker cluster", maximum=100
        )
        if identity_state not in {"cluster", "confirmed"}:
            raise WorkspaceProblem("Speaker review state is invalid.")
        label = self._safe_text(
            display_name, label="Speaker label", maximum=120
        )
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT mapping.transcript_id,mapping.revision,mapping.display_name,"
                "mapping.identity_state FROM workbench_speaker_mapping_revision mapping "
                "JOIN workbench_media_transcript transcript "
                "ON transcript.transcript_id=mapping.transcript_id "
                "WHERE transcript.matter_id=? AND transcript.document_id=? "
                "AND transcript.source_version_id=? AND mapping.speaker_cluster=? "
                "ORDER BY mapping.revision DESC LIMIT 1",
                (matter_id, document_id, source_version_id, cluster),
            ).fetchone()
            if row is None:
                raise KeyError(cluster)
            revision = int(row["revision"])
            if expected_revision != revision:
                raise WorkspaceProblem(
                    "This speaker label changed in another session. Refresh before editing it."
                )
            if label != row["display_name"] or identity_state != row["identity_state"]:
                self.connection.execute(
                    "INSERT INTO workbench_speaker_mapping_revision("
                    "mapping_revision_id,transcript_id,matter_id,speaker_cluster,revision,"
                    "display_name,identity_state,edited_by,edited_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"speaker-mapping-{uuid.uuid4().hex}",
                        row["transcript_id"],
                        matter_id,
                        cluster,
                        revision + 1,
                        label,
                        identity_state,
                        actor_id,
                        now,
                    ),
                )
                self.connection.execute(
                    "UPDATE workbench_media_transcript SET review_state='human_reviewed',"
                    "updated_at=? WHERE transcript_id=?",
                    (now, row["transcript_id"]),
                )
                self.connection.execute(
                    "UPDATE workbench_media_summary SET state='stale',"
                    "message='Speaker labels changed after this overview was created.',"
                    "updated_at=? WHERE transcript_id=? AND state IN ('queued','running','ready')",
                    (now, row["transcript_id"]),
                )
        return next(
            speaker
            for speaker in self.transcript_speakers(
                matter_id, document_id, source_version_id
            )
            if speaker.speaker_cluster == cluster
        )

    def create_media_clip(
        self,
        matter_id: str,
        document_id: str,
        source_version_id: str,
        *,
        title: str,
        start_ms: int,
        end_ms: int,
        actor_id: str,
    ) -> MediaClipRecord:
        self.membership(matter_id, actor_id)
        label = self._safe_text(title, label="Clip title", maximum=160)
        transcript = self.media_transcript(matter_id, document_id, source_version_id)
        if transcript is None:
            raise KeyError(document_id)
        if (
            start_ms < 0
            or end_ms <= start_ms
            or end_ms > transcript.duration_ms
            or end_ms - start_ms > 10 * 60 * 1000
        ):
            raise WorkspaceProblem(
                "Choose a clip between one second and ten minutes within this recording."
            )
        if end_ms - start_ms < 1_000:
            raise WorkspaceProblem(
                "Choose a clip between one second and ten minutes within this recording."
            )
        clip_id = f"media-clip-{uuid.uuid4().hex}"
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO workbench_media_clip("
                "clip_id,matter_id,document_id,source_version_id,title,start_ms,end_ms,"
                "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    clip_id,
                    matter_id,
                    document_id,
                    source_version_id,
                    label,
                    start_ms,
                    end_ms,
                    actor_id,
                    now,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_media_clip WHERE clip_id=?", (clip_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("media clip did not persist")
        return self._media_clip(row)

    def media_clips(
        self, matter_id: str, document_id: str, source_version_id: str
    ) -> tuple[MediaClipRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_media_clip WHERE matter_id=? AND document_id=? "
                "AND source_version_id=? ORDER BY start_ms,created_at,clip_id",
                (matter_id, document_id, source_version_id),
            ).fetchall()
        return tuple(self._media_clip(row) for row in rows)

    def media_clip(self, matter_id: str, clip_id: str) -> MediaClipRecord:
        if not _MEDIA_CLIP.fullmatch(clip_id or ""):
            raise KeyError(clip_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_media_clip WHERE matter_id=? AND clip_id=?",
                (matter_id, clip_id),
            ).fetchone()
        if row is None:
            raise KeyError(clip_id)
        return self._media_clip(row)

    def delete_media_clip(self, matter_id: str, clip_id: str, actor_id: str) -> None:
        self.membership(matter_id, actor_id)
        with self._lock, self.connection:
            changed = self.connection.execute(
                "DELETE FROM workbench_media_clip WHERE matter_id=? AND clip_id=?",
                (matter_id, clip_id),
            ).rowcount
        if changed != 1:
            raise KeyError(clip_id)

    def remove_media_document(self, matter_id: str, document_id: str) -> str | None:
        """Remove durable derived media state and return transient job id to purge."""

        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT external_job_id FROM workbench_media_job "
                "WHERE matter_id=? AND document_id=?",
                (matter_id, document_id),
            ).fetchone()
            self.connection.execute(
                "DELETE FROM workbench_media_job WHERE matter_id=? AND document_id=?",
                (matter_id, document_id),
            )
        return str(row["external_job_id"]) if row and row["external_job_id"] else None

    @staticmethod
    def _source_document_id(document_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", document_id or ""):
            raise ValueError("invalid source identifier")
        return document_id

    def _active_matter_locked(self, matter_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT m.matter_id,m.owner_id FROM workbench_matter m "
            "JOIN workbench_matter_lifecycle ml ON ml.matter_id=m.matter_id "
            "WHERE m.matter_id=? AND ml.state='active'",
            (matter_id,),
        ).fetchone()
        if row is None:
            raise KeyError(matter_id)
        return row

    def _unique_library_name_locked(self, table: str, matter_id: str, requested: str) -> tuple[str, str]:
        if table not in {"workbench_source_collection", "workbench_source_set"}:
            raise RuntimeError("invalid source-library table")
        base = self._safe_text(requested, label="Name", maximum=160)
        for number in range(1, 1_001):
            candidate = base if number == 1 else f"{base} ({number})"
            key = candidate.casefold()
            row = self.connection.execute(
                f"SELECT 1 FROM {table} WHERE matter_id=? AND name_key=?",
                (matter_id, key),
            ).fetchone()
            if row is None:
                return candidate, key
        raise WorkspaceProblem("A unique name could not be created.")

    def _create_source_collection_locked(
        self,
        matter_id: str,
        requested_name: str,
        kind: str,
        actor_id: str | None,
        now: str,
    ) -> SourceCollectionRecord:
        if kind not in {"upload", "registered", "migrated"}:
            raise ValueError("invalid source collection kind")
        name, name_key = self._unique_library_name_locked(
            "workbench_source_collection", matter_id, requested_name
        )
        collection_id = f"source-collection-{uuid.uuid4().hex}"
        self.connection.execute(
            "INSERT INTO workbench_source_collection("
            "collection_id,matter_id,name,name_key,kind,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (collection_id, matter_id, name, name_key, kind, actor_id, now, now),
        )
        return SourceCollectionRecord(
            collection_id, matter_id, name, kind, actor_id, now, now, 0
        )

    def create_source_collection(
        self, matter_id: str, name: str, kind: str, actor_id: str
    ) -> SourceCollectionRecord:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            self._active_matter_locked(matter_id)
            self.membership(matter_id, actor)
            return self._create_source_collection_locked(
                matter_id, name, kind, actor, now
            )

    def _prepare_source_catalog_rows(
        self, matter_id: str, sources: Sequence[Mapping[str, object]]
    ) -> list[tuple[object, ...]]:
        allowed_kinds = {
            "PDF",
            "DOCX",
            "TXT",
            "AUDIO",
            "VIDEO",
            "IMAGE",
            "EMAIL",
            "SPREADSHEET",
        }
        prepared: list[tuple[object, ...]] = []
        seen: set[str] = set()
        now = self._now()
        for source in sources:
            document_id = self._source_document_id(str(source.get("document_id", "")))
            if document_id in seen:
                raise ValueError("duplicate source catalog identity")
            seen.add(document_id)
            version_id = str(source.get("version_id", ""))
            action_token = str(source.get("action_token", ""))
            if not re.fullmatch(r"[0-9a-f]{32}", version_id) or not re.fullmatch(
                r"[0-9a-f]{32}", action_token
            ):
                raise ValueError("invalid source catalog version")
            display_name = self._safe_text(
                str(source.get("display_name", "")),
                label="Source display name",
                maximum=2_048,
            )
            relative_path = self._safe_text(
                str(source.get("relative_path", "")),
                label="Source relative path",
                maximum=2_048,
            )
            media_type = self._safe_text(
                str(source.get("media_type", "")),
                label="Source media type",
                maximum=120,
            )
            kind = str(source.get("kind", ""))
            tone = str(source.get("tone", ""))
            origin = str(source.get("origin", ""))
            if kind not in allowed_kinds or tone not in {
                "ready",
                "processing",
                "attention",
            } or origin not in {"upload", "registered"}:
                raise ValueError("invalid source catalog classification")
            source_state = self._safe_text(
                str(source.get("source_state", "")),
                label="Source state",
                maximum=80,
            )
            state_label = self._safe_text(
                str(source.get("state_label", "")),
                label="Source state label",
                maximum=500,
            )
            count_label = self._safe_text(
                str(source.get("count_label", "")),
                label="Source count label",
                maximum=100,
            )
            processing_stage = self._safe_text(
                str(source.get("processing_stage", "")),
                label="Source processing stage",
                maximum=160,
                required=False,
            )
            integers: list[int] = []
            for key in (
                "completed_units",
                "total_units",
                "page_count",
                "duration_ms",
                "byte_size",
            ):
                value = source.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("invalid source catalog count")
                integers.append(value)
            booleans: list[int] = []
            for key in ("retryable", "removable", "has_video"):
                value = source.get(key, False)
                if not isinstance(value, bool):
                    raise ValueError("invalid source catalog flag")
                booleans.append(int(value))
            content_basis_digest = str(source.get("content_basis_digest", ""))
            if content_basis_digest and not re.fullmatch(
                r"[0-9a-f]{64}", content_basis_digest
            ):
                raise ValueError("invalid source content basis")
            search_key = f"{display_name}\n{relative_path}".casefold()
            prepared.append(
                (
                    matter_id,
                    document_id,
                    version_id,
                    action_token,
                    display_name,
                    display_name.casefold(),
                    relative_path,
                    search_key,
                    media_type,
                    kind,
                    source_state,
                    tone,
                    state_label,
                    count_label,
                    processing_stage,
                    *integers,
                    origin,
                    *booleans,
                    content_basis_digest,
                    now,
                    now,
                )
            )
        return prepared

    def _upsert_source_catalog_rows_locked(
        self, prepared: Sequence[tuple[object, ...]]
    ) -> None:
        if not prepared:
            return
        self.connection.executemany(
            "INSERT INTO workbench_source_catalog("
            "matter_id,document_id,version_id,action_token,display_name,display_name_key,"
            "relative_path,search_key,media_type,kind,source_state,tone,state_label,"
            "count_label,processing_stage,completed_units,total_units,page_count,"
            "duration_ms,byte_size,origin,retryable,removable,has_video,content_basis_digest,"
            "cataloged_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(matter_id,document_id) DO UPDATE SET "
            "version_id=excluded.version_id,action_token=excluded.action_token,"
            "display_name=excluded.display_name,display_name_key=excluded.display_name_key,"
            "relative_path=excluded.relative_path,search_key=excluded.search_key,"
            "media_type=excluded.media_type,kind=excluded.kind,"
            "source_state=excluded.source_state,tone=excluded.tone,"
            "state_label=excluded.state_label,count_label=excluded.count_label,"
            "processing_stage=excluded.processing_stage,"
            "completed_units=excluded.completed_units,total_units=excluded.total_units,"
            "page_count=excluded.page_count,duration_ms=excluded.duration_ms,"
            "byte_size=excluded.byte_size,origin=excluded.origin,"
            "retryable=excluded.retryable,removable=excluded.removable,"
            "has_video=excluded.has_video,content_basis_digest=excluded.content_basis_digest,"
            "updated_at=excluded.updated_at",
            prepared,
        )

    def _sync_source_byte_matches_locked(
        self, matter_id: str, sources: Sequence[Mapping[str, object]]
    ) -> None:
        # Only the committed registry projection supplies this digest, never an
        # intake descriptor. Remove a previous lookup when admission is unknown.
        self.connection.executemany(
            "DELETE FROM workbench_source_byte_match WHERE matter_id=? AND document_id=?",
            [(matter_id, source["document_id"]) for source in sources],
        )
        rows = []
        for source in sources:
            digest = source.get("source_sha256", "")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            if source["source_state"] not in {
                "queued", "processing", "ready", "needs_ocr", "failed", "playback_only"
            }:
                continue
            rows.append((matter_id, source["document_id"], source["version_id"],
                digest, source["byte_size"]))
        self.connection.executemany(
            "INSERT INTO workbench_source_byte_match VALUES (?,?,?,?,?)", rows
        )

    def source_byte_comparison_available(self, matter_id: str, token: str) -> bool:
        with self._lock:
            self._active_matter_locked(matter_id)
            return self.connection.execute(
                "SELECT 1 FROM workbench_current_source_bytes "
                "WHERE matter_id=? AND action_token=? LIMIT 1", (matter_id, token)
            ).fetchone() is not None

    def upsert_source_catalog(
        self, matter_id: str, sources: Sequence[Mapping[str, object]]
    ) -> int:
        """Project only committed source deltas without rebuilding a matter."""

        prepared = self._prepare_source_catalog_rows(matter_id, sources)
        with self._lock, self.connection:
            self._active_matter_locked(matter_id)
            self._upsert_source_catalog_rows_locked(prepared)
            self._sync_source_byte_matches_locked(matter_id, sources)
        return len(prepared)

    def delete_source_catalog(
        self, matter_id: str, document_ids: Sequence[str]
    ) -> int:
        """Remove exact committed source identities from the query projection."""

        prepared = tuple(
            dict.fromkeys(self._source_document_id(document_id) for document_id in document_ids)
        )
        with self._lock, self.connection:
            self._active_matter_locked(matter_id)
            removed = self.connection.executemany(
                "DELETE FROM workbench_source_catalog WHERE matter_id=? AND document_id=?",
                [(matter_id, document_id) for document_id in prepared],
            )
            return removed.rowcount

    def replace_source_catalog(
        self, matter_id: str, sources: Sequence[Mapping[str, object]]
    ) -> int:
        """Repair the queryable projection from the transactional source registry.

        The per-matter registry and managed files remain the recovery boundary.
        This full reconciliation runs when a matter store opens; ordinary state
        transitions use the bounded upsert/delete methods above.
        """

        prepared = self._prepare_source_catalog_rows(matter_id, sources)
        with self._lock, self.connection:
            self._active_matter_locked(matter_id)
            self.connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS workbench_source_catalog_sync ("
                "document_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            self.connection.execute("DELETE FROM workbench_source_catalog_sync")
            if prepared:
                self.connection.executemany(
                    "INSERT INTO workbench_source_catalog_sync(document_id) VALUES (?)",
                    [(row[1],) for row in prepared],
                )
                self._upsert_source_catalog_rows_locked(prepared)
                self._sync_source_byte_matches_locked(matter_id, sources)
                self.connection.execute(
                    "DELETE FROM workbench_source_catalog WHERE matter_id=? AND NOT EXISTS ("
                    "SELECT 1 FROM workbench_source_catalog_sync s "
                    "WHERE s.document_id=workbench_source_catalog.document_id)",
                    (matter_id,),
                )
            else:
                self.connection.execute(
                    "DELETE FROM workbench_source_catalog WHERE matter_id=?", (matter_id,)
                )
            self.connection.execute("DELETE FROM workbench_source_catalog_sync")
        return len(prepared)

    @staticmethod
    def source_folder_path(value: str) -> str:
        if not isinstance(value, str):
            raise WorkspaceProblem("Choose a listed source folder.")
        if not value:
            return ""
        try:
            path = unicodedata.normalize("NFC", value)
            if len(path.encode("utf-8")) > 2048 or any(unicodedata.category(c) in {"Cc", "Cf"} for c in path):
                raise ValueError("invalid folder")
            return validate_relative_path(path)
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceProblem("Choose a listed source folder.") from exc

    def _source_catalog_filter(
        self, matter_id: str, *, query_key: str = "", tone: str = "",
        actionable_only: bool = False, kind: str = "", review_state: str = "",
        collection_id: str = "", source_set_id: str = "", folder: str = "",
        same_content: str = "", matching_only: bool = False,
    ) -> tuple[str, tuple[object, ...]]:
        where = ["c.matter_id=?"]
        parameters: list[object] = [matter_id]
        if query_key:
            where.append("instr(c.search_key, ?) > 0")
            parameters.append(query_key.casefold())
        if tone:
            where.append("c.tone=?")
            parameters.append(tone)
        if actionable_only:
            where.append("c.source_state!='playback_only'")
        if kind:
            where.append("c.kind=?")
            parameters.append(kind)
        if review_state:
            where.append("COALESCE(o.review_state,'unreviewed')=?")
            parameters.append(review_state)
        if collection_id:
            where.append("o.collection_id=?")
            parameters.append(collection_id)
        if source_set_id:
            where.append(
                "EXISTS (SELECT 1 FROM workbench_source_set_item si "
                "WHERE si.matter_id=c.matter_id AND si.document_id=c.document_id "
                "AND si.source_set_id=?)"
            )
            parameters.append(source_set_id)
        if same_content:
            if not re.fullmatch(r"[0-9a-f]{32}", same_content):
                raise WorkspaceProblem("Choose a listed source comparison.")
            where.append(
                "EXISTS (SELECT 1 FROM workbench_current_source_bytes own "
                "JOIN workbench_current_source_bytes ref ON ref.matter_id=own.matter_id "
                "AND ref.source_sha256=own.source_sha256 AND ref.byte_size=own.byte_size "
                "WHERE own.matter_id=c.matter_id AND own.document_id=c.document_id "
                "AND ref.action_token=?)"
            )
            parameters.append(same_content)
        if matching_only:
            where.append(
                "EXISTS (SELECT 1 FROM workbench_current_source_bytes own "
                "JOIN workbench_current_source_bytes peer ON peer.matter_id=own.matter_id "
                "AND peer.source_sha256=own.source_sha256 AND peer.byte_size=own.byte_size "
                "AND peer.document_id!=own.document_id "
                "WHERE own.matter_id=c.matter_id AND own.document_id=c.document_id)"
            )
        folder = self.source_folder_path(folder)
        if folder:
            # Literal separator-bound prefix: percent/underscore are not SQL
            # wildcards, and a similarly named sibling cannot enter this view.
            prefix = folder + "/"
            where.append("substr(c.relative_path,1,?)=? COLLATE BINARY")
            parameters.extend((len(prefix), prefix))
        return " WHERE " + " AND ".join(where), tuple(parameters)

    def source_catalog_folders(
        self, matter_id: str, *, folder: str = "", query_key: str = "", tone: str = "",
        kind: str = "", review_state: str = "", collection_id: str = "",
        source_set_id: str = "", limit: int = 50, offset: int = 0,
        same_content: str = "", matching_only: bool = False,
    ) -> SourceFolderPageRecord:
        """Page immediate directories; counts include descendants under current filters."""
        folder = self.source_folder_path(folder)
        predicate, parameters = self._source_catalog_filter(
            matter_id, folder=folder, query_key=query_key, tone=tone, kind=kind,
            review_state=review_state, collection_id=collection_id, source_set_id=source_set_id,
            same_content=same_content, matching_only=matching_only,
        )
        remaining_start = len(folder) + 2 if folder else 1
        cte = (
            "WITH scoped AS (SELECT substr(c.relative_path,?) AS remaining "
            "FROM workbench_source_catalog c LEFT JOIN workbench_source_organization o "
            "ON o.matter_id=c.matter_id AND o.document_id=c.document_id" + predicate + "), "
            "folders AS (SELECT substr(remaining,1,instr(remaining,'/')-1) AS name,"
            "count(*) AS source_count FROM scoped WHERE instr(remaining,'/')>0 GROUP BY name) "
        )
        with self._lock:
            self._active_matter_locked(matter_id)
            count = self.connection.execute(cte + "SELECT count(*) FROM folders",
                (remaining_start, *parameters)).fetchone()[0]
            rows = self.connection.execute(cte + "SELECT name,source_count FROM folders "
                "ORDER BY name COLLATE NOCASE,name LIMIT ? OFFSET ?",
                (remaining_start, *parameters, min(max(int(limit), 1), 50), max(int(offset), 0))).fetchall()
        return SourceFolderPageRecord(tuple(SourceFolderRecord(row['name'],
            (folder + '/' if folder else '') + row['name'], row['source_count']) for row in rows), count)

    def source_availability_fingerprint(self, matter_id: str, source_set_id: str | None = None) -> str:
        """Stream identity/version/state metadata only for the retrieval scope."""
        if not _IDENTIFIER.fullmatch(matter_id):
            raise KeyError(matter_id)
        digest = hashlib.sha256((b"source-availability-scoped-v4\0" if source_set_id else b"source-availability-v3\0") + matter_id.encode("ascii"))
        with self._lock:
            if self.connection.execute(
                "SELECT 1 FROM workbench_matter WHERE matter_id=?", (matter_id,),
            ).fetchone() is None:
                raise KeyError(matter_id)
            if source_set_id:
                self.source_set(matter_id, source_set_id)
            scope_catalog = (
                " AND EXISTS (SELECT 1 FROM workbench_source_set_item selected "
                "WHERE selected.matter_id=c.matter_id AND selected.document_id=c.document_id AND selected.source_set_id=?)"
                if source_set_id else ""
            )
            scope_upload = (
                " AND EXISTS (SELECT 1 FROM workbench_source_set_item selected "
                "WHERE selected.matter_id=i.matter_id AND selected.document_id=i.document_id AND selected.source_set_id=?)"
                if source_set_id else ""
            )
            parameters = (matter_id, source_set_id) if source_set_id else (matter_id,)
            rows = self.connection.execute(
                "SELECT c.document_id,c.version_id,c.content_basis_digest,c.source_state,"
                "c.media_type,c.tone,c.kind,ij.state,mj.state FROM workbench_source_catalog c "
                "LEFT JOIN workbench_ingest_job ij ON ij.matter_id=c.matter_id AND ij.document_id=c.document_id "
                "LEFT JOIN workbench_media_job mj ON mj.matter_id=c.matter_id AND mj.document_id=c.document_id "
                "AND mj.source_version_id=c.version_id WHERE c.matter_id=?" + scope_catalog + " ORDER BY c.document_id",
                parameters,
            )
            for row in rows:
                digest.update(json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":")).encode("ascii"))
                digest.update(b"\n")
            digest.update(b"orphan-uploads\0")
            rows = self.connection.execute(
                "SELECT i.upload_item_id,i.upload_session_id,i.document_id,i.state,"
                "i.expected_size,i.received_size,s.state FROM workbench_upload_item i "
                "JOIN workbench_upload_session s ON s.matter_id=i.matter_id "
                "AND s.upload_session_id=i.upload_session_id "
                "LEFT JOIN workbench_source_catalog c ON c.matter_id=i.matter_id AND c.document_id=i.document_id "
                "WHERE i.matter_id=? AND s.state<>'cancelled' AND (i.document_id IS NULL OR c.document_id IS NULL) "
                + scope_upload + " ORDER BY i.upload_item_id", parameters,
            )
            for row in rows:
                digest.update(json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":")).encode("ascii"))
                digest.update(b"\n")
            if source_set_id:
                self.source_set(matter_id, source_set_id)
                digest.update(b"selected-source-set\0" + source_set_id.encode("ascii") + b"\0")
                for row in self.connection.execute(
                    "SELECT document_id FROM workbench_source_set_item "
                    "WHERE matter_id=? AND source_set_id=? ORDER BY document_id",
                    (matter_id, source_set_id),
                ):
                    digest.update(row["document_id"].encode("ascii") + b"\n")
        return digest.hexdigest()

    @staticmethod
    def _source_catalog_order(sort: str) -> str:
        return {
            "newest": "COALESCE(o.added_at,c.cataloged_at) DESC,c.document_id DESC",
            "oldest": "COALESCE(o.added_at,c.cataloged_at),c.document_id",
            "name": "c.display_name_key,c.document_id",
            "name_desc": "c.display_name_key DESC,c.document_id DESC",
            "status": (
                "CASE c.tone WHEN 'attention' THEN 0 WHEN 'processing' THEN 1 ELSE 2 END,"
                "c.display_name_key,c.document_id"
            ),
        }.get(sort, "COALESCE(o.added_at,c.cataloged_at) DESC,c.document_id DESC")

    def source_catalog_position(self, matter_id: str, document_id: str, *,
                                sort: str = "newest", **filters) -> int | None:
        """Find the current source in library order without loading the catalog."""
        predicate, parameters = self._source_catalog_filter(matter_id, **filters)
        with self._lock:
            self._active_matter_locked(matter_id)
            row = self.connection.execute(
                "SELECT position FROM (SELECT c.document_id, ROW_NUMBER() OVER (ORDER BY "
                + self._source_catalog_order(sort)
                + ") AS position FROM workbench_source_catalog c "
                "LEFT JOIN workbench_source_organization o "
                "ON o.matter_id=c.matter_id AND o.document_id=c.document_id "
                "LEFT JOIN workbench_source_collection sc "
                "ON sc.matter_id=o.matter_id AND sc.collection_id=o.collection_id "
                + predicate + ") ranked WHERE document_id=?",
                (*parameters, self._source_document_id(document_id)),
            ).fetchone()
        return int(row["position"]) if row is not None else None

    def source_catalog_page(
        self,
        matter_id: str,
        *,
        query_key: str = "",
        tone: str = "",
        actionable_only: bool = False,
        kind: str = "",
        review_state: str = "",
        collection_id: str = "",
        source_set_id: str = "",
        folder: str = "",
        same_content: str = "",
        matching_only: bool = False,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
    ) -> SourceCatalogPageRecord:
        """Return one bounded source page and database-native library counts."""

        predicate, parameters = self._source_catalog_filter(
            matter_id, query_key=query_key, tone=tone, actionable_only=actionable_only,
            kind=kind, review_state=review_state, collection_id=collection_id,
            source_set_id=source_set_id, folder=folder,
            same_content=same_content, matching_only=matching_only,
        )
        order = self._source_catalog_order(sort)
        base_join = (
            " FROM workbench_source_catalog c "
            "LEFT JOIN workbench_source_organization o "
            "ON o.matter_id=c.matter_id AND o.document_id=c.document_id "
            "LEFT JOIN workbench_source_collection sc "
            "ON sc.matter_id=o.matter_id AND sc.collection_id=o.collection_id "
        )
        limit_value = min(max(int(limit), 1), 100)
        offset_value = max(int(offset), 0)
        with self._lock:
            self._active_matter_locked(matter_id)
            stats_row = self.connection.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN c.tone='ready' THEN 1 ELSE 0 END) AS ready,"
                "SUM(CASE WHEN c.tone='processing' THEN 1 ELSE 0 END) AS processing,"
                "SUM(CASE WHEN c.tone='attention' THEN 1 ELSE 0 END) AS attention,"
                "SUM(CASE WHEN COALESCE(o.review_state,'unreviewed')='reviewed' THEN 1 ELSE 0 END) AS reviewed,"
                "SUM(CASE WHEN COALESCE(o.review_state,'unreviewed')='flagged' THEN 1 ELSE 0 END) AS flagged"
                + base_join
                + " WHERE c.matter_id=?",
                (matter_id,),
            ).fetchone()
            type_rows = self.connection.execute(
                "SELECT kind,COUNT(*) AS count FROM workbench_source_catalog "
                "WHERE matter_id=? GROUP BY kind",
                (matter_id,),
            ).fetchall()
            review_rows = self.connection.execute(
                "SELECT COALESCE(o.review_state,'unreviewed') AS state,COUNT(*) AS count"
                + base_join
                + " WHERE c.matter_id=? GROUP BY COALESCE(o.review_state,'unreviewed')",
                (matter_id,),
            ).fetchall()
            total_row = self.connection.execute(
                "SELECT COUNT(*) AS count" + base_join + predicate,
                tuple(parameters),
            ).fetchone()
            rows = self.connection.execute(
                "WITH selected_page AS MATERIALIZED (SELECT c.*,COALESCE(o.collection_id,'') AS collection_id,"
                "COALESCE(sc.name,'Unfiled') AS collection_name,"
                "COALESCE(o.review_state,'unreviewed') AS review_state,COALESCE(o.source_role,'unclassified') AS source_role,"
                "COALESCE(o.added_at,c.cataloged_at) AS added_at"
                + base_join
                + predicate
                + " ORDER BY "
                + order
                + " LIMIT ? OFFSET ?),"
                "current_bytes AS MATERIALIZED (SELECT document_id,source_sha256,byte_size "
                "FROM workbench_current_source_bytes WHERE matter_id=?),"
                "byte_counts AS MATERIALIZED (SELECT source_sha256,byte_size,COUNT(*) AS match_count "
                "FROM current_bytes GROUP BY source_sha256,byte_size) "
                "SELECT c.*,COALESCE(g.match_count,0) AS byte_match_count FROM selected_page c "
                "LEFT JOIN workbench_source_byte_match own "
                "ON own.matter_id=c.matter_id AND own.document_id=c.document_id "
                "AND own.document_id IN (SELECT document_id FROM current_bytes) "
                "LEFT JOIN byte_counts g ON g.source_sha256=own.source_sha256 AND g.byte_size=own.byte_size "
                "ORDER BY "
                + order.replace("COALESCE(o.added_at,c.cataloged_at)", "c.added_at"),
                (*parameters, limit_value, offset_value, matter_id),
            ).fetchall()
        stats = {
            key: int((stats_row[key] if stats_row is not None else 0) or 0)
            for key in ("total", "ready", "processing", "attention", "reviewed", "flagged")
        }
        type_counts = {str(row["kind"]): int(row["count"]) for row in type_rows}
        review_counts = {str(row["state"]): int(row["count"]) for row in review_rows}
        return SourceCatalogPageRecord(
            tuple(self._source_catalog(row) for row in rows),
            int(total_row["count"] if total_row is not None else 0),
            stats,
            type_counts,
            review_counts,
        )

    def matter_readiness(self, matter_id: str) -> MatterReadinessRecord:
        """Project bounded, content-free preparation state for one matter.

        Catalog state alone is intentionally insufficient: extraction can mark
        a document ready immediately before its search-index transaction.  The
        durable ingest/media job therefore wins until it reaches terminal
        success. Upload items without a committed catalog identity are folded
        in once, so a 10,000-file selection remains visible without enumerating
        it in Python.
        """

        with self._lock:
            self._active_matter_locked(matter_id)
            data_version = int(
                self.connection.execute("PRAGMA data_version").fetchone()[0]
            )
            cache_token = (self.connection.total_changes, data_version)
            cached = self._matter_readiness_cache.get(matter_id)
            if cached is not None and cached[0] == cache_token:
                return cached[1]
            row = self.connection.execute(
                "WITH catalog_base AS ("
                "SELECT c.document_id,c.kind,c.tone,c.updated_at AS catalog_updated_at,"
                "ij.state AS ingest_state,ij.stage AS ingest_stage,"
                "ij.updated_at AS ingest_updated_at,mj.state AS media_state,"
                "mj.updated_at AS media_updated_at "
                "FROM workbench_source_catalog c "
                "LEFT JOIN workbench_ingest_job ij ON ij.matter_id=c.matter_id "
                "AND ij.document_id=c.document_id "
                "LEFT JOIN workbench_media_job mj ON mj.matter_id=c.matter_id "
                "AND mj.document_id=c.document_id AND mj.source_version_id=c.version_id "
                "WHERE c.matter_id=:matter_id),"
                "catalog_effective AS ("
                "SELECT document_id,1 AS saved_count,"
                "CASE WHEN tone='ready' OR ingest_state='succeeded' "
                "OR media_state IN ('succeeded','degraded') "
                "OR (ingest_state='running' AND instr(lower(ingest_stage),'index')>0) "
                "THEN 1 ELSE 0 END AS extracted_count,"
                "CASE "
                "WHEN ingest_state IN ('queued','running') "
                "OR media_state IN ('queued','running') THEN 'processing' "
                "WHEN ingest_state IN ('failed','cancelled') "
                "OR media_state IN ('failed','cancelled') THEN 'attention' "
                "WHEN tone='attention' THEN 'attention' "
                "WHEN tone='processing' THEN 'processing' "
                "WHEN tone='ready' THEN 'ready' ELSE 'processing' END AS effective_state,"
                "CASE "
                "WHEN ingest_state IN ('queued','running') "
                "AND instr(lower(ingest_stage),'index')>0 THEN 'indexing' "
                "WHEN ingest_state IN ('queued','running') THEN 'extracting' "
                "WHEN media_state IN ('queued','running') THEN 'transcribing' "
                "WHEN tone='processing' AND kind IN ('AUDIO','VIDEO') THEN 'transcribing' "
                "WHEN tone='processing' THEN 'extracting' ELSE '' END AS activity,"
                "max(catalog_updated_at,COALESCE(ingest_updated_at,catalog_updated_at),"
                "COALESCE(media_updated_at,catalog_updated_at)) AS updated_at "
                "FROM catalog_base),"
                "orphan_upload_state AS ("
                "SELECT i.upload_item_id AS document_id,"
                "CASE WHEN i.received_size=i.expected_size "
                "AND i.state IN ('uploaded','queued') THEN 1 ELSE 0 END AS saved_count,"
                "0 AS extracted_count,"
                "CASE WHEN i.state IN ('failed','cancelled') OR (i.document_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM workbench_ingest_job ij "
                "WHERE ij.matter_id=i.matter_id AND ij.document_id=i.document_id "
                "AND ij.state IN ('queued','running')) "
                "AND NOT EXISTS (SELECT 1 FROM workbench_media_job mj "
                "WHERE mj.matter_id=i.matter_id AND mj.document_id=i.document_id "
                "AND mj.state IN ('queued','running'))) "
                "THEN 'attention' ELSE 'processing' END AS effective_state,"
                "i.updated_at "
                "FROM workbench_upload_item i "
                "JOIN workbench_upload_session s ON s.matter_id=i.matter_id "
                "AND s.upload_session_id=i.upload_session_id "
                "LEFT JOIN workbench_source_catalog c ON c.matter_id=i.matter_id "
                "AND c.document_id=i.document_id "
                "WHERE i.matter_id=:matter_id AND s.state<>'cancelled' "
                "AND (i.document_id IS NULL OR c.document_id IS NULL)),"
                "orphan_upload AS (SELECT document_id,saved_count,extracted_count,effective_state,"
                "CASE WHEN effective_state='attention' THEN '' ELSE 'uploading' END AS activity,"
                "updated_at FROM orphan_upload_state),"
                "combined AS (SELECT * FROM catalog_effective UNION ALL SELECT * FROM orphan_upload) "
                "SELECT COUNT(*) AS total_count,"
                "COALESCE(SUM(saved_count),0) AS saved_count,"
                "COALESCE(SUM(extracted_count),0) AS extracted_count,"
                "COALESCE(SUM(effective_state='ready'),0) AS searchable_count,"
                "COALESCE(SUM(effective_state='processing'),0) AS processing_count,"
                "COALESCE(SUM(effective_state='attention'),0) AS attention_count,"
                "COALESCE(SUM(activity='uploading'),0) AS uploading_count,"
                "COALESCE(SUM(activity='extracting'),0) AS extracting_count,"
                "COALESCE(SUM(activity='indexing'),0) AS indexing_count,"
                "COALESCE(SUM(activity='transcribing'),0) AS transcribing_count,"
                "(SELECT COUNT(*) FROM workbench_media_summary "
                "WHERE matter_id=:matter_id AND state IN ('queued','running','stale')) "
                "AS overview_processing_count,"
                "(SELECT COUNT(*) FROM workbench_media_summary "
                "WHERE matter_id=:matter_id AND state='failed') AS overview_attention_count,"
                "(SELECT COUNT(*) FROM workbench_source_catalog WHERE matter_id=:matter_id "
                "AND source_state='playback_only') AS playback_only_count,"
                "(SELECT COUNT(*) FROM workbench_source_catalog WHERE matter_id=:matter_id "
                "AND source_state='needs_review') AS recording_review_count,"
                "(SELECT COUNT(*) FROM workbench_source_catalog WHERE matter_id=:matter_id "
                "AND media_type='message/rfc822') AS email_count,"
                "(SELECT COUNT(*) FROM workbench_source_catalog WHERE matter_id=:matter_id "
                "AND media_type='application/pdf') AS pdf_count,"
                "COALESCE(MAX(updated_at),(SELECT updated_at FROM workbench_matter "
                "WHERE matter_id=:matter_id)) AS updated_at FROM combined",
                {"matter_id": matter_id},
            ).fetchone()
            if row is None:
                raise KeyError(matter_id)
            total = int(row["total_count"] or 0)
            searchable = int(row["searchable_count"] or 0)
            processing = int(row["processing_count"] or 0)
            attention = int(row["attention_count"] or 0)
            if total == 0:
                state = "empty"
            elif processing:
                state = "preparing"
            elif attention:
                state = "attention"
            elif searchable == total:
                state = "ready"
            else:
                state = "preparing"
            record = MatterReadinessRecord(
                matter_id=matter_id,
                state=state,
                total_count=total,
                saved_count=min(int(row["saved_count"] or 0), total),
                extracted_count=min(int(row["extracted_count"] or 0), total),
                searchable_count=searchable,
                processing_count=processing,
                attention_count=attention,
                uploading_count=int(row["uploading_count"] or 0),
                extracting_count=int(row["extracting_count"] or 0),
                indexing_count=int(row["indexing_count"] or 0),
                transcribing_count=int(row["transcribing_count"] or 0),
                overview_processing_count=int(row["overview_processing_count"] or 0),
                overview_attention_count=int(row["overview_attention_count"] or 0),
                progress_percent=round((searchable / total) * 100) if total else 0,
                updated_at=str(row["updated_at"] or self._now()),
                playback_only_count=int(row["playback_only_count"] or 0),
                recording_review_count=int(row["recording_review_count"] or 0),
                email_count=int(row["email_count"] or 0),
                pdf_count=int(row["pdf_count"] or 0),
            )
            self._matter_readiness_cache[matter_id] = (cache_token, record)
            return record

    def source_catalog_record(
        self, matter_id: str, document_id: str
    ) -> SourceCatalogRecord:
        """Resolve one current catalog source without rebuilding the manifest."""

        value = self._source_document_id(document_id)
        with self._lock:
            self._active_matter_locked(matter_id)
            row = self.connection.execute(
                "SELECT c.*,COALESCE(o.collection_id,'') AS collection_id,"
                "COALESCE(sc.name,'Unfiled') AS collection_name,"
                "COALESCE(o.review_state,'unreviewed') AS review_state,COALESCE(o.source_role,'unclassified') AS source_role,"
                "COALESCE(o.added_at,c.cataloged_at) AS added_at "
                "FROM workbench_source_catalog c "
                "LEFT JOIN workbench_source_organization o "
                "ON o.matter_id=c.matter_id AND o.document_id=c.document_id "
                "LEFT JOIN workbench_source_collection sc "
                "ON sc.matter_id=o.matter_id AND sc.collection_id=o.collection_id "
                "WHERE c.matter_id=? AND c.document_id=?",
                (matter_id, value),
            ).fetchone()
        if row is None:
            raise KeyError(document_id)
        return self._source_catalog(row)

    def source_catalog_neighbor(
        self, matter_id: str, document_id: str, direction: str
    ) -> SourceCatalogRecord | None:
        """Return one adjacent source in stable oldest-first review order."""

        value = self._source_document_id(document_id)
        if direction not in {"previous", "next"}:
            raise ValueError("invalid source neighbor direction")
        with self._lock:
            self._active_matter_locked(matter_id)
            current = self.connection.execute(
                "SELECT COALESCE(o.added_at,c.cataloged_at) AS added_at "
                "FROM workbench_source_catalog c "
                "LEFT JOIN workbench_source_organization o "
                "ON o.matter_id=c.matter_id AND o.document_id=c.document_id "
                "WHERE c.matter_id=? AND c.document_id=?",
                (matter_id, value),
            ).fetchone()
            if current is None:
                raise KeyError(document_id)
            comparison = "<" if direction == "previous" else ">"
            order = "DESC" if direction == "previous" else "ASC"
            row = self.connection.execute(
                "SELECT c.*,COALESCE(o.collection_id,'') AS collection_id,"
                "COALESCE(sc.name,'Unfiled') AS collection_name,"
                "COALESCE(o.review_state,'unreviewed') AS review_state,COALESCE(o.source_role,'unclassified') AS source_role,"
                "COALESCE(o.added_at,c.cataloged_at) AS added_at "
                "FROM workbench_source_catalog c "
                "LEFT JOIN workbench_source_organization o "
                "ON o.matter_id=c.matter_id AND o.document_id=c.document_id "
                "LEFT JOIN workbench_source_collection sc "
                "ON sc.matter_id=o.matter_id AND sc.collection_id=o.collection_id "
                "WHERE c.matter_id=? AND (COALESCE(o.added_at,c.cataloged_at) "
                + comparison
                + " ? OR (COALESCE(o.added_at,c.cataloged_at)=? AND c.document_id "
                + comparison
                + " ?)) ORDER BY COALESCE(o.added_at,c.cataloged_at) "
                + order
                + ",c.document_id "
                + order
                + " LIMIT 1",
                (matter_id, current["added_at"], current["added_at"], value),
            ).fetchone()
        return self._source_catalog(row) if row is not None else None

    def reconcile_source_organizations(
        self,
        matter_id: str,
        sources: Sequence[tuple[str, str, str]],
    ) -> int:
        """Attach manifest sources missing A8 metadata without changing source bytes."""

        prepared: list[tuple[str, str, str]] = []
        for document_id, origin, relative_path in sources:
            self._source_document_id(document_id)
            if origin not in {"upload", "registered"}:
                raise ValueError("invalid source origin")
            value = self._safe_text(
                relative_path,
                label="Source relative path",
                maximum=2_048,
            )
            prepared.append((document_id, origin, value))
        now = self._now()
        inserted = 0
        with self._lock, self.connection:
            matter = self._active_matter_locked(matter_id)
            collection_by_origin: dict[str, SourceCollectionRecord] = {}
            for document_id, origin, relative_path in prepared:
                exists = self.connection.execute(
                    "SELECT 1 FROM workbench_source_organization "
                    "WHERE matter_id=? AND document_id=?",
                    (matter_id, document_id),
                ).fetchone()
                if exists is not None:
                    continue
                collection = collection_by_origin.get(origin)
                if collection is None:
                    requested = (
                        "Existing uploads" if origin == "upload" else "Existing registered sources"
                    )
                    row = self.connection.execute(
                        "SELECT collection_id,matter_id,name,kind,created_by,created_at,updated_at,"
                        "0 AS source_count FROM workbench_source_collection "
                        "WHERE matter_id=? AND name_key=?",
                        (matter_id, requested.casefold()),
                    ).fetchone()
                    collection = (
                        self._source_collection(row)
                        if row is not None
                        else self._create_source_collection_locked(
                            matter_id,
                            requested,
                            "migrated",
                            matter["owner_id"],
                            now,
                        )
                    )
                    collection_by_origin[origin] = collection
                self.connection.execute(
                    "INSERT INTO workbench_source_organization("
                    "matter_id,document_id,collection_id,relative_path,review_state,"
                    "added_by,added_at,updated_by,updated_at) "
                    "VALUES (?,?,?,?,'unreviewed',?,?,?,?)",
                    (
                        matter_id,
                        document_id,
                        collection.collection_id,
                        relative_path,
                        matter["owner_id"],
                        now,
                        matter["owner_id"],
                        now,
                    ),
                )
                inserted += 1
            if inserted:
                self.connection.execute(
                    "UPDATE workbench_source_collection SET updated_at=? WHERE matter_id=?",
                    (now, matter_id),
                )
        return inserted

    def source_organizations(
        self, matter_id: str
    ) -> tuple[SourceOrganizationRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT matter_id,document_id,collection_id,relative_path,review_state,"
                "added_by,added_at,updated_by,updated_at,source_role "
                "FROM workbench_source_organization WHERE matter_id=?",
                (matter_id,),
            ).fetchall()
        return tuple(self._source_organization(row) for row in rows)

    def source_collections(self, matter_id: str) -> tuple[SourceCollectionRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT c.collection_id,c.matter_id,c.name,c.kind,c.created_by,c.created_at,"
                "c.updated_at,COUNT(o.document_id) AS source_count "
                "FROM workbench_source_collection c "
                "LEFT JOIN workbench_source_organization o "
                "ON o.collection_id=c.collection_id AND o.matter_id=c.matter_id "
                "WHERE c.matter_id=? GROUP BY c.collection_id "
                "ORDER BY c.updated_at DESC,c.collection_id",
                (matter_id,),
            ).fetchall()
        return tuple(self._source_collection(row) for row in rows)

    def source_collection(self, matter_id: str, collection_id: str) -> SourceCollectionRecord:
        if not _SOURCE_COLLECTION.fullmatch(collection_id or ""):
            raise KeyError(collection_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT c.collection_id,c.matter_id,c.name,c.kind,c.created_by,c.created_at,"
                "c.updated_at,COUNT(o.document_id) AS source_count "
                "FROM workbench_source_collection c "
                "LEFT JOIN workbench_source_organization o "
                "ON o.collection_id=c.collection_id AND o.matter_id=c.matter_id "
                "WHERE c.matter_id=? AND c.collection_id=? GROUP BY c.collection_id",
                (matter_id, collection_id),
            ).fetchone()
        if row is None:
            raise KeyError(collection_id)
        return self._source_collection(row)

    def rename_source_collection(
        self, matter_id: str, collection_id: str, name: str, actor_id: str
    ) -> SourceCollectionRecord:
        current = self.source_collection(matter_id, collection_id)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        value = self._safe_text(name, label="Collection name", maximum=160)
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            collision = self.connection.execute(
                "SELECT 1 FROM workbench_source_collection WHERE matter_id=? "
                "AND name_key=? AND collection_id<>?",
                (matter_id, value.casefold(), collection_id),
            ).fetchone()
            if collision is not None:
                raise WorkspaceProblem("Another collection already uses that name.")
            self.connection.execute(
                "UPDATE workbench_source_collection SET name=?,name_key=?,updated_at=? "
                "WHERE matter_id=? AND collection_id=?",
                (value, value.casefold(), now, matter_id, collection_id),
            )
        return SourceCollectionRecord(
            current.collection_id,
            current.matter_id,
            value,
            current.kind,
            current.created_by,
            current.created_at,
            now,
            current.source_count,
        )

    def register_source_organizations(
        self,
        matter_id: str,
        collection_id: str,
        sources: Sequence[tuple[str, str]],
        actor_id: str,
    ) -> int:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        prepared = [
            (
                self._source_document_id(document_id),
                self._safe_text(relative_path, label="Source relative path", maximum=2_048),
            )
            for document_id, relative_path in sources
        ]
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            self.source_collection(matter_id, collection_id)
            for document_id, relative_path in prepared:
                self.connection.execute(
                    "INSERT INTO workbench_source_organization("
                    "matter_id,document_id,collection_id,relative_path,review_state,"
                    "added_by,added_at,updated_by,updated_at) "
                    "VALUES (?,?,?,?,'unreviewed',?,?,?,?) "
                    "ON CONFLICT(matter_id,document_id) DO UPDATE SET "
                    "relative_path=excluded.relative_path,updated_by=excluded.updated_by,"
                    "updated_at=excluded.updated_at",
                    (
                        matter_id,
                        document_id,
                        collection_id,
                        relative_path,
                        actor,
                        now,
                        actor,
                        now,
                    ),
                )
            self.connection.execute(
                "UPDATE workbench_source_collection SET updated_at=? "
                "WHERE matter_id=? AND collection_id=?",
                (now, matter_id, collection_id),
            )
        return len(prepared)

    def _require_sources_locked(
        self, matter_id: str, document_ids: Sequence[str]
    ) -> tuple[str, ...]:
        values = tuple(dict.fromkeys(self._source_document_id(value) for value in document_ids))
        if not values:
            raise WorkspaceProblem("Select at least one source.")
        rows = self.connection.execute(
            "SELECT document_id FROM workbench_source_organization WHERE matter_id=? "
            "AND document_id IN (" + ",".join("?" for _ in values) + ")",
            (matter_id, *values),
        ).fetchall()
        if {row["document_id"] for row in rows} != set(values):
            raise KeyError("source")
        return values

    def update_source_review_state(
        self, matter_id: str, document_ids: Sequence[str], state: str, actor_id: str
    ) -> int:
        if state not in {"unreviewed", "reviewed", "flagged"}:
            raise WorkspaceProblem("Choose a valid review state.")
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            values = self._require_sources_locked(matter_id, document_ids)
            changed = self.connection.execute(
                "UPDATE workbench_source_organization SET review_state=?,updated_by=?,updated_at=? "
                "WHERE matter_id=? AND document_id IN ("
                + ",".join("?" for _ in values)
                + ")",
                (state, actor, now, matter_id, *values),
            ).rowcount
        return changed

    def move_sources_to_collection(
        self,
        matter_id: str,
        document_ids: Sequence[str],
        collection_id: str,
        actor_id: str,
    ) -> int:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            self.source_collection(matter_id, collection_id)
            values = self._require_sources_locked(matter_id, document_ids)
            changed = self.connection.execute(
                "UPDATE workbench_source_organization SET collection_id=?,updated_by=?,updated_at=? "
                "WHERE matter_id=? AND document_id IN ("
                + ",".join("?" for _ in values)
                + ")",
                (collection_id, actor, now, matter_id, *values),
            ).rowcount
            self.connection.execute(
                "UPDATE workbench_source_collection SET updated_at=? "
                "WHERE matter_id=? AND collection_id=?",
                (now, matter_id, collection_id),
            )
        return changed

    def create_source_set(
        self, matter_id: str, name: str, document_ids: Sequence[str], actor_id: str
    ) -> SourceSetRecord:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            values = self._require_sources_locked(matter_id, document_ids)
            final_name, name_key = self._unique_library_name_locked(
                "workbench_source_set", matter_id, name
            )
            source_set_id = f"source-set-{uuid.uuid4().hex}"
            self.connection.execute(
                "INSERT INTO workbench_source_set("
                "source_set_id,matter_id,name,name_key,created_by,created_at,updated_by,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (source_set_id, matter_id, final_name, name_key, actor, now, actor, now),
            )
            self.connection.executemany(
                "INSERT INTO workbench_source_set_item("
                "source_set_id,matter_id,document_id,added_by,added_at) VALUES (?,?,?,?,?)",
                [(source_set_id, matter_id, value, actor, now) for value in values],
            )
        return SourceSetRecord(
            source_set_id, matter_id, final_name, actor, now, actor, now, len(values)
        )

    def singleton_source_set(
        self, matter_id: str, document_id: str, name: str, actor_id: str
    ) -> SourceSetRecord:
        """Return a reusable one-source scope, creating it only when needed."""

        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        value = self._source_document_id(document_id)
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            self._require_sources_locked(matter_id, (value,))
            row = self.connection.execute(
                "SELECT s.source_set_id,s.matter_id,s.name,s.created_by,s.created_at,"
                "s.updated_by,s.updated_at,1 AS source_count "
                "FROM workbench_source_set s "
                "JOIN workbench_source_set_item target "
                "ON target.source_set_id=s.source_set_id AND target.matter_id=s.matter_id "
                "WHERE s.matter_id=? AND target.document_id=? "
                "AND NOT EXISTS (SELECT 1 FROM workbench_source_set_item other "
                "WHERE other.source_set_id=s.source_set_id "
                "AND other.document_id<>target.document_id) "
                "ORDER BY s.updated_at DESC,s.source_set_id LIMIT 1",
                (matter_id, value),
            ).fetchone()
            if row is not None:
                return self._source_set(row)
        return self.create_source_set(matter_id, name, (value,), actor)

    def add_sources_to_set(
        self,
        matter_id: str,
        source_set_id: str,
        document_ids: Sequence[str],
        actor_id: str,
    ) -> int:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            self.source_set(matter_id, source_set_id)
            values = self._require_sources_locked(matter_id, document_ids)
            before = self.connection.total_changes
            self.connection.executemany(
                "INSERT OR IGNORE INTO workbench_source_set_item("
                "source_set_id,matter_id,document_id,added_by,added_at) VALUES (?,?,?,?,?)",
                [(source_set_id, matter_id, value, actor, now) for value in values],
            )
            changed = self.connection.total_changes - before
            self.connection.execute(
                "UPDATE workbench_source_set SET updated_by=?,updated_at=? "
                "WHERE matter_id=? AND source_set_id=?",
                (actor, now, matter_id, source_set_id),
            )
        return changed

    def source_sets(self, matter_id: str) -> tuple[SourceSetRecord, ...]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT s.source_set_id,s.matter_id,s.name,s.created_by,s.created_at,"
                "s.updated_by,s.updated_at,COUNT(i.document_id) AS source_count "
                "FROM workbench_source_set s LEFT JOIN workbench_source_set_item i "
                "ON i.source_set_id=s.source_set_id AND i.matter_id=s.matter_id "
                "WHERE s.matter_id=? GROUP BY s.source_set_id "
                "ORDER BY s.updated_at DESC,s.source_set_id",
                (matter_id,),
            ).fetchall()
        return tuple(self._source_set(row) for row in rows)

    def source_set(self, matter_id: str, source_set_id: str) -> SourceSetRecord:
        if not _SOURCE_SET.fullmatch(source_set_id or ""):
            raise KeyError(source_set_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT s.source_set_id,s.matter_id,s.name,s.created_by,s.created_at,"
                "s.updated_by,s.updated_at,COUNT(i.document_id) AS source_count "
                "FROM workbench_source_set s LEFT JOIN workbench_source_set_item i "
                "ON i.source_set_id=s.source_set_id AND i.matter_id=s.matter_id "
                "WHERE s.matter_id=? AND s.source_set_id=? GROUP BY s.source_set_id",
                (matter_id, source_set_id),
            ).fetchone()
        if row is None:
            raise KeyError(source_set_id)
        return self._source_set(row)

    def source_set_document_ids(self, matter_id: str, source_set_id: str) -> frozenset[str]:
        self.source_set(matter_id, source_set_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT document_id FROM workbench_source_set_item "
                "WHERE matter_id=? AND source_set_id=? ORDER BY document_id",
                (matter_id, source_set_id),
            ).fetchall()
        return frozenset(row["document_id"] for row in rows)

    def remove_source_organization(self, matter_id: str, document_id: str) -> None:
        value = self._source_document_id(document_id)
        with self._lock, self.connection:
            self.connection.execute(
                "DELETE FROM workbench_source_set_item WHERE matter_id=? AND document_id=?",
                (matter_id, value),
            )
            self.connection.execute(
                "DELETE FROM workbench_source_organization WHERE matter_id=? AND document_id=?",
                (matter_id, value),
            )
            self.connection.execute(
                "DELETE FROM workbench_matter_activity WHERE matter_id=? "
                "AND activity_kind='source' AND object_id=?",
                (matter_id, value),
            )

    def create_upload_session(
        self,
        matter_id: str,
        actor_id: str,
        collection_name: str,
        files: Sequence[Mapping[str, object]],
        *,
        collection_id: str = "",
        intake_receipt_id: str = "",
        intake_ordinals: Sequence[int] = (),
    ) -> tuple[UploadSessionRecord, tuple[UploadItemRecord, ...]]:
        if not files:
            raise WorkspaceProblem("Choose at least one supported file.")
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        prepared: list[tuple[str, str, str, int]] = []
        seen: set[str] = set()
        total_bytes = 0
        for item in files:
            display_name = self._safe_text(
                str(item.get("display_name", "")), label="Source name", maximum=240
            )
            relative_path = self._safe_text(
                str(item.get("relative_path", "")),
                label="Source relative path",
                maximum=2_048,
            )
            media_type = self._safe_text(
                str(item.get("media_type", "")), label="Source type", maximum=120
            )
            size_value = item.get("expected_size")
            if isinstance(size_value, bool) or not isinstance(size_value, int) or size_value <= 0:
                raise WorkspaceProblem("Every selected source must have a valid size.")
            key = relative_path.casefold()
            if key in seen:
                raise WorkspaceProblem("The selected files contain the same relative path twice.")
            seen.add(key)
            total_bytes += size_value
            prepared.append((display_name, relative_path, media_type, size_value))
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor)
            if intake_receipt_id:
                from .intake_receipts import IntakeReceipts

                receipts = IntakeReceipts(self)
                existing_session = receipts.validate_upload_locked(
                    matter_id, actor, intake_receipt_id, intake_ordinals, files
                )
                if existing_session:
                    return self.upload_session(matter_id, actor, existing_session)
            elif intake_ordinals:
                raise WorkspaceProblem("The upload receipt is missing. Review the selection again.")
            if collection_id:
                if not _SOURCE_COLLECTION.fullmatch(collection_id):
                    raise WorkspaceProblem("The upload collection is invalid.")
                collection_row = self.connection.execute(
                    "SELECT collection_id,kind,created_by FROM workbench_source_collection "
                    "WHERE matter_id=? AND collection_id=?",
                    (matter_id, collection_id),
                ).fetchone()
                if (
                    collection_row is None
                    or collection_row["kind"] != "upload"
                    or collection_row["created_by"] != actor
                ):
                    raise WorkspaceProblem(
                        "This upload collection cannot accept another batch."
                    )
                existing_paths = {
                    row[0].casefold()
                    for row in self.connection.execute(
                        "SELECT i.relative_path FROM workbench_upload_item i "
                        "JOIN workbench_upload_session s "
                        "ON s.upload_session_id=i.upload_session_id "
                        "WHERE s.matter_id=? AND s.collection_id=? "
                        "AND i.state<>'cancelled'",
                        (matter_id, collection_id),
                    ).fetchall()
                }
                if existing_paths & seen:
                    raise WorkspaceProblem(
                        "The selected files repeat a relative path already in this upload collection."
                    )
                self.connection.execute(
                    "UPDATE workbench_source_collection SET updated_at=? "
                    "WHERE matter_id=? AND collection_id=?",
                    (now, matter_id, collection_id),
                )
                selected_collection_id = collection_id
            else:
                collection = self._create_source_collection_locked(
                    matter_id, collection_name, "upload", actor, now
                )
                selected_collection_id = collection.collection_id
            session_id = f"upload-session-{uuid.uuid4().hex}"
            self.connection.execute(
                "INSERT INTO workbench_upload_session("
                "upload_session_id,matter_id,collection_id,actor_id,state,item_count,"
                "total_bytes,created_at,updated_at) VALUES (?,?,?,?,'open',?,?,?,?)",
                (
                    session_id,
                    matter_id,
                    selected_collection_id,
                    actor,
                    len(prepared),
                    total_bytes,
                    now,
                    now,
                ),
            )
            item_ids: list[str] = []
            for ordinal, (display_name, relative_path, media_type, expected_size) in enumerate(
                prepared, 1
            ):
                item_id = f"upload-item-{uuid.uuid4().hex}"
                item_ids.append(item_id)
                self.connection.execute(
                    "INSERT INTO workbench_upload_item("
                    "upload_item_id,upload_session_id,matter_id,ordinal,display_name,"
                    "relative_path,media_type,expected_size,received_size,state,document_id,"
                    "message,updated_at) VALUES (?,?,?,?,?,?,?,?,0,'pending',NULL,'',?)",
                    (
                        item_id,
                        session_id,
                        matter_id,
                        ordinal,
                        display_name,
                        relative_path,
                        media_type,
                        expected_size,
                        now,
                    ),
                )
            session_row = self.connection.execute(
                "SELECT upload_session_id,matter_id,collection_id,actor_id,state,item_count,"
                "total_bytes,created_at,updated_at FROM workbench_upload_session "
                "WHERE upload_session_id=?",
                (session_id,),
            ).fetchone()
            if intake_receipt_id:
                receipts.bind_upload_locked(matter_id, intake_receipt_id, intake_ordinals, item_ids)
            item_rows = self.connection.execute(
                "SELECT upload_item_id,upload_session_id,matter_id,ordinal,display_name,"
                "relative_path,media_type,expected_size,received_size,state,document_id,"
                "message,updated_at FROM workbench_upload_item "
                "WHERE upload_session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        return self._upload_session(session_row), tuple(
            self._upload_item(row) for row in item_rows
        )

    def upload_session(
        self, matter_id: str, actor_id: str, session_id: str
    ) -> tuple[UploadSessionRecord, tuple[UploadItemRecord, ...]]:
        if not _UPLOAD_SESSION.fullmatch(session_id or ""):
            raise KeyError(session_id)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        with self._lock:
            row = self.connection.execute(
                "SELECT upload_session_id,matter_id,collection_id,actor_id,state,item_count,"
                "total_bytes,created_at,updated_at FROM workbench_upload_session "
                "WHERE upload_session_id=? AND matter_id=? AND actor_id=?",
                (session_id, matter_id, actor),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            items = self.connection.execute(
                "SELECT upload_item_id,upload_session_id,matter_id,ordinal,display_name,"
                "relative_path,media_type,expected_size,received_size,state,document_id,"
                "message,updated_at FROM workbench_upload_item "
                "WHERE upload_session_id=? AND matter_id=? ORDER BY ordinal",
                (session_id, matter_id),
            ).fetchall()
        return self._upload_session(row), tuple(self._upload_item(item) for item in items)

    def upload_session_record(
        self, matter_id: str, actor_id: str, session_id: str
    ) -> UploadSessionRecord:
        """Return session metadata without materializing every upload item."""

        if not _UPLOAD_SESSION.fullmatch(session_id or ""):
            raise KeyError(session_id)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        with self._lock:
            row = self.connection.execute(
                "SELECT upload_session_id,matter_id,collection_id,actor_id,state,item_count,"
                "total_bytes,created_at,updated_at FROM workbench_upload_session "
                "WHERE upload_session_id=? AND matter_id=? AND actor_id=?",
                (session_id, matter_id, actor),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self._upload_session(row)

    def upload_session_summary(
        self, matter_id: str, actor_id: str, session_id: str
    ) -> Mapping[str, int | bool]:
        """Compute bounded upload/work counts in one database query."""

        session = self.upload_session_record(matter_id, actor_id, session_id)
        with self._lock:
            row = self.connection.execute(
                "WITH projected AS (SELECT i.received_size,i.state,i.media_type,"
                "CASE "
                "WHEN i.state IN ('failed','cancelled') THEN 'attention' "
                "WHEN i.document_id IS NULL THEN i.state "
                "WHEN c.source_state='ready' THEN 'ready' "
                "WHEN c.source_state='playback_only' THEN 'playback_only' "
                "WHEN c.source_state IN ('failed','needs_ocr','changed','missing','needs_review') "
                "THEN 'attention' "
                "WHEN mj.state IN ('succeeded','degraded') THEN 'ready' "
                "WHEN mj.state='failed' THEN 'attention' "
                "WHEN mj.state IN ('queued','running') THEN 'processing' "
                "WHEN ij.state='succeeded' THEN 'ready' "
                "WHEN ij.state='failed' THEN 'attention' "
                "WHEN ij.state IN ('queued','running') THEN 'processing' "
                "ELSE 'processing' END AS work_state "
                "FROM workbench_upload_item i "
                "LEFT JOIN workbench_source_catalog c ON c.matter_id=i.matter_id "
                "AND c.document_id=i.document_id "
                "LEFT JOIN workbench_ingest_job ij ON ij.matter_id=i.matter_id "
                "AND ij.document_id=i.document_id "
                "LEFT JOIN workbench_media_job mj ON mj.matter_id=i.matter_id "
                "AND mj.document_id=i.document_id AND mj.source_version_id=c.version_id "
                "WHERE i.matter_id=? AND i.upload_session_id=?) "
                "SELECT COUNT(*) AS item_count,COALESCE(SUM(received_size),0) AS received_bytes,"
                "COALESCE(SUM(state='queued'),0) AS queued_count,"
                "COALESCE(SUM(state IN ('failed','cancelled')),0) AS failed_count,"
                "COALESCE(SUM(work_state='processing'),0) AS processing_count,"
                "COALESCE(SUM(work_state='ready'),0) AS ready_count,"
                "COALESCE(SUM(work_state='playback_only'),0) AS playback_only_count,"
                "COALESCE(SUM(work_state='attention'),0) AS attention_count,"
                "COALESCE(SUM(media_type LIKE 'audio/%' OR media_type LIKE 'video/%'),0) "
                "AS media_count,"
                "COALESCE(SUM(state IN ('queued','failed','cancelled')),0) AS terminal_count "
                "FROM projected",
                (matter_id, session_id),
            ).fetchone()
        item_count = int(row["item_count"])
        processing_count = int(row["processing_count"])
        return {
            "received_bytes": int(row["received_bytes"]),
            "queued_count": int(row["queued_count"]),
            "failed_count": int(row["failed_count"]),
            "processing_count": processing_count,
            "ready_count": int(row["ready_count"]),
            "playback_only_count": int(row["playback_only_count"]),
            "attention_count": int(row["attention_count"]),
            "contains_media": bool(row["media_count"]),
            "review_ready": item_count > 0 and int(row["terminal_count"]) == item_count,
            "work_complete": processing_count == 0
            and session.state in {"complete", "partial"},
        }

    def recent_upload_sessions(
        self, matter_id: str, actor_id: str, *, limit: int = 5
    ) -> tuple[UploadSessionRecord, ...]:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        bounded = min(max(int(limit), 1), 20)
        with self._lock:
            rows = self.connection.execute(
                "SELECT upload_session_id,matter_id,collection_id,actor_id,state,item_count,"
                "total_bytes,created_at,updated_at FROM workbench_upload_session "
                "WHERE matter_id=? AND actor_id=? ORDER BY updated_at DESC LIMIT ?",
                (matter_id, actor, bounded),
            ).fetchall()
        return tuple(self._upload_session(row) for row in rows)

    def abandoned_upload_sessions(
        self, *, maximum_age_hours: int = 24
    ) -> tuple[UploadSessionRecord, ...]:
        if (
            isinstance(maximum_age_hours, bool)
            or not isinstance(maximum_age_hours, int)
            or not 1 <= maximum_age_hours <= 24 * 30
        ):
            raise ValueError("invalid abandoned upload age")
        cutoff = self._timestamp(
            self.current_time() - timedelta(hours=maximum_age_hours)
        )
        with self._lock:
            rows = self.connection.execute(
                "SELECT s.upload_session_id,s.matter_id,s.collection_id,s.actor_id,"
                "s.state,s.item_count,s.total_bytes,s.created_at,s.updated_at "
                "FROM workbench_upload_session s "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=s.matter_id "
                "WHERE ml.state='active' AND s.state='open' AND s.updated_at<=? "
                "AND EXISTS (SELECT 1 FROM workbench_upload_item i "
                "WHERE i.upload_session_id=s.upload_session_id "
                "AND i.state IN ('pending','uploading','uploaded')) "
                "ORDER BY s.updated_at,s.upload_session_id",
                (cutoff,),
            ).fetchall()
        return tuple(self._upload_session(row) for row in rows)

    def pending_upload_bytes(self, matter_id: str | None = None) -> int:
        """Return source bytes reserved but not yet present on managed storage."""

        where = "state IN ('pending','uploading','uploaded')"
        parameters: tuple[object, ...] = ()
        if matter_id is not None:
            where += " AND matter_id=?"
            parameters = (matter_id,)
        with self._lock:
            row = self.connection.execute(
                "SELECT COALESCE(SUM(expected_size-received_size),0) "
                f"FROM workbench_upload_item WHERE {where}",
                parameters,
            ).fetchone()
        return max(int(row[0]), 0)

    def upload_item(
        self, matter_id: str, actor_id: str, session_id: str, item_id: str
    ) -> UploadItemRecord:
        if not _UPLOAD_ITEM.fullmatch(item_id or ""):
            raise KeyError(item_id)
        self.upload_session(matter_id, actor_id, session_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT upload_item_id,upload_session_id,matter_id,ordinal,display_name,"
                "relative_path,media_type,expected_size,received_size,state,document_id,"
                "message,updated_at FROM workbench_upload_item WHERE upload_item_id=? "
                "AND upload_session_id=? AND matter_id=?",
                (item_id, session_id, matter_id),
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return self._upload_item(row)

    def set_upload_item_offset(
        self,
        matter_id: str,
        actor_id: str,
        session_id: str,
        item_id: str,
        previous_size: int,
        received_size: int,
    ) -> UploadItemRecord:
        item = self.upload_item(matter_id, actor_id, session_id, item_id)
        if (
            previous_size != item.received_size
            or received_size < previous_size
            or received_size > item.expected_size
        ):
            raise WorkspaceProblem("The upload offset changed. Refresh its saved status and resume.")
        state = "uploaded" if received_size == item.expected_size else "uploading"
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_upload_item SET received_size=?,state=?,message='',updated_at=? "
                "WHERE upload_item_id=? AND upload_session_id=? AND matter_id=? "
                "AND received_size=? AND state IN ('pending','uploading','uploaded')",
                (
                    received_size,
                    state,
                    now,
                    item_id,
                    session_id,
                    matter_id,
                    previous_size,
                ),
            ).rowcount
            if changed != 1:
                raise WorkspaceProblem("The upload state changed. Refresh its saved status and resume.")
            self.connection.execute(
                "UPDATE workbench_upload_session SET updated_at=? "
                "WHERE upload_session_id=? AND state='open'",
                (now, session_id),
            )
        return self.upload_item(matter_id, actor_id, session_id, item_id)

    def finish_upload_item(
        self,
        matter_id: str,
        actor_id: str,
        session_id: str,
        item_id: str,
        document_id: str,
        *,
        queue_ingestion: bool = True,
        source_version_id: str = "",
        media_details: Mapping[str, object] | None = None,
        maximum_media_bytes: int = 5 * 1024 * 1024 * 1024,
    ) -> tuple[UploadItemRecord, IngestJobRecord | None]:
        document = self._source_document_id(document_id)
        item = self.upload_item(matter_id, actor_id, session_id, item_id)
        if item.state == "queued":
            return item, self.ingest_job(matter_id, document)
        if item.state != "uploaded" or item.received_size != item.expected_size:
            raise WorkspaceProblem("Finish uploading this source before it is queued.")
        session, _ = self.upload_session(matter_id, actor_id, session_id)
        now = self._now()
        job_id = f"ingest-job-{uuid.uuid4().hex}"
        media_values: tuple[str, str, int, str, int] | None = None
        if media_details is not None:
            if queue_ingestion:
                raise ValueError("media upload cannot enter document ingestion")
            version = self._safe_text(
                str(media_details.get("source_version_id") or ""),
                label="Source version",
                maximum=100,
            )
            digest = str(media_details.get("source_sha256") or "")
            size = int(media_details.get("byte_size") or 0)
            media_type = self._safe_text(
                str(media_details.get("media_type") or ""),
                label="Media type",
                maximum=100,
            )
            duration_ms = int(media_details.get("duration_ms") or 0)
            if (
                not re.fullmatch(r"[0-9a-f]{32}", version)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or size != item.expected_size
                or not 0 < size <= int(maximum_media_bytes)
                or not 0 < duration_ms <= 12 * 60 * 60 * 1000
            ):
                raise ValueError("invalid media upload metadata")
            media_values = (version, digest, size, media_type, duration_ms)
        with self._lock, self.connection:
            linked_receipt = self.connection.execute(
                "SELECT source_version_id FROM workbench_intake_transfer WHERE matter_id=? AND upload_item_id=?",
                (matter_id, item_id),
            ).fetchone()
            if linked_receipt is not None:
                if not re.fullmatch(r"[0-9a-f]{32}", source_version_id):
                    raise WorkspaceProblem("The received source version is unavailable. Retry finalizing this upload.")
                if linked_receipt["source_version_id"] not in {"", source_version_id}:
                    raise WorkspaceProblem("The received source version changed. Review the upload again.")
                self.connection.execute(
                    "UPDATE workbench_intake_transfer SET source_version_id=? WHERE matter_id=? AND upload_item_id=?",
                    (source_version_id, matter_id, item_id),
                )
            self.connection.execute(
                "INSERT INTO workbench_source_organization("
                "matter_id,document_id,collection_id,relative_path,review_state,"
                "added_by,added_at,updated_by,updated_at) "
                "VALUES (?,?,?,?,'unreviewed',?,?,?,?) "
                "ON CONFLICT(matter_id,document_id) DO UPDATE SET "
                "relative_path=excluded.relative_path,updated_by=excluded.updated_by,"
                "updated_at=excluded.updated_at",
                (
                    matter_id,
                    document,
                    session.collection_id,
                    item.relative_path,
                    actor_id,
                    now,
                    actor_id,
                    now,
                ),
            )
            if queue_ingestion:
                self.connection.execute(
                    "INSERT OR IGNORE INTO workbench_ingest_job("
                    "job_id,matter_id,document_id,state,stage,created_at,updated_at) "
                    "VALUES (?,?,?,'queued','Queued',?,?)",
                    (job_id, matter_id, document, now, now),
                )
            if media_values is not None:
                version, digest, size, media_type, duration_ms = media_values
                self.connection.execute(
                    "INSERT OR IGNORE INTO workbench_media_job("
                    "media_job_id,matter_id,document_id,source_version_id,requested_by,"
                    "source_sha256,byte_size,media_type,duration_ms,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"media-job-{uuid.uuid4().hex}",
                        matter_id,
                        document,
                        version,
                        actor_id,
                        digest,
                        size,
                        media_type,
                        duration_ms,
                        now,
                        now,
                    ),
                )
            self.connection.execute(
                "UPDATE workbench_upload_item SET state='queued',document_id=?,message='',"
                "updated_at=? WHERE upload_item_id=? AND upload_session_id=? "
                "AND matter_id=? AND state='uploaded'",
                (document, now, item_id, session_id, matter_id),
            )
            remaining = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_upload_item WHERE upload_session_id=? "
                    "AND state NOT IN ('queued','failed','cancelled')",
                    (session_id,),
                ).fetchone()[0]
            )
            failures = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_upload_item WHERE upload_session_id=? "
                    "AND state IN ('failed','cancelled')",
                    (session_id,),
                ).fetchone()[0]
            )
            session_state = "open" if remaining else ("partial" if failures else "complete")
            self.connection.execute(
                "UPDATE workbench_upload_session SET state=?,updated_at=? "
                "WHERE upload_session_id=?",
                (session_state, now, session_id),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_ingest_job WHERE matter_id=? AND document_id=?",
                (matter_id, document),
            ).fetchone()
        return self.upload_item(matter_id, actor_id, session_id, item_id), (
            self._job(row) if row is not None else None
        )

    def fail_upload_item(
        self,
        matter_id: str,
        actor_id: str,
        session_id: str,
        item_id: str,
        message: str,
    ) -> UploadItemRecord:
        self.upload_item(matter_id, actor_id, session_id, item_id)
        value = self._safe_text(
            message, label="Upload message", maximum=240, required=False
        )
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE workbench_upload_item SET state='failed',message=?,updated_at=? "
                "WHERE upload_item_id=? AND upload_session_id=? AND matter_id=? "
                "AND state NOT IN ('queued','cancelled')",
                (value, now, item_id, session_id, matter_id),
            )
            remaining = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_upload_item WHERE upload_session_id=? "
                    "AND state NOT IN ('queued','failed','cancelled')",
                    (session_id,),
                ).fetchone()[0]
            )
            self.connection.execute(
                "UPDATE workbench_upload_session SET state=?,updated_at=? "
                "WHERE upload_session_id=?",
                ("open" if remaining else "partial", now, session_id),
            )
        return self.upload_item(matter_id, actor_id, session_id, item_id)

    def cancel_upload_session(
        self, matter_id: str, actor_id: str, session_id: str
    ) -> tuple[UploadItemRecord, ...]:
        session, _ = self.upload_session(matter_id, actor_id, session_id)
        if session.state == "complete":
            raise WorkspaceProblem("That upload collection has already completed.")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE workbench_upload_item SET state='cancelled',"
                "message='Upload cancelled',updated_at=? WHERE upload_session_id=? "
                "AND matter_id=? AND state NOT IN ('queued','cancelled')",
                (now, session_id, matter_id),
            )
            self.connection.execute(
                "UPDATE workbench_upload_session SET state='cancelled',updated_at=? "
                "WHERE upload_session_id=? AND matter_id=?",
                (now, session_id, matter_id),
            )
        _, updated = self.upload_session(matter_id, actor_id, session_id)
        return tuple(item for item in updated if item.state == "cancelled")

    def start_analysis_run(self, matter_id: str, actor_id: str) -> AnalysisRunRecord:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        now = self._now()
        analysis_id = f"analysis-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor)
            self.connection.execute(
                "INSERT INTO workbench_analysis_run("
                "analysis_id,matter_id,requested_by,state,created_at) "
                "VALUES (?,?,?,'running',?)",
                (analysis_id, matter_id, actor, now),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_analysis_run WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("analysis run creation did not persist")
        return self._analysis_run(row)

    def complete_analysis_run(
        self,
        analysis_id: str,
        matter_id: str,
        actor_id: str,
        *,
        source_count: int,
        unit_count: int,
        entity_count: int,
        capped: bool,
        findings: Sequence[Mapping[str, object]],
    ) -> AnalysisRunRecord:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        if not re.fullmatch(r"analysis-[0-9a-f]{32}", analysis_id):
            raise KeyError(analysis_id)
        if min(source_count, unit_count, entity_count) < 0:
            raise ValueError("invalid analysis counts")
        prepared: list[
            tuple[str, str, str, str, tuple[tuple[object, ...], ...]]
        ] = []
        for finding in findings:
            kind = str(finding.get("kind", ""))
            if kind not in {"comparison", "contradiction"}:
                raise ValueError("invalid review finding kind")
            signature = str(finding.get("signature", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", signature):
                raise ValueError("invalid review finding signature")
            title = self._derived_text(
                str(finding.get("title", "")), label="Finding title", maximum=200
            )
            summary = self._derived_text(
                str(finding.get("summary", "")),
                label="Finding summary",
                maximum=4_000,
                multiline=True,
            )
            references = finding.get("references")
            if not isinstance(references, Sequence) or isinstance(references, (str, bytes)):
                raise ValueError("review finding references are invalid")
            prepared_references: list[tuple[object, ...]] = []
            for ordinal, reference in enumerate(references, 1):
                if not isinstance(reference, Mapping):
                    raise ValueError("review finding reference is invalid")
                document_id = self._source_document_id(
                    str(reference.get("document_id", ""))
                )
                source_version_id = str(reference.get("source_version_id", ""))
                chunk_id = str(reference.get("chunk_id", ""))
                excerpt_digest = str(reference.get("excerpt_digest", ""))
                support_token = str(reference.get("support_token", ""))
                unit_number = reference.get("unit_number", 0)
                if (
                    not re.fullmatch(r"[0-9a-f]{32}", source_version_id)
                    or not re.fullmatch(r"chunk-[1-9][0-9]{0,8}", chunk_id)
                    or not re.fullmatch(r"[0-9a-f]{64}", excerpt_digest)
                    or not re.fullmatch(r"[0-9a-f]{40}", support_token)
                    or isinstance(unit_number, bool)
                    or not isinstance(unit_number, int)
                    or unit_number <= 0
                ):
                    raise ValueError("review finding reference identity is invalid")
                prepared_references.append(
                    (
                        ordinal,
                        document_id,
                        source_version_id,
                        self._safe_text(
                            str(reference.get("source_name", "")),
                            label="Finding source",
                            maximum=2_048,
                        ),
                        self._safe_text(
                            str(reference.get("location", "")),
                            label="Finding source location",
                            maximum=200,
                        ),
                        unit_number,
                        chunk_id,
                        excerpt_digest,
                        self._source_text(
                            str(reference.get("excerpt", "")),
                            label="Finding excerpt",
                            maximum=6_000,
                            multiline=True,
                        ),
                        support_token,
                    )
                )
            if not 2 <= len(prepared_references) <= 10:
                raise ValueError("review findings require two to ten source references")
            prepared.append(
                (kind, signature, title, summary, tuple(prepared_references))
            )
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            run = self.connection.execute(
                "SELECT state FROM workbench_analysis_run "
                "WHERE analysis_id=? AND matter_id=?",
                (analysis_id, matter_id),
            ).fetchone()
            if run is None or run["state"] != "running":
                raise KeyError(analysis_id)
            self.connection.execute(
                "UPDATE workbench_review_finding SET active=0,updated_by=?,updated_at=? "
                "WHERE matter_id=?",
                (actor, now, matter_id),
            )
            active_ids: list[str] = []
            for kind, signature, title, summary, references in prepared:
                existing = self.connection.execute(
                    "SELECT finding_id FROM workbench_review_finding "
                    "WHERE matter_id=? AND kind=? AND signature=?",
                    (matter_id, kind, signature),
                ).fetchone()
                if existing is None:
                    finding_id = f"review-finding-{uuid.uuid4().hex}"
                    self.connection.execute(
                        "INSERT INTO workbench_review_finding("
                        "finding_id,matter_id,kind,signature,title,summary,status,active,"
                        "created_by,created_at,updated_by,updated_at) "
                        "VALUES (?,?,?,?,?,?,'suggested',1,?,?,?,?)",
                        (
                            finding_id,
                            matter_id,
                            kind,
                            signature,
                            title,
                            summary,
                            actor,
                            now,
                            actor,
                            now,
                        ),
                    )
                else:
                    finding_id = str(existing["finding_id"])
                    self.connection.execute(
                        "UPDATE workbench_review_finding SET title=?,summary=?,active=1,"
                        "updated_by=?,updated_at=? WHERE matter_id=? AND finding_id=?",
                        (title, summary, actor, now, matter_id, finding_id),
                    )
                    self.connection.execute(
                        "DELETE FROM workbench_review_finding_reference "
                        "WHERE matter_id=? AND finding_id=?",
                        (matter_id, finding_id),
                    )
                active_ids.append(finding_id)
                self.connection.executemany(
                    "INSERT INTO workbench_review_finding_reference("
                    "reference_id,finding_id,matter_id,ordinal,document_id,source_version_id,"
                    "source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,"
                    "support_token,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            f"review-reference-{uuid.uuid4().hex}",
                            finding_id,
                            matter_id,
                            *reference,
                            now,
                        )
                        for reference in references
                    ],
                )
            self.connection.execute(
                "UPDATE workbench_analysis_run SET state='complete',source_count=?,"
                "unit_count=?,entity_count=?,finding_count=?,capped=?,message=?,finished_at=? "
                "WHERE analysis_id=? AND matter_id=?",
                (
                    source_count,
                    unit_count,
                    entity_count,
                    len(active_ids),
                    int(capped),
                    (
                        "Review map refreshed within bounded scan limits."
                        if capped
                        else "Review map refreshed."
                    ),
                    now,
                    analysis_id,
                    matter_id,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_analysis_run WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("analysis run completion did not persist")
        return self._analysis_run(row)

    def fail_analysis_run(
        self, analysis_id: str, matter_id: str, message: str
    ) -> None:
        value = self._safe_text(
            message,
            label="Analysis failure",
            maximum=500,
            required=False,
        )
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE workbench_analysis_run SET state='failed',message=?,finished_at=? "
                "WHERE analysis_id=? AND matter_id=? AND state='running'",
                (value, now, analysis_id, matter_id),
            )

    def latest_analysis_run(self, matter_id: str) -> AnalysisRunRecord | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_analysis_run WHERE matter_id=? "
                "ORDER BY created_at DESC,analysis_id DESC LIMIT 1",
                (matter_id,),
            ).fetchone()
        return self._analysis_run(row) if row is not None else None

    def review_findings(
        self, matter_id: str, *, kind: str = ""
    ) -> tuple[ReviewFindingRecord, ...]:
        if kind and kind not in {"comparison", "contradiction"}:
            raise ValueError("invalid review finding kind")
        parameters: tuple[object, ...] = (matter_id, kind) if kind else (matter_id,)
        condition = " AND kind=?" if kind else ""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_review_finding WHERE matter_id=? AND active=1"
                + condition
                + " ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'needs_review' THEN 1 "
                "WHEN 'suggested' THEN 2 ELSE 3 END,updated_at DESC,finding_id",
                parameters,
            ).fetchall()
        return tuple(self._review_finding(row) for row in rows)

    def review_finding_references(
        self, matter_id: str, finding_id: str
    ) -> tuple[ReviewFindingReferenceRecord, ...]:
        if not re.fullmatch(r"review-finding-[0-9a-f]{32}", finding_id):
            raise KeyError(finding_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_review_finding_reference "
                "WHERE matter_id=? AND finding_id=? ORDER BY ordinal",
                (matter_id, finding_id),
            ).fetchall()
        return tuple(self._review_finding_reference(row) for row in rows)

    def update_review_finding_status(
        self, matter_id: str, finding_id: str, status: str, actor_id: str
    ) -> ReviewFindingRecord:
        if status not in {"suggested", "confirmed", "needs_review", "dismissed"}:
            raise WorkspaceProblem("Choose a valid review status.")
        if not re.fullmatch(r"review-finding-[0-9a-f]{32}", finding_id):
            raise KeyError(finding_id)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        now = self._now()
        with self._lock, self.connection:
            self.membership(matter_id, actor)
            changed = self.connection.execute(
                "UPDATE workbench_review_finding SET status=?,updated_by=?,updated_at=? "
                "WHERE matter_id=? AND finding_id=? AND active=1",
                (status, actor, now, matter_id, finding_id),
            ).rowcount
            if not changed:
                raise KeyError(finding_id)
            row = self.connection.execute(
                "SELECT * FROM workbench_review_finding "
                "WHERE matter_id=? AND finding_id=?",
                (matter_id, finding_id),
            ).fetchone()
        if row is None:
            raise KeyError(finding_id)
        return self._review_finding(row)


    def _snapshot_notebook_scope_locked(
        self,
        matter_id: str,
        job_id: str,
        mode: str,
        item_ids: Sequence[str],
        now: str,
    ) -> tuple[AnswerNotebookContextRecord, ...]:
        if mode == "confirmed":
            rows = self.connection.execute(
                "SELECT item_id,item_type,status,title,body,date_label "
                "FROM workbench_notebook_item WHERE matter_id=? AND status='confirmed' "
                "ORDER BY is_pinned DESC,updated_at DESC,item_id DESC LIMIT 50",
                (matter_id,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in item_ids)
            selected = self.connection.execute(
                "SELECT item_id,item_type,status,title,body,date_label "
                "FROM workbench_notebook_item WHERE matter_id=? AND status<>'dismissed' "
                f"AND item_id IN ({placeholders})",
                (matter_id, *item_ids),
            ).fetchall()
            by_id = {row["item_id"]: row for row in selected}
            if len(by_id) != len(item_ids):
                raise WorkspaceProblem(
                    "One or more selected notebook items are no longer available."
                )
            rows = [by_id[item_id] for item_id in item_ids]
        if not rows:
            raise WorkspaceProblem(
                "Choose at least one available notebook item for answer context."
            )
        self.connection.execute(
            "INSERT INTO workbench_answer_notebook_scope(job_id,matter_id,mode,item_count,created_at) "
            "VALUES (?,?,?,?,?)",
            (job_id, matter_id, mode, len(rows), now),
        )
        snapshots: list[AnswerNotebookContextRecord] = []
        for ordinal, row in enumerate(rows, 1):
            material = json.dumps(
                {
                    "item_type": row["item_type"],
                    "status": row["status"],
                    "title": row["title"],
                    "body": row["body"],
                    "date_label": row["date_label"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            self.connection.execute(
                "INSERT INTO workbench_answer_notebook_scope_item("
                "job_id,matter_id,ordinal,notebook_item_id,item_type,status,title,body,"
                "date_label,content_digest) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, matter_id, ordinal, row["item_id"], row["item_type"],
                    row["status"], row["title"], row["body"], row["date_label"], digest,
                ),
            )
            snapshots.append(
                AnswerNotebookContextRecord(
                    row["item_id"], row["item_type"], row["status"], row["title"],
                    row["body"], row["date_label"], digest,
                )
            )
        return tuple(snapshots)

    def _append_answer_event_locked(
        self,
        job_id: str,
        *,
        state: str,
        stage: str,
        message: str,
        created_at: str,
    ) -> None:
        ordinal = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_answer_event WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            "INSERT INTO workbench_answer_event(job_id,ordinal,state,stage,message,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (job_id, ordinal, state, stage, message, created_at),
        )

    def queue_answer_job(
        self,
        matter_id: str,
        conversation_id: str | None,
        actor_id: str,
        question: str,
        idempotency_key: str,
        source_set_id: str | None = None,
        notebook_mode: str | None = None,
        notebook_item_ids: Sequence[str] = (),
        *, use_saved_context: bool = False, snapshot_builder=None,
        check_authority=lambda: None,
    ) -> tuple[AnswerJobRecord, bool]:
        """Persist one question and its answer job in the same transaction.

        A missing conversation identifies an unsaved browser draft. In that
        case conversation creation, automatic titling, the first user message,
        and durable answer admission share this transaction. A repeated
        actor/matter request key resolves the first transaction after a lost
        HTTP response instead of creating an empty or duplicate conversation.
        """

        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        value = " ".join(
            self._safe_text(
                question,
                label="Question",
                maximum=2_000,
                multiline=True,
            ).split()
        )
        if not _ANSWER_REQUEST.fullmatch(idempotency_key):
            raise WorkspaceProblem("The answer request identifier is invalid.")
        scope_id = (source_set_id or "").strip() or None
        if scope_id is not None and not _SOURCE_SET.fullmatch(scope_id):
            raise WorkspaceProblem("The selected source set is invalid.")
        context_mode = (notebook_mode or "").strip().casefold() or None
        if context_mode not in {None, "confirmed", "selected"}:
            raise WorkspaceProblem("The selected notebook context is invalid.")
        context_item_ids = tuple(dict.fromkeys(
            str(item_id).strip() for item_id in notebook_item_ids if str(item_id).strip()
        ))
        if len(context_item_ids) > 50 or any(
            not _NOTEBOOK_ITEM.fullmatch(item_id) for item_id in context_item_ids
        ):
            raise WorkspaceProblem("Choose at most 50 valid notebook items.")
        if context_mode == "selected" and not context_item_ids:
            raise WorkspaceProblem("Choose at least one notebook item for answer context.")
        if context_mode != "selected" and context_item_ids:
            raise WorkspaceProblem("Selected notebook items require selected context mode.")
        if use_saved_context and (context_mode or context_item_ids):
            raise WorkspaceProblem("Choose saved matter context OR legacy notebook context, not simultaneous modes.")
        if use_saved_context and snapshot_builder is None:
            raise WorkspaceProblem("Saved context requires authorized snapshot admission.")
        now = self._now()
        job_id = f"answer-job-{uuid.uuid4().hex}"
        question_message_id = f"message-{uuid.uuid4().hex}"
        selected_conversation_id = (conversation_id or "").strip() or None
        with self._lock, self.connection:
            # Serialize the idempotency lookup before creating a draft. Python's
            # sqlite context manager otherwise waits until the first INSERT to
            # begin its IMMEDIATE transaction, allowing two process-local
            # stores to observe the same missing key.
            self.connection.execute("BEGIN IMMEDIATE")
            check_authority()
            self.membership(matter_id, actor)
            if selected_conversation_id is None:
                prior_draft = self.connection.execute(
                    "SELECT * FROM workbench_answer_job WHERE actor_id=? AND matter_id=? "
                    "AND idempotency_key=? ORDER BY created_at,job_id LIMIT 1",
                    (actor, matter_id, idempotency_key),
                ).fetchone()
                if prior_draft is not None:
                    selected_conversation_id = str(prior_draft["conversation_id"])
                else:
                    conversation = self._create_conversation_locked(
                        matter_id,
                        "New conversation",
                        now=now,
                        actor_id=actor,
                    )
                    selected_conversation_id = conversation.conversation_id
            existing = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE actor_id=? AND matter_id=? "
                "AND conversation_id=? AND idempotency_key=?",
                (actor, matter_id, selected_conversation_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                active = self.connection.execute(
                    "SELECT 1 FROM workbench_conversation c JOIN workbench_conversation_organization o ON o.conversation_id=c.conversation_id "
                    "WHERE c.matter_id=? AND c.conversation_id=? AND o.state='active'", (matter_id, selected_conversation_id)).fetchone()
                if active is None:
                    raise KeyError(selected_conversation_id)
                has_context = self.connection.execute('SELECT 1 FROM workbench_answer_context WHERE job_id=?', (existing['job_id'],)).fetchone() is not None
                if has_context != use_saved_context:
                    raise WorkspaceProblem('That request already belongs to a different context mode.')
                existing_scope = self.connection.execute(
                    "SELECT source_set_id FROM workbench_answer_source_scope WHERE job_id=?",
                    (existing["job_id"],),
                ).fetchone()
                if existing["question"] != value or (
                    existing_scope["source_set_id"] if existing_scope is not None else None
                ) != scope_id:
                    raise WorkspaceProblem(
                        "That answer request already belongs to a different question or source scope."
                    )
                existing_notebook = self.connection.execute(
                    "SELECT mode FROM workbench_answer_notebook_scope WHERE job_id=?",
                    (existing["job_id"],),
                ).fetchone()
                existing_mode = (
                    existing_notebook["mode"] if existing_notebook is not None else None
                )
                if existing_mode != context_mode:
                    raise WorkspaceProblem(
                        "That answer request already belongs to different notebook context."
                    )
                if context_mode == "selected":
                    existing_ids = tuple(
                        row["notebook_item_id"]
                        for row in self.connection.execute(
                            "SELECT notebook_item_id FROM workbench_answer_notebook_scope_item "
                            "WHERE job_id=? ORDER BY ordinal",
                            (existing["job_id"],),
                        ).fetchall()
                    )
                    if existing_ids != context_item_ids:
                        raise WorkspaceProblem(
                            "That answer request already belongs to different notebook context."
                        )
                return self._answer_job(existing), False
            bound = self.connection.execute(
                "SELECT c.title,(SELECT COUNT(*) FROM workbench_message existing "
                "WHERE existing.conversation_id=c.conversation_id) AS message_count "
                "FROM workbench_conversation c "
                "JOIN workbench_conversation_organization organization "
                "ON organization.conversation_id=c.conversation_id "
                "JOIN workbench_effective_membership mm ON mm.matter_id=c.matter_id "
                "JOIN workbench_principal p ON p.principal_id=mm.principal_id "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=c.matter_id "
                "WHERE c.conversation_id=? AND c.matter_id=? AND mm.principal_id=? "
                "AND mm.state='active' AND p.active=1 AND ml.state='active' "
                "AND organization.state='active'",
                (selected_conversation_id, matter_id, actor),
            ).fetchone()
            if bound is None:
                raise KeyError(selected_conversation_id)
            active_answer = self.connection.execute(
                "SELECT 1 FROM workbench_answer_job WHERE conversation_id=? "
                "AND matter_id=? AND state IN ('queued','running') LIMIT 1",
                (selected_conversation_id, matter_id),
            ).fetchone()
            if active_answer is not None:
                raise WorkspaceProblem(
                    "This conversation already has an answer in progress."
                )
            active_research = self.connection.execute(
                "SELECT 1 FROM workbench_research_job WHERE conversation_id=? "
                "AND matter_id=? AND state IN ('queued','running') LIMIT 1",
                (selected_conversation_id, matter_id),
            ).fetchone()
            if active_research is not None:
                raise WorkspaceProblem(
                    "A broader investigation is still working in this conversation. "
                    "Wait for it to return before asking a follow-up."
                )
            if scope_id is not None:
                scope = self.connection.execute(
                    "SELECT 1 FROM workbench_source_set s "
                    "JOIN workbench_source_set_item i ON i.source_set_id=s.source_set_id "
                    "WHERE s.source_set_id=? AND s.matter_id=? LIMIT 1",
                    (scope_id, matter_id),
                ).fetchone()
                if scope is None:
                    raise WorkspaceProblem(
                        "That source set is empty or no longer available."
                    )
            if int(bound["message_count"]) == 0 and bound["title"] in {
                "Case review",
                "New conversation",
            }:
                self.connection.execute(
                    "UPDATE workbench_conversation SET title=? "
                    "WHERE conversation_id=? AND matter_id=?",
                    (
                        self._automatic_conversation_title(value),
                        selected_conversation_id,
                        matter_id,
                    ),
                )
            ordinal = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_message "
                    "WHERE conversation_id=?",
                    (selected_conversation_id,),
                ).fetchone()[0]
            )
            self.connection.execute(
                "INSERT INTO workbench_message(message_id,conversation_id,ordinal,role,content,payload_json,created_at) "
                "VALUES (?,?,?,'user',?,'{}',?)",
                (question_message_id, selected_conversation_id, ordinal, value, now),
            )
            self.connection.execute(
                "INSERT INTO workbench_answer_job("
                "job_id,matter_id,conversation_id,actor_id,idempotency_key,question,"
                "question_message_id,state,stage,message,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'queued','queued',?,?,?)",
                (
                    job_id,
                    matter_id,
                    selected_conversation_id,
                    actor,
                    idempotency_key,
                    value,
                    question_message_id,
                    "Waiting for earlier saved work to finish.",
                    now,
                    now,
                ),
            )
            if scope_id is not None:
                self.connection.execute(
                    "INSERT INTO workbench_answer_source_scope(job_id,matter_id,source_set_id) "
                    "VALUES (?,?,?)",
                    (job_id, matter_id, scope_id),
                )
            if context_mode is not None:
                self._snapshot_notebook_scope_locked(
                    matter_id,
                    job_id,
                    context_mode,
                    context_item_ids,
                    now,
                )
            if use_saved_context:
                from .matter_context_repository import serialized
                snapshot = snapshot_builder()
                snapshot.update(job_id=job_id, conversation_id=selected_conversation_id, request_key=idempotency_key)
                encoded_snapshot = serialized(snapshot)
                if len(encoded_snapshot.encode('utf-8')) > 512 * 1024:
                    raise WorkspaceProblem('The complete submitted context exceeds its storage bound. Reduce the selection.')
                self.connection.execute('INSERT INTO workbench_answer_context VALUES (?,?,?)',
                                        (job_id, matter_id, encoded_snapshot))
            check_authority()
            self._append_answer_event_locked(
                job_id,
                state="queued",
                stage="queued",
                message="Request saved and queued.",
                created_at=now,
            )
            self.connection.execute(
                "UPDATE workbench_conversation SET updated_at=? "
                "WHERE conversation_id=? AND matter_id=?",
                (now, selected_conversation_id, matter_id),
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._answer_job(row), True

    def get_answer_job(
        self, matter_id: str, actor_id: str, job_id: str
    ) -> AnswerJobRecord:
        if not _ANSWER_JOB.fullmatch(job_id):
            raise KeyError(job_id)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=? AND matter_id=? AND actor_id=?",
                (job_id, matter_id, actor),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._answer_job(row)

    def answer_events(
        self, matter_id: str, actor_id: str, job_id: str
    ) -> tuple[AnswerEventRecord, ...]:
        self.get_answer_job(matter_id, actor_id, job_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT job_id,ordinal,state,stage,message,created_at "
                "FROM workbench_answer_event WHERE job_id=? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
        return tuple(self._answer_event(row) for row in rows)

    def latest_answer_job(
        self, matter_id: str, conversation_id: str, actor_id: str
    ) -> AnswerJobRecord | None:
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE matter_id=? AND conversation_id=? "
                "AND actor_id=? ORDER BY created_at DESC,job_id DESC LIMIT 1",
                (matter_id, conversation_id, actor),
            ).fetchone()
        if row is None or row["result_message_id"] is not None:
            return None
        return self._answer_job(row)

    def answer_jobs(
        self, matter_id: str, actor_id: str, *, limit: int = 25
    ) -> tuple[AnswerJobRecord, ...]:
        """Return recent answer work for one authorized matter member.

        The activity center projects only job state and navigation metadata;
        callers must not surface the stored question from these records.
        """

        self.membership(matter_id, actor_id)
        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        bounded = min(max(int(limit), 1), 100)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE matter_id=? AND actor_id=? "
                "ORDER BY updated_at DESC,job_id DESC LIMIT ?",
                (matter_id, actor, bounded),
            ).fetchall()
        return tuple(self._answer_job(row) for row in rows)

    def answer_jobs_for_actor(
        self, actor_id: str, *, limit: int = 150
    ) -> tuple[AnswerJobRecord, ...]:
        """Return recent personal answer work across active memberships."""

        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self.connection.execute(
                "SELECT j.* FROM workbench_answer_job j "
                "JOIN workbench_effective_membership mm ON mm.matter_id=j.matter_id "
                "AND mm.principal_id=? AND mm.state='active' "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=j.matter_id "
                "AND ml.state='active' "
                "WHERE j.actor_id=? ORDER BY j.updated_at DESC,j.job_id DESC LIMIT ?",
                (actor, actor, bounded),
            ).fetchall()
        return tuple(self._answer_job(row) for row in rows)

    def recover_running_answer_jobs(self) -> int:
        """Requeue interrupted work, or complete an already-requested cancellation."""

        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                "SELECT job_id,cancellation_requested FROM workbench_answer_job "
                "WHERE state='running' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active') ORDER BY created_at,job_id"
            ).fetchall()
            for row in rows:
                if int(row["cancellation_requested"]):
                    state = "cancelled"
                    stage = "cancelled"
                    message = "Cancelled during restart recovery."
                    self.connection.execute(
                        "UPDATE workbench_answer_job SET state=?,stage=?,message=?,worker_id=NULL,"
                        "finished_at=?,updated_at=? WHERE job_id=? AND state='running'",
                        (state, stage, message, now, now, row["job_id"]),
                    )
                else:
                    state = "queued"
                    stage = "queued"
                    message = "Queued again after the workbench restarted."
                    self.connection.execute(
                        "UPDATE workbench_answer_job SET state=?,stage=?,message=?,worker_id=NULL,"
                        "started_at=NULL,finished_at=NULL,updated_at=? "
                        "WHERE job_id=? AND state='running'",
                        (state, stage, message, now, row["job_id"]),
                    )
                self._append_answer_event_locked(
                    row["job_id"],
                    state=state,
                    stage=stage,
                    message=message,
                    created_at=now,
                )
        return len(rows)

    def claim_answer_job(self, worker_id: str) -> AnswerJobRecord | None:
        """Claim fairly across actor-and-matter lanes, serializing conversations."""

        worker = self._safe_text(worker_id, label="Worker identity", maximum=100)
        now = self._now()
        lane_last_claim = (
            "(SELECT MAX(h.last_claimed_at) FROM workbench_answer_job h "
            "WHERE h.actor_id=q.actor_id AND h.matter_id=q.matter_id)"
        )
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT q.job_id FROM workbench_answer_job q "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=q.matter_id "
                "WHERE q.state='queued' AND ml.state='active' "
                "AND NOT EXISTS (SELECT 1 FROM workbench_answer_job r "
                "WHERE r.conversation_id=q.conversation_id AND r.state='running') "
                f"ORDER BY CASE WHEN {lane_last_claim} IS NULL THEN 0 ELSE 1 END, "
                f"{lane_last_claim},q.created_at,q.job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = self.connection.execute(
                "UPDATE workbench_answer_job SET state='running',stage='queued',"
                "attempts=attempts+1,worker_id=?,cancellation_requested=0,"
                "message='Starting this request.',started_at=?,last_claimed_at=?,"
                "finished_at=NULL,updated_at=? WHERE job_id=? AND state='queued'",
                (worker, now, now, now, row["job_id"]),
            ).rowcount
            if changed != 1:
                return None
            self._append_answer_event_locked(
                row["job_id"],
                state="running",
                stage="queued",
                message="Answering has started.",
                created_at=now,
            )
            claimed = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._answer_job(claimed)

    def update_answer_job_stage(self, job_id: str, stage: str, message: str) -> bool:
        if not _ANSWER_JOB.fullmatch(job_id):
            raise KeyError(job_id)
        if stage not in _ANSWER_ACTIVE_STAGES:
            raise ValueError("invalid active answer stage")
        value = self._safe_text(
            message, label="Answer status", maximum=240, required=False
        )
        now = self._now()
        with self._lock, self.connection:
            changed = self.connection.execute(
                "UPDATE workbench_answer_job SET stage=?,message=?,updated_at=? "
                "WHERE job_id=? AND state='running' AND cancellation_requested=0",
                (stage, value, now, job_id),
            ).rowcount
            if changed == 1:
                self._append_answer_event_locked(
                    job_id,
                    state="running",
                    stage=stage,
                    message=value,
                    created_at=now,
                )
                return True
            row = self.connection.execute(
                "SELECT state,cancellation_requested FROM workbench_answer_job WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if int(row["cancellation_requested"]) or row["state"] == "cancelled":
            return False
        raise WorkspaceProblem("This answer request is no longer running.")

    def answer_cancellation_requested(self, job_id: str) -> bool:
        if not _ANSWER_JOB.fullmatch(job_id):
            raise KeyError(job_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT cancellation_requested,state FROM workbench_answer_job WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return bool(row["cancellation_requested"] or row["state"] == "cancelled")

    def finish_answer_job(
        self,
        job_id: str,
        *,
        content: str,
        payload: Mapping[str, object],
    ) -> MessageRecord | None:
        """Commit one assistant message and success state atomically."""

        value = self._derived_text(
            content, label="Answer", maximum=20_000, multiline=True
        )
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 100_000:
            raise WorkspaceProblem(
                "The answer and its source details exceed the save limit. "
                "Ask a narrower question or select fewer sources; existing work is retained."
            )
        now = self._now()
        with self._lock, self.connection:
            job = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            if payload.get('recorded_context_job'):
                if payload['recorded_context_job'] != job_id or payload.get('recorded_context_attempt') != job['attempts']:
                    raise WorkspaceProblem('This answer worker was superseded before saving.')
            if job["state"] == "succeeded" and job["result_message_id"]:
                row = self.connection.execute(
                    "SELECT message_id,conversation_id,ordinal,role,content,payload_json,created_at "
                    "FROM workbench_message WHERE message_id=?",
                    (job["result_message_id"],),
                ).fetchone()
                if row is None:
                    raise RuntimeError("completed answer message is missing")
                return MessageRecord(
                    row["message_id"],
                    row["conversation_id"],
                    int(row["ordinal"]),
                    row["role"],
                    row["content"],
                    json.loads(row["payload_json"]),
                    row["created_at"],
                )
            if job["state"] != "running":
                raise WorkspaceProblem("This answer request is no longer running.")
            if int(job["cancellation_requested"]):
                self.connection.execute(
                    "UPDATE workbench_answer_job SET state='cancelled',stage='cancelled',"
                    "message='Answer cancelled.',worker_id=NULL,finished_at=?,updated_at=? "
                    "WHERE job_id=? AND state='running'",
                    (now, now, job_id),
                )
                self._append_answer_event_locked(
                    job_id,
                    state="cancelled",
                    stage="cancelled",
                    message="Answer cancelled.",
                    created_at=now,
                )
                return None
            authorized = self.connection.execute(
                "SELECT 1 FROM workbench_effective_membership membership "
                "JOIN workbench_principal principal "
                "ON principal.principal_id=membership.principal_id "
                "JOIN workbench_matter_lifecycle lifecycle "
                "ON lifecycle.matter_id=membership.matter_id "
                "WHERE membership.matter_id=? AND membership.principal_id=? "
                "AND membership.state='active' AND principal.active=1 "
                "AND lifecycle.state='active'",
                (job["matter_id"], job["actor_id"]),
            ).fetchone()
            if authorized is None:
                raise WorkspaceProblem(
                    "Access to this matter was removed before the answer could be saved."
                )
            ordinal = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_message "
                    "WHERE conversation_id=?",
                    (job["conversation_id"],),
                ).fetchone()[0]
            )
            message_id = f"message-{uuid.uuid4().hex}"
            self.connection.execute(
                "INSERT INTO workbench_message(message_id,conversation_id,ordinal,role,content,payload_json,created_at) "
                "VALUES (?,?,?,'assistant',?,?,?)",
                (message_id, job["conversation_id"], ordinal, value, encoded, now),
            )
            changed = self.connection.execute(
                "UPDATE workbench_answer_job SET state='succeeded',stage='complete',"
                "message='Answer ready.',worker_id=NULL,result_message_id=?,finished_at=?,updated_at=? "
                "WHERE job_id=? AND state='running' AND cancellation_requested=0",
                (message_id, now, now, job_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("answer completion state changed unexpectedly")
            self._append_answer_event_locked(
                job_id,
                state="succeeded",
                stage="complete",
                message="Answer ready.",
                created_at=now,
            )
            self.connection.execute(
                "UPDATE workbench_conversation SET updated_at=? WHERE conversation_id=?",
                (now, job["conversation_id"]),
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, job["matter_id"]),
            )
        return MessageRecord(
            message_id,
            job["conversation_id"],
            ordinal,
            "assistant",
            value,
            dict(payload),
            now,
        )

    def fail_answer_job(self, job_id: str, message: str, *, expected_attempt: int | None = None) -> AnswerJobRecord:
        value = self._safe_text(
            message, label="Answer status", maximum=240, required=False
        ) or "The answer could not be completed. Try again."
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if expected_attempt is not None and row['attempts'] != expected_attempt:
                return self._answer_job(row)
            if row["state"] != "running":
                return self._answer_job(row)
            cancelled = bool(row["cancellation_requested"])
            state = "cancelled" if cancelled else "failed"
            stage = "cancelled" if cancelled else "failed"
            final_message = "Answer cancelled." if cancelled else value
            self.connection.execute(
                "UPDATE workbench_answer_job SET state=?,stage=?,message=?,worker_id=NULL,"
                "finished_at=?,updated_at=? WHERE job_id=? AND state='running'",
                (state, stage, final_message, now, now, job_id),
            )
            self._append_answer_event_locked(
                job_id,
                state=state,
                stage=stage,
                message=final_message,
                created_at=now,
            )
            updated = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._answer_job(updated)

    def cancel_answer_job(
        self, matter_id: str, actor_id: str, job_id: str
    ) -> AnswerJobRecord:
        job = self.get_answer_job(matter_id, actor_id, job_id)
        now = self._now()
        with self._lock, self.connection:
            current = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=? AND matter_id=? AND actor_id=?",
                (job_id, matter_id, actor_id),
            ).fetchone()
            if current["state"] == "cancelled":
                return self._answer_job(current)
            if current["state"] == "queued":
                state = "cancelled"
                stage = "cancelled"
                message = "Answer cancelled before it started."
                finished_at = now
            elif current["state"] == "running":
                state = "running"
                stage = "cancelling"
                message = "Cancelling after the current answer step."
                finished_at = None
            else:
                raise WorkspaceProblem("This answer request can no longer be cancelled.")
            self.connection.execute(
                "UPDATE workbench_answer_job SET state=?,stage=?,message=?,"
                "cancellation_requested=1,finished_at=?,updated_at=? WHERE job_id=?",
                (state, stage, message, finished_at, now, job_id),
            )
            self._append_answer_event_locked(
                job_id,
                state=state,
                stage=stage,
                message=message,
                created_at=now,
            )
            updated = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._answer_job(updated)

    def retry_answer_job(
        self, matter_id: str, actor_id: str, job_id: str
    ) -> AnswerJobRecord:
        self.get_answer_job(matter_id, actor_id, job_id)
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor_id)
            current = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=? AND matter_id=? AND actor_id=?",
                (job_id, matter_id, actor_id),
            ).fetchone()
            if current is None:
                raise KeyError(job_id)
            if current["state"] in {"queued", "running"}:
                return self._answer_job(current)
            if current["state"] not in {"failed", "cancelled"}:
                raise WorkspaceProblem("This completed answer does not need to be retried.")
            active_answer = self.connection.execute(
                "SELECT 1 FROM workbench_answer_job WHERE conversation_id=? "
                "AND matter_id=? AND job_id<>? AND state IN ('queued','running') LIMIT 1",
                (current["conversation_id"], matter_id, job_id),
            ).fetchone()
            if active_answer is not None:
                raise WorkspaceProblem(
                    "This conversation already has an answer in progress."
                )
            active_research = self.connection.execute(
                "SELECT 1 FROM workbench_research_job WHERE conversation_id=? "
                "AND matter_id=? AND state IN ('queued','running') LIMIT 1",
                (current["conversation_id"], matter_id),
            ).fetchone()
            if active_research is not None:
                raise WorkspaceProblem(
                    "A broader investigation is still working in this conversation. "
                    "Wait for it to return before asking a follow-up."
                )
            self.connection.execute(
                "UPDATE workbench_answer_job SET state='queued',stage='queued',"
                "message='Request queued to try again.',worker_id=NULL,cancellation_requested=0,"
                "started_at=NULL,finished_at=NULL,updated_at=? WHERE job_id=?",
                (now, job_id),
            )
            self._append_answer_event_locked(
                job_id,
                state="queued",
                stage="queued",
                message="Request queued to try again.",
                created_at=now,
            )
            updated = self.connection.execute(
                "SELECT * FROM workbench_answer_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._answer_job(updated)

    def answer_queue_position(
        self, matter_id: str, actor_id: str, job_id: str
    ) -> int | None:
        job = self.get_answer_job(matter_id, actor_id, job_id)
        if job.state != "queued":
            return None
        lane_last_claim = (
            "(SELECT MAX(h.last_claimed_at) FROM workbench_answer_job h "
            "WHERE h.actor_id=q.actor_id AND h.matter_id=q.matter_id)"
        )
        with self._lock:
            rows = self.connection.execute(
                "SELECT q.job_id FROM workbench_answer_job q WHERE q.state='queued' "
                f"ORDER BY CASE WHEN {lane_last_claim} IS NULL THEN 0 ELSE 1 END, "
                f"{lane_last_claim},q.created_at,q.job_id"
            ).fetchall()
        return next(
            (position for position, row in enumerate(rows, 1) if row["job_id"] == job_id),
            None,
        )

    def answer_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM workbench_answer_job GROUP BY state"
            ).fetchall()
        counts = {
            state: 0
            for state in ("queued", "running", "succeeded", "failed", "cancelled")
        }
        counts.update({row["state"]: int(row["count"]) for row in rows})
        return counts

    # Durable Deep Research -------------------------------------------------

    def _append_research_event_locked(
        self,
        job_id: str,
        *,
        state: str,
        stage: str,
        message: str,
        completed_steps: int,
        total_steps: int,
        created_at: str,
    ) -> None:
        ordinal = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_research_event WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            "INSERT INTO workbench_research_event(job_id,ordinal,state,stage,"
            "completed_steps,total_steps,message,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, ordinal, state, stage, completed_steps, total_steps, message, created_at),
        )

    def queue_research_job(
        self,
        matter_id: str,
        actor_id: str,
        question: str,
        title: str,
        idempotency_key: str,
        source_set_id: str | None = None,
        *,
        conversation_id: str | None = None,
        initial_plan: Mapping[str, object] | None = None,
        initial_result: Mapping[str, object] | None = None,
        validate_locked: Callable[[], None] | None = None,
    ) -> tuple[ResearchJobRecord, bool]:
        actor = self.membership(matter_id, actor_id).principal_id
        value = self._safe_text(question, label="Research question", maximum=2_000,
            multiline=is_full_text_synthesis(initial_plan, initial_result))
        heading = self._safe_text(title, label="Research title", maximum=160)
        if not _RESEARCH_REQUEST.fullmatch(idempotency_key or ""):
            raise WorkspaceProblem("The research request expired. Refresh and try again.")
        scope_id = (source_set_id or "").strip() or None
        conversation_ref = (conversation_id or "").strip() or None
        if scope_id is not None:
            if not self.source_set_document_ids(matter_id, scope_id):
                raise WorkspaceProblem("That source set is empty or no longer available.")
        now = self._now()
        plan_json = json.dumps(dict(initial_plan or {}), ensure_ascii=False, separators=(",", ":"))
        result_json = json.dumps(dict(initial_result or {}), ensure_ascii=False, separators=(",", ":"))
        if len(plan_json) > 40_000 or len(result_json) > 2_000_000:
            raise WorkspaceProblem("This synthesis input exceeds the saved-work limit.")
        if is_full_text_synthesis(initial_plan, initial_result) and validate_locked is None:
            raise WorkspaceProblem("Full-text synthesis admission requires its frozen-input validator.")
        with self._lock, self.connection:
            # Admission across the answer and research tables must share the
            # same database write lock, including across process-local stores.
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor)
            if validate_locked is not None:
                validate_locked()
            existing = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE actor_id=? AND matter_id=? "
                "AND idempotency_key=?",
                (actor, matter_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["question"] != value
                    or existing["title"] != heading
                    or existing["source_set_id"] != scope_id
                    or existing["conversation_id"] != conversation_ref
                    or (initial_plan is not None and json.loads(existing["plan_json"]) != dict(initial_plan))
                ):
                    raise WorkspaceProblem(
                        "That saved request belongs to different investigation details. Refresh and try again."
                    )
                return self._research_job(existing), False
            job_id = f"research-job-{uuid.uuid4().hex}"
            if conversation_ref is not None:
                bound = self.connection.execute(
                    "SELECT c.title,(SELECT COUNT(*) FROM workbench_message existing "
                    "WHERE existing.conversation_id=c.conversation_id) AS message_count "
                    "FROM workbench_conversation c "
                    "JOIN workbench_conversation_organization organization "
                    "ON organization.conversation_id=c.conversation_id "
                    "JOIN workbench_effective_membership mm ON mm.matter_id=c.matter_id "
                    "JOIN workbench_principal p ON p.principal_id=mm.principal_id "
                    "JOIN workbench_matter_lifecycle ml ON ml.matter_id=c.matter_id "
                    "WHERE c.conversation_id=? AND c.matter_id=? AND mm.principal_id=? "
                    "AND mm.state='active' AND p.active=1 AND ml.state='active' "
                    "AND organization.state='active'",
                    (conversation_ref, matter_id, actor),
                ).fetchone()
                if bound is None:
                    raise WorkspaceProblem(
                        "That conversation is not available in this matter."
                    )
                active_answer = self.connection.execute(
                    "SELECT 1 FROM workbench_answer_job WHERE conversation_id=? "
                    "AND matter_id=? AND state IN ('queued','running') LIMIT 1",
                    (conversation_ref, matter_id),
                ).fetchone()
                active_research = self.connection.execute(
                    "SELECT 1 FROM workbench_research_job WHERE conversation_id=? "
                    "AND matter_id=? AND state IN ('queued','running') LIMIT 1",
                    (conversation_ref, matter_id),
                ).fetchone()
                if active_answer is not None or active_research is not None:
                    raise WorkspaceProblem(
                        "This conversation already has review work in progress."
                    )
                if int(bound["message_count"]) == 0 and bound["title"] in {
                    "Case review",
                    "New conversation",
                }:
                    self.connection.execute(
                        "UPDATE workbench_conversation SET title=? "
                        "WHERE conversation_id=? AND matter_id=?",
                        (
                            self._automatic_conversation_title(value),
                            conversation_ref,
                            matter_id,
                        ),
                    )
                question_message_id = f"message-{uuid.uuid4().hex}"
                ordinal = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_message "
                        "WHERE conversation_id=?",
                        (conversation_ref,),
                    ).fetchone()[0]
                )
                request_payload = json.dumps(
                    {
                        "kind": "research-request",
                        "workflow": "research",
                        "research_job_id": job_id,
                    },
                    separators=(",", ":"),
                )
                self.connection.execute(
                    "INSERT INTO workbench_message(message_id,conversation_id,ordinal,role,"
                    "content,payload_json,created_at) VALUES (?,?,?,'user',?,?,?)",
                    (
                        question_message_id,
                        conversation_ref,
                        ordinal,
                        value,
                        request_payload,
                        now,
                    ),
                )
            self.connection.execute(
                "INSERT INTO workbench_research_job(job_id,matter_id,actor_id,idempotency_key,"
                "question,title,source_set_id,conversation_id,state,stage,message,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'queued','queued','Research saved and queued.',?,?)",
                (
                    job_id,
                    matter_id,
                    actor,
                    idempotency_key,
                    value,
                    heading,
                    scope_id,
                    conversation_ref,
                    now,
                    now,
                ),
            )
            if initial_plan is not None or initial_result is not None:
                self.connection.execute(
                    "UPDATE workbench_research_job SET plan_json=?,result_json=?,total_steps=2 WHERE job_id=?",
                    (plan_json, result_json, job_id),
                )
                if is_full_text_synthesis(initial_plan, initial_result):
                    from .full_text_synthesis import validate_job_input
                    initial_row = self.connection.execute(
                        "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,),
                    ).fetchone()
                    validate_job_input(self._research_job(initial_row))
                    self.connection.execute(
                        "UPDATE workbench_research_job SET candidate_count=?,evidence_count=?,message=? WHERE job_id=?",
                        (initial_result["full_text_synthesis_input"]["counts"]["candidate_findings"],
                         len(initial_result["evidence"]), "Saved full-text findings queued for synthesis.", job_id),
                    )
            self._append_research_event_locked(
                job_id, state="queued", stage="queued", message="Research saved and queued.",
                completed_steps=0, total_steps=0, created_at=now,
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (now, matter_id)
            )
            if conversation_ref is not None:
                self.connection.execute(
                    "UPDATE workbench_conversation SET updated_at=? "
                    "WHERE conversation_id=? AND matter_id=?",
                    (now, conversation_ref, matter_id),
                )
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._research_job(row), True

    def research_job(
        self,
        matter_id: str,
        actor_id: str,
        job_id: str,
        *,
        administrator_override: bool = False,
    ) -> ResearchJobRecord:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        if not _RESEARCH_JOB.fullmatch(job_id or ""):
            raise KeyError(job_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE matter_id=? AND job_id=?",
                (matter_id, job_id),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._research_job(row)

    def research_jobs(
        self,
        matter_id: str,
        actor_id: str,
        *,
        limit: int = 100,
        administrator_override: bool = False,
    ) -> tuple[ResearchJobRecord, ...]:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE matter_id=? "
                "ORDER BY created_at DESC,job_id DESC LIMIT ?", (matter_id, bounded)
            ).fetchall()
        return tuple(self._research_job(row) for row in rows)

    def succeeded_research_jobs_for_final_bundle(
        self,
        matter_id: str,
        actor_id: str,
        *,
        maximum: int,
        administrator_override: bool = False,
    ) -> tuple[ResearchJobRecord, ...]:
        """Return at most one over the explicit final-bundle ledger bound."""

        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        bounded = int(maximum)
        if bounded < 1 or bounded > 500:
            raise ValueError("final-bundle investigation bound is invalid")
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE matter_id=? "
                "AND state='succeeded' ORDER BY created_at DESC,job_id DESC LIMIT ?",
                (matter_id, bounded + 1),
            ).fetchall()
        return tuple(self._research_job(row) for row in rows)

    def research_jobs_for_actor(
        self, actor_id: str, *, limit: int = 150
    ) -> tuple[ResearchJobRecord, ...]:
        """Return recent shared research work across active memberships."""

        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self.connection.execute(
                "SELECT j.* FROM workbench_research_job j "
                "JOIN workbench_effective_membership mm ON mm.matter_id=j.matter_id "
                "AND mm.principal_id=? AND mm.state='active' "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=j.matter_id "
                "AND ml.state='active' "
                "ORDER BY j.updated_at DESC,j.job_id DESC LIMIT ?",
                (actor, bounded),
            ).fetchall()
        return tuple(self._research_job(row) for row in rows)

    def research_events(self, matter_id: str, actor_id: str, job_id: str) -> tuple[ResearchEventRecord, ...]:
        self.research_job(matter_id, actor_id, job_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_research_event WHERE job_id=? ORDER BY ordinal", (job_id,)
            ).fetchall()
        return tuple(self._research_event(row) for row in rows)

    def recover_running_research_jobs(self) -> int:
        now = self._now()
        with self._lock, self.connection:
            # Recovery may overlap a delayed worker on another connection.
            # Freeze its latest charged checkpoint before reading and requeueing.
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                "SELECT job_id,cancellation_requested,total_steps,completed_steps,result_json "
                "FROM workbench_research_job WHERE state='running' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active') ORDER BY created_at,job_id"
            ).fetchall()
            for row in rows:
                cancelled = bool(row["cancellation_requested"])
                state = "cancelled" if cancelled else "queued"
                stage = "cancelled" if cancelled else "queued"
                message = (
                    "Cancelled during restart recovery." if cancelled
                    else "Queued again after the workbench restarted. Completed evidence is preserved."
                )
                result = json.loads(row["result_json"] or "{}")
                if cancelled:
                    result["stop_reason"] = "cancelled"
                encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                self.connection.execute(
                    "UPDATE workbench_research_job SET result_json=?,state=?,stage=?,message=?,worker_id=NULL,"
                    "started_at=NULL,finished_at=?,updated_at=? WHERE job_id=? AND state='running'",
                    (encoded, state, stage, message, now if cancelled else None, now, row["job_id"]),
                )
                self._append_research_event_locked(
                    row["job_id"], state=state, stage=stage, message=message,
                    completed_steps=int(row["completed_steps"]), total_steps=int(row["total_steps"]),
                    created_at=now,
                )
        return len(rows)

    def claim_research_job(self, worker_id: str) -> ResearchJobRecord | None:
        worker = self._safe_text(worker_id, label="Worker identity", maximum=100)
        now = self._now()
        lane_last_claim = (
            "(SELECT MAX(h.last_claimed_at) FROM workbench_research_job h "
            "WHERE h.actor_id=q.actor_id AND h.matter_id=q.matter_id)"
        )
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT q.job_id FROM workbench_research_job q "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=q.matter_id "
                "WHERE q.state='queued' AND ml.state='active' "
                f"ORDER BY CASE WHEN {lane_last_claim} IS NULL THEN 0 ELSE 1 END,"
                f"{lane_last_claim},q.created_at,q.job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = self.connection.execute(
                "UPDATE workbench_research_job SET state='running',stage='planning',"
                "attempts=attempts+1,worker_id=?,cancellation_requested=0,"
                "message='Building the research plan.',started_at=COALESCE(started_at,?),"
                "last_claimed_at=?,finished_at=NULL,updated_at=? WHERE job_id=? AND state='queued'",
                (worker, now, now, now, row["job_id"]),
            ).rowcount
            if changed != 1:
                return None
            claimed = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            self._append_research_event_locked(
                row["job_id"], state="running", stage="planning",
                message="Building the research plan.", completed_steps=int(claimed["completed_steps"]),
                total_steps=int(claimed["total_steps"]), created_at=now,
            )
        return self._research_job(claimed)

    def set_research_plan(self, job_id: str, plan: Mapping[str, object], total_steps: int) -> ResearchJobRecord:
        if not _RESEARCH_JOB.fullmatch(job_id or ""):
            raise KeyError(job_id)
        encoded = json.dumps(dict(plan), ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 40_000 or not 1 <= int(total_steps) <= 20:
            raise ValueError("invalid research plan")
        now = self._now()
        with self._lock, self.connection:
            self._reject_full_text_legacy_write_locked(job_id)
            changed = self.connection.execute(
                "UPDATE workbench_research_job SET plan_json=?,total_steps=?,stage='searching',"
                "message='Research plan ready. Finding evidence.',updated_at=? "
                "WHERE job_id=? AND state='running' AND cancellation_requested=0",
                (encoded, int(total_steps), now, job_id),
            ).rowcount
            if changed != 1:
                raise WorkspaceProblem("This research run is no longer active.")
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
            self._append_research_event_locked(
                job_id, state="running", stage="searching",
                message="Research plan ready. Finding evidence.",
                completed_steps=int(row["completed_steps"]), total_steps=int(row["total_steps"]),
                created_at=now,
            )
        return self._research_job(row)

    def update_research_progress(
        self, job_id: str, *, stage: str, message: str, completed_steps: int,
        candidate_count: int, evidence_count: int,
    ) -> bool:
        if stage not in {"searching", "synthesizing", "verifying"}:
            raise ValueError("invalid research stage")
        value = self._safe_text(message, label="Research status", maximum=240, required=False)
        now = self._now()
        with self._lock, self.connection:
            self._reject_full_text_legacy_write_locked(job_id)
            changed = self.connection.execute(
                "UPDATE workbench_research_job SET stage=?,message=?,completed_steps=?,"
                "candidate_count=?,evidence_count=?,updated_at=? WHERE job_id=? "
                "AND state='running' AND cancellation_requested=0 AND completed_steps<=?",
                (stage, value, int(completed_steps), int(candidate_count), int(evidence_count),
                 now, job_id, int(completed_steps)),
            ).rowcount
            if changed != 1:
                return False
            row = self.connection.execute(
                "SELECT total_steps FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
            self._append_research_event_locked(
                job_id, state="running", stage=stage, message=value,
                completed_steps=int(completed_steps), total_steps=int(row["total_steps"]),
                created_at=now,
            )
        return True

    def restart_research_checkpoint(self, job_id: str, *, checkpoint: Mapping[str, object] | None = None) -> bool:
        """Atomically replace stale findings and reset progress, retaining accounting."""
        if not _RESEARCH_JOB.fullmatch(job_id or ""):
            raise KeyError(job_id)
        now = self._now()
        message = "Source coverage changed or was not recorded. Repeating the evidence searches."
        encoded = json.dumps(dict(checkpoint or {}), ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 2_000_000:
            raise ValueError("research checkpoint is too large")
        with self._lock, self.connection:
            self._reject_full_text_legacy_write_locked(job_id)
            changed = self.connection.execute(
                "UPDATE workbench_research_job SET result_json=?,completed_steps=0,"
                "candidate_count=0,evidence_count=0,stage='searching',message=?,updated_at=? "
                "WHERE job_id=? AND state='running' AND cancellation_requested=0",
                (encoded, message, now, job_id),
            ).rowcount
            if changed != 1:
                return False
            row = self.connection.execute(
                "SELECT total_steps FROM workbench_research_job WHERE job_id=?", (job_id,),
            ).fetchone()
            self._append_research_event_locked(
                job_id, state="running", stage="searching", message=message,
                completed_steps=0, total_steps=int(row["total_steps"]), created_at=now,
            )
        return True

    def _reject_full_text_legacy_write_locked(self, job_id: str) -> None:
        row = self.connection.execute(
            "SELECT plan_json,result_json FROM workbench_research_job WHERE job_id=?", (job_id,),
        ).fetchone()
        if row is not None and is_full_text_synthesis(json.loads(row["plan_json"]), json.loads(row["result_json"])):
            raise WorkspaceProblem("Use the versioned full-text synthesis workflow to preserve its input and charged work.")

    def _validate_research_attempt_locked(self, job: ResearchJobRecord, *, stopping: bool = False) -> None:
        """Caller owns the write transaction; delayed workers cannot mutate a retry."""
        current = self.connection.execute(
            "SELECT * FROM workbench_research_job WHERE job_id=?", (job.job_id,),
        ).fetchone()
        if (current is None or current["matter_id"] != job.matter_id
                or current["actor_id"] != job.actor_id or current["state"] != "running"
                or current["attempts"] != job.attempts or current["worker_id"] != job.worker_id
                or (current["cancellation_requested"] and not stopping)):
            raise WorkspaceProblem("This synthesis attempt is no longer active.")
        if not stopping:
            self.membership(job.matter_id, job.actor_id)

    def checkpoint_research_job(
        self, job_id: str, result: Mapping[str, object], *,
        expected_job: ResearchJobRecord | None = None,
        validate_locked: Callable[[], None] | None = None,
    ) -> ResearchJobRecord:
        """Persist resumable pass output without marking the run complete."""

        encoded = json.dumps(dict(result), ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 2_000_000:
            raise ValueError("research checkpoint is too large")
        now = self._now()
        with self._lock, self.connection:
            if expected_job is not None:
                if expected_job.job_id != job_id:
                    raise WorkspaceProblem("The synthesis attempt does not match this job.")
                self.connection.execute("BEGIN IMMEDIATE")
                self._validate_research_attempt_locked(expected_job)
            current = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,),
            ).fetchone()
            if current is not None and (is_full_text_synthesis(json.loads(current["plan_json"]), json.loads(current["result_json"]))
                    or is_full_text_synthesis(None, result)):
                if expected_job is None or validate_locked is None:
                    raise WorkspaceProblem("Full-text synthesis checkpoints require a validated active attempt.")
                from .full_text_synthesis import validate_job_input
                current_job = self._research_job(current)
                validate_job_input(current_job)
                validate_job_input(current_job, result)
            if expected_job is not None:
                if validate_locked is not None:
                    validate_locked()
            changed = self.connection.execute(
                "UPDATE workbench_research_job SET result_json=?,updated_at=? WHERE job_id=? "
                "AND state='running' AND cancellation_requested=0",
                (encoded, now, job_id),
            ).rowcount
            if changed != 1:
                raise WorkspaceProblem("This research run is no longer active.")
            if expected_job is not None:
                state = result.get("hierarchical_synthesis", {})
                self.connection.execute(
                    "UPDATE workbench_research_job SET stage='synthesizing',completed_steps=1,message=? WHERE job_id=?",
                    (f"Saved {len(state.get('issue', []))} issue groups and {len(state.get('matter', []))} matter sections; "
                     f"{state.get('requests_spent', 0)} generation requests charged.", job_id),
                )
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._research_job(row)

    def research_cancellation_requested(self, job_id: str) -> bool:
        if not _RESEARCH_JOB.fullmatch(job_id or ""):
            raise KeyError(job_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT cancellation_requested,state FROM workbench_research_job WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return bool(row["cancellation_requested"] or row["state"] == "cancelled")

    def finish_research_job(self, job_id: str, result: Mapping[str, object], *,
            expected_job: ResearchJobRecord | None = None,
            validate_locked: Callable[[], None] | None = None) -> ResearchJobRecord:
        result = dict(result)
        result.pop("_retrieval_source_fingerprint", None)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 2_000_000:
            raise ValueError("research result is too large")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if expected_job is not None:
                if expected_job.job_id != job_id:
                    raise WorkspaceProblem("The synthesis attempt does not match this job.")
                self._validate_research_attempt_locked(expected_job)
                if validate_locked is not None:
                    validate_locked()
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if (is_full_text_synthesis(json.loads(row["plan_json"]), json.loads(row["result_json"]))
                    or is_full_text_synthesis(None, result)):
                if expected_job is None or validate_locked is None:
                    raise WorkspaceProblem("Full-text synthesis completion requires a validated active attempt.")
                from .full_text_synthesis import validate_job_input
                current_job = self._research_job(row)
                validate_job_input(current_job)
                validate_job_input(current_job, result)
            if row["state"] == "succeeded":
                return self._research_job(row)
            if row["state"] != "running":
                raise WorkspaceProblem("This research run is no longer active.")
            if int(row["cancellation_requested"]):
                return self.fail_research_job(job_id, "Research cancelled.")
            authorized = self.connection.execute(
                "SELECT 1 FROM workbench_effective_membership membership "
                "JOIN workbench_principal principal "
                "ON principal.principal_id=membership.principal_id "
                "JOIN workbench_matter_lifecycle lifecycle "
                "ON lifecycle.matter_id=membership.matter_id "
                "WHERE membership.matter_id=? AND membership.principal_id=? "
                "AND membership.state='active' AND principal.active=1 "
                "AND lifecycle.state='active'",
                (row["matter_id"], row["actor_id"]),
            ).fetchone()
            if authorized is None:
                raise WorkspaceProblem(
                    "Access to this matter was removed before the investigation result could be saved."
                )
            if row["source_set_id"] is not None:
                try:
                    scope = self.source_set_document_ids(row["matter_id"], row["source_set_id"])
                except KeyError as exc:
                    raise WorkspaceProblem("The selected source set is no longer available.") from exc
                evidence = result.get("evidence")
                if not isinstance(evidence, list) or any(
                    not isinstance(item, Mapping) or item.get("document_id") not in scope
                    for item in evidence
                ):
                    raise WorkspaceProblem(
                        "A cited source left the selected set before the investigation could be saved."
                    )
            result_message_id = row["result_message_id"]
            conversation_id = row["conversation_id"]
            if conversation_id is not None and result_message_id is None:
                conversation = self.connection.execute(
                    "SELECT 1 FROM workbench_conversation c "
                    "JOIN workbench_conversation_organization o "
                    "ON o.conversation_id=c.conversation_id "
                    "WHERE c.conversation_id=? AND c.matter_id=? AND o.state='active'",
                    (conversation_id, row["matter_id"]),
                ).fetchone()
                if conversation is None:
                    raise WorkspaceProblem(
                        "The conversation for this research result is no longer available."
                    )
                answer = result.get("answer")
                payload = dict(answer) if isinstance(answer, Mapping) else {}
                summary = self._derived_text(
                    str(result.get("summary") or "Investigation complete."),
                    label="Research result",
                    maximum=20_000,
                    multiline=True,
                )
                if payload.get("kind") not in {"generated", "not-supported"}:
                    payload = {
                        "kind": "not-supported",
                        "missing_information": summary,
                    }
                payload.update(
                    {
                        "workflow": "research",
                        "research_job_id": job_id,
                        "research_title": str(row["title"]),
                        "coverage": dict(result.get("coverage") or {})
                        if isinstance(result.get("coverage"), Mapping)
                        else {},
                    }
                )
                payload_json = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                )
                if len(payload_json) > 100_000:
                    raise ValueError("research conversation payload is too large")
                result_message_id = f"message-{uuid.uuid4().hex}"
                ordinal = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_message "
                        "WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()[0]
                )
                self.connection.execute(
                    "INSERT INTO workbench_message(message_id,conversation_id,ordinal,role,"
                    "content,payload_json,created_at) VALUES (?,?,?,'assistant',?,?,?)",
                    (
                        result_message_id,
                        conversation_id,
                        ordinal,
                        summary,
                        payload_json,
                        now,
                    ),
                )
                self.connection.execute(
                    "UPDATE workbench_conversation SET updated_at=? "
                    "WHERE conversation_id=? AND matter_id=?",
                    (now, conversation_id, row["matter_id"]),
                )
            self.connection.execute(
                "UPDATE workbench_research_job SET state='succeeded',stage='complete',"
                "result_json=?,completed_steps=total_steps,message='Research ready.',worker_id=NULL,"
                "result_message_id=?,finished_at=?,updated_at=? "
                "WHERE job_id=? AND state='running'",
                (encoded, result_message_id, now, now, job_id),
            )
            completed = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
            self._append_research_event_locked(
                job_id, state="succeeded", stage="complete", message="Research ready.",
                completed_steps=int(completed["completed_steps"]), total_steps=int(completed["total_steps"]),
                created_at=now,
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, completed["matter_id"]),
            )
        return self._research_job(completed)

    def fail_research_job(self, job_id: str, message: str, *,
            expected_job: ResearchJobRecord | None = None) -> ResearchJobRecord:
        value = self._safe_text(message, label="Research status", maximum=240, required=False)
        now = self._now()
        with self._lock, self.connection:
            if expected_job is None:
                self._reject_full_text_legacy_write_locked(job_id)
            if expected_job is not None:
                if expected_job.job_id != job_id:
                    raise WorkspaceProblem("The synthesis attempt does not match this job.")
                self.connection.execute("BEGIN IMMEDIATE")
                self._validate_research_attempt_locked(expected_job, stopping=True)
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] != "running":
                return self._research_job(row)
            cancelled = bool(row["cancellation_requested"]) or value == "Research cancelled."
            state, stage = ("cancelled", "cancelled") if cancelled else ("failed", "failed")
            final_message = "Research cancelled." if cancelled else (value or "Research could not be completed.")
            result = json.loads(row["result_json"] or "{}")
            result["stop_reason"] = state
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            self.connection.execute(
                "UPDATE workbench_research_job SET result_json=?,state=?,stage=?,message=?,worker_id=NULL,"
                "finished_at=?,updated_at=? WHERE job_id=? AND state='running'",
                (encoded, state, stage, final_message, now, now, job_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
            self._append_research_event_locked(
                job_id, state=state, stage=stage, message=final_message,
                completed_steps=int(updated["completed_steps"]), total_steps=int(updated["total_steps"]),
                created_at=now,
            )
        return self._research_job(updated)

    def cancel_research_job(self, matter_id: str, actor_id: str, job_id: str) -> ResearchJobRecord:
        job = self.research_job(matter_id, actor_id, job_id)
        if job.actor_id != actor_id:
            raise WorkspaceProblem("Only the reviewer who started this run can cancel it.")
        now = self._now()
        with self._lock, self.connection:
            # Take the database write lock before reloading the checkpoint.
            # Otherwise another store can save a charged request or node after
            # this read and cancellation can overwrite it with stale JSON.
            self.connection.execute("BEGIN IMMEDIATE")
            job = self.research_job(matter_id, actor_id, job_id)
            if job.state == "queued":
                state, stage, message, finished = "cancelled", "cancelled", "Research cancelled before it started.", now
            elif job.state == "running":
                state, stage, message, finished = "running", "cancelling", "Cancelling after the current research step.", None
            elif job.state == "cancelled":
                return job
            else:
                raise WorkspaceProblem("This research run can no longer be cancelled.")
            result = dict(job.result)
            if state == "cancelled":
                result["stop_reason"] = "cancelled_before_start"
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            self.connection.execute(
                "UPDATE workbench_research_job SET result_json=?,state=?,stage=?,message=?,"
                "cancellation_requested=1,finished_at=?,updated_at=? WHERE job_id=?",
                (encoded, state, stage, message, finished, now, job_id),
            )
            self._append_research_event_locked(
                job_id, state=state, stage=stage, message=message,
                completed_steps=job.completed_steps, total_steps=job.total_steps, created_at=now,
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._research_job(row)

    def retry_research_job(self, matter_id: str, actor_id: str, job_id: str, *, additional_passes: int = 0, expected_passes: int | None = None, checkpoint_is_stale: Callable[[ResearchJobRecord], bool] | None = None, validate_locked: Callable[[ResearchJobRecord], None] | None = None) -> ResearchJobRecord:
        """Retry with an optional source-frozen locator validator for current data."""
        from .review_budget import ReviewBudget
        if type(additional_passes) is not int or not 0 <= additional_passes <= 5:
            raise WorkspaceProblem("Choose between one and five additional passes.")
        job = self.research_job(matter_id, actor_id, job_id)
        if job.actor_id != actor_id:
            raise WorkspaceProblem("Only the reviewer who started this run can retry it.")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor_id)
            current = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=? "
                "AND matter_id=? AND actor_id=?",
                (job_id, matter_id, actor_id),
            ).fetchone()
            if current is None:
                raise KeyError(job_id)
            full_text = is_full_text_synthesis(json.loads(current["plan_json"]), json.loads(current["result_json"]))
            if full_text:
                from .full_text_synthesis import validate_job_input
                validate_job_input(self._research_job(current))
                if validate_locked is None:
                    raise WorkspaceProblem("Resuming synthesis requires validation of its frozen input.")
                if additional_passes or current["state"] == "succeeded":
                    raise WorkspaceProblem("This synthesis cannot gain new scope or budget. Start a new synthesis from a terminal review.")
            if validate_locked is not None:
                validate_locked(self._research_job(current))
            if current["state"] in {"queued", "running"}:
                if additional_passes:
                    queued_plan = json.loads(current["plan_json"])
                    if (type(expected_passes) is not int or queued_plan.get("retry_request") !=
                            {"additional_passes": additional_passes, "expected_passes": expected_passes}):
                        raise WorkspaceProblem("This investigation is already active with different extension details. Reload before adding more work.")
                return self._research_job(current)
            if current["state"] not in {"failed", "cancelled", "succeeded"}:
                raise WorkspaceProblem(
                    "This completed research run does not need to be retried."
                )
            # Completed results remain immutable targets for conversation links.
            continuation_key = "continuation-" + job_id
            if current["state"] == "succeeded":
                existing = self.connection.execute(
                    "SELECT * FROM workbench_research_job WHERE matter_id=? AND actor_id=? AND idempotency_key=?",
                    (matter_id, actor_id, continuation_key),
                ).fetchone()
                if existing is not None:
                    parent_plan = json.loads(current["plan_json"])
                    continuation_plan = json.loads(existing["plan_json"])
                    extension_index = len(parent_plan.get("extensions", []))
                    extensions = continuation_plan.get("extensions", [])
                    recorded_request = continuation_plan.get("continuation_request")
                    if recorded_request is None and len(extensions) > extension_index:
                        recorded_request = {"additional_passes": extensions[extension_index].get("additional_passes"),
                                            "expected_passes": parent_plan.get("budget", {}).get("effective", {}).get("passes")}
                    if (type(expected_passes) is not int
                            or recorded_request != {"additional_passes": additional_passes, "expected_passes": expected_passes}):
                        raise WorkspaceProblem("This run already has a continuation with different extension details. Reload before adding more work.")
                    return self._research_job(existing)
            if current["source_set_id"] is not None:
                try:
                    scope = self.source_set_document_ids(matter_id, current["source_set_id"])
                except KeyError as exc:
                    raise WorkspaceProblem("This source set is no longer available. Start a new investigation with an available scope.") from exc
                if not scope:
                    raise WorkspaceProblem("This source set is empty. Add sources before rebuilding or extending the investigation.")
            conversation_id = current["conversation_id"]
            if conversation_id is not None:
                active_answer = self.connection.execute(
                    "SELECT 1 FROM workbench_answer_job WHERE conversation_id=? "
                    "AND matter_id=? AND state IN ('queued','running') LIMIT 1",
                    (conversation_id, matter_id),
                ).fetchone()
                active_research = self.connection.execute(
                    "SELECT 1 FROM workbench_research_job WHERE conversation_id=? "
                    "AND matter_id=? AND job_id<>? "
                    "AND state IN ('queued','running') LIMIT 1",
                    (conversation_id, matter_id, job_id),
                ).fetchone()
                if active_answer is not None or active_research is not None:
                    raise WorkspaceProblem(
                        "This conversation already has review work in progress."
                    )
            plan = json.loads(current["plan_json"])
            result = json.loads(current["result_json"])
            adaptive = plan.get("planner_version") == 1
            stale = adaptive and (
                checkpoint_is_stale(self._research_job(current)) if checkpoint_is_stale is not None else
                bool(result.get("passes")) and result.get("retrieval_source_fingerprint") != self.source_availability_fingerprint(matter_id, current["source_set_id"])
            )
            if current["state"] == "succeeded" and not additional_passes:
                if not stale:
                    raise WorkspaceProblem("This completed research run does not need to be retried.")
                limits = plan["budget"]["effective"]
                if type(expected_passes) is not int or expected_passes != limits["passes"]:
                    raise WorkspaceProblem("The investigation budget changed. Reload before rebuilding its checkpoint.")
                spent = len(result.get("passes", [])) + int(result.get("discarded_passes", 0))
                if spent >= limits["passes"] or float(result.get("search_elapsed_seconds", 0)) >= limits["search_seconds"]:
                    raise WorkspaceProblem("Rebuilding needs additional search budget. Choose an extension or start a new investigation.")
            if additional_passes and not adaptive:
                raise WorkspaceProblem("Start a new investigation to use evidence-driven searches.")
            if full_text:
                # The versioned input and hierarchy carry the immutable basis,
                # first-start deadline, saved nodes and lifetime charged calls.
                result.pop("stop_reason", None)
                completed, total = int(current["completed_steps"]), int(current["total_steps"])
            elif adaptive:
                plan["retry_request"] = {"additional_passes": additional_passes, "expected_passes": expected_passes}
                if additional_passes:
                    if not stale and not result.get("pending_searches") and not result.get("seed_search_pending"):
                        raise WorkspaceProblem("No unsearched source-backed proposals remain. Start a new question.")
                    limits = dict(plan["budget"]["effective"])
                    if type(expected_passes) is not int or expected_passes != limits["passes"]:
                        raise WorkspaceProblem("The investigation budget changed. Reload before adding more work.")
                    limits["passes"] += additional_passes
                    limits["search_seconds"] += additional_passes * 180
                    try:
                        budget = ReviewBudget(**limits)
                    except ValueError as exc:
                        raise WorkspaceProblem("An investigation can use at most 15 passes and 45 search minutes. Start a new investigation.") from exc
                    if not stale and len(result.get("evidence", [])) >= budget.unique_evidence:
                        raise WorkspaceProblem("The evidence budget is full. Start a new investigation.")
                    if float(result.get("search_elapsed_seconds", 0)) >= budget.search_seconds:
                        raise WorkspaceProblem("The added search time is already exhausted. Choose a larger extension or start a new investigation.")
                    plan["budget"] = budget.metadata()
                    plan.setdefault("extensions", []).append({"additional_passes": additional_passes,
                        "additional_seconds": additional_passes * 180, "requested_at": now})
                    result["no_new_count"] = 0
                    result.pop("search_stop_reason", None)
                    result["budget"] = {**result.get("budget", {}), "effective": limits, "requested": limits, "stop_reason": "running"}
                result.pop("stop_reason", None)
                completed = len(result.get("passes", [])) + int(result.get("discarded_passes", 0))
                total = plan["budget"]["effective"]["passes"] + 2
                # The continuation synthesizes a new result; completed parent
                # rows and their conversation message targets remain unchanged.
                for key in ("summary", "answer", "coverage", "gaps"):
                    result.pop(key, None)
            else:
                plan, result, completed, total = {}, {}, 0, 0
            if current["state"] == "succeeded":
                parent_job_id = job_id
                job_id = f"research-job-{uuid.uuid4().hex}"
                plan["continued_from_job_id"] = parent_job_id
                plan["continuation_request"] = {"additional_passes": additional_passes, "expected_passes": expected_passes}
                self.connection.execute(
                    "INSERT INTO workbench_research_job(job_id,matter_id,actor_id,idempotency_key,"
                    "question,title,source_set_id,conversation_id,state,stage,message,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,'queued','queued','Investigation continued from checkpoint.',?,?)",
                    (job_id, matter_id, actor_id, continuation_key, current["question"], current["title"],
                     current["source_set_id"], conversation_id, now, now),
                )
            self.connection.execute(
                "UPDATE workbench_research_job SET state='queued',stage='queued',"
                "message='Investigation queued from checkpoint.',worker_id=NULL,cancellation_requested=0,"
                "plan_json=?,result_json=?,total_steps=?,completed_steps=?,"
                "candidate_count=?,evidence_count=?,result_message_id=NULL,started_at=NULL,finished_at=NULL,updated_at=? "
                "WHERE job_id=?", (json.dumps(plan), json.dumps(result), total, completed,
                                   result.get("full_text_synthesis_input", {}).get("counts", {}).get("candidate_findings", result.get("candidate_count", 0)),
                                   len(result.get("evidence", [])), now, job_id)
            )
            self._append_research_event_locked(
                job_id, state="queued", stage="queued", message="Investigation queued from checkpoint.",
                completed_steps=completed, total_steps=total, created_at=now,
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_research_job WHERE job_id=?", (job_id,)
            ).fetchone()
        return self._research_job(row)

    def research_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM workbench_research_job GROUP BY state"
            ).fetchall()
        counts = {state: 0 for state in ("queued", "running", "succeeded", "failed", "cancelled")}
        counts.update({row["state"]: int(row["count"]) for row in rows})
        return counts

    # Criterion-driven Full Review -----------------------------------------

    def _criterion_select(self) -> str:
        return (
            "SELECT c.*,COUNT(v.criterion_version_id) AS version_count,"
            "COALESCE(MAX(v.version_number),0) AS current_version_number,"
            "COALESCE((SELECT latest.criterion_version_id FROM workbench_review_criterion_version latest "
            "WHERE latest.matter_id=c.matter_id AND latest.criterion_id=c.criterion_id "
            "ORDER BY latest.version_number DESC LIMIT 1),'') AS current_version_id "
            "FROM workbench_review_criterion c LEFT JOIN workbench_review_criterion_version v "
            "ON v.matter_id=c.matter_id AND v.criterion_id=c.criterion_id "
        )

    def create_review_criterion(
        self, matter_id: str, actor_id: str, *, title: str, instructions: str,
        include_guidance: str = "", exclude_guidance: str = "",
    ) -> tuple[ReviewCriterionRecord, ReviewCriterionVersionRecord]:
        actor = self.membership(matter_id, actor_id).principal_id
        heading = self._safe_text(title, label="Criterion title", maximum=160)
        rule = self._safe_text(
            instructions, label="Review instructions", maximum=8_000, multiline=True
        )
        include = self._safe_text(
            include_guidance, label="Include guidance", maximum=4_000,
            required=False, multiline=True,
        )
        exclude = self._safe_text(
            exclude_guidance, label="Exclude guidance", maximum=4_000,
            required=False, multiline=True,
        )
        criterion_id = f"review-criterion-{uuid.uuid4().hex}"
        version_id = f"review-version-{uuid.uuid4().hex}"
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO workbench_review_criterion(criterion_id,matter_id,title,created_by,"
                "created_at,updated_by,updated_at) VALUES (?,?,?,?,?,?,?)",
                (criterion_id, matter_id, heading, actor, now, actor, now),
            )
            self.connection.execute(
                "INSERT INTO workbench_review_criterion_version(criterion_version_id,criterion_id,"
                "matter_id,version_number,instructions,include_guidance,exclude_guidance,"
                "created_by,created_at) VALUES (?,?,?,1,?,?,?,?,?)",
                (version_id, criterion_id, matter_id, rule, include, exclude, actor, now),
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (now, matter_id)
            )
        return self.review_criterion(matter_id, criterion_id), self.review_criterion_version(
            matter_id, version_id
        )

    def add_review_criterion_version(
        self, matter_id: str, actor_id: str, criterion_id: str, *, title: str,
        instructions: str, include_guidance: str = "", exclude_guidance: str = "",
    ) -> ReviewCriterionVersionRecord:
        actor = self.membership(matter_id, actor_id).principal_id
        if not _REVIEW_CRITERION.fullmatch(criterion_id or ""):
            raise KeyError(criterion_id)
        heading = self._safe_text(title, label="Criterion title", maximum=160)
        rule = self._safe_text(
            instructions, label="Review instructions", maximum=8_000, multiline=True
        )
        include = self._safe_text(
            include_guidance, label="Include guidance", maximum=4_000,
            required=False, multiline=True,
        )
        exclude = self._safe_text(
            exclude_guidance, label="Exclude guidance", maximum=4_000,
            required=False, multiline=True,
        )
        version_id = f"review-version-{uuid.uuid4().hex}"
        now = self._now()
        with self._lock, self.connection:
            current = self.connection.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM workbench_review_criterion_version "
                "WHERE matter_id=? AND criterion_id=?", (matter_id, criterion_id)
            ).fetchone()
            criterion = self.connection.execute(
                "SELECT 1 FROM workbench_review_criterion WHERE matter_id=? AND criterion_id=?",
                (matter_id, criterion_id),
            ).fetchone()
            if criterion is None:
                raise KeyError(criterion_id)
            version_number = int(current[0])
            self.connection.execute(
                "INSERT INTO workbench_review_criterion_version(criterion_version_id,criterion_id,"
                "matter_id,version_number,instructions,include_guidance,exclude_guidance,"
                "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (version_id, criterion_id, matter_id, version_number, rule, include, exclude, actor, now),
            )
            self.connection.execute(
                "UPDATE workbench_review_criterion SET title=?,updated_by=?,updated_at=? "
                "WHERE matter_id=? AND criterion_id=?",
                (heading, actor, now, matter_id, criterion_id),
            )
        return self.review_criterion_version(matter_id, version_id)

    def review_criteria(self, matter_id: str, actor_id: str) -> tuple[ReviewCriterionRecord, ...]:
        self.membership(matter_id, actor_id)
        with self._lock:
            rows = self.connection.execute(
                self._criterion_select() + "WHERE c.matter_id=? GROUP BY c.criterion_id "
                "ORDER BY c.updated_at DESC,c.criterion_id", (matter_id,)
            ).fetchall()
        return tuple(self._review_criterion(row) for row in rows)

    def review_criterion(self, matter_id: str, criterion_id: str, *, _snapshot: sqlite3.Connection | None = None) -> ReviewCriterionRecord:
        if not _REVIEW_CRITERION.fullmatch(criterion_id or ""):
            raise KeyError(criterion_id)
        with self._lock:
            row = (_snapshot if _snapshot is not None else self.connection).execute(
                self._criterion_select() + "WHERE c.matter_id=? AND c.criterion_id=? "
                "GROUP BY c.criterion_id", (matter_id, criterion_id)
            ).fetchone()
        if row is None:
            raise KeyError(criterion_id)
        return self._review_criterion(row)

    def review_criterion_version(
        self, matter_id: str, criterion_version_id: str, *, _snapshot: sqlite3.Connection | None = None
    ) -> ReviewCriterionVersionRecord:
        if not _REVIEW_CRITERION_VERSION.fullmatch(criterion_version_id or ""):
            raise KeyError(criterion_version_id)
        with self._lock:
            row = (_snapshot if _snapshot is not None else self.connection).execute(
                "SELECT * FROM workbench_review_criterion_version WHERE matter_id=? "
                "AND criterion_version_id=?", (matter_id, criterion_version_id)
            ).fetchone()
        if row is None:
            raise KeyError(criterion_version_id)
        return self._review_criterion_version(row)

    def review_criterion_versions(
        self, matter_id: str, criterion_id: str
    ) -> tuple[ReviewCriterionVersionRecord, ...]:
        self.review_criterion(matter_id, criterion_id)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_review_criterion_version WHERE matter_id=? "
                "AND criterion_id=? ORDER BY version_number DESC",
                (matter_id, criterion_id),
            ).fetchall()
        return tuple(self._review_criterion_version(row) for row in rows)

    def _append_review_event_locked(
        self, run_id: str, *, state: str, stage: str, message: str,
        reviewed_count: int, snapshot_count: int, created_at: str,
    ) -> None:
        ordinal = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM workbench_review_event WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        self.connection.execute(
            "INSERT INTO workbench_review_event(run_id,ordinal,state,stage,reviewed_count,"
            "snapshot_count,message,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, ordinal, state, stage, reviewed_count, snapshot_count, message, created_at),
        )

    def queue_review_run(
        self, matter_id: str, actor_id: str, criterion_version_id: str, *,
        run_kind: str, source_set_id: str | None = None, review_mode: str = "selected_passages",
        text_policy=None,
    ) -> ReviewRunRecord:
        actor = self.membership(matter_id, actor_id).principal_id
        if review_mode not in {"selected_passages", "full_text"} or (review_mode == "full_text" and run_kind != "full"):
            raise WorkspaceProblem("Choose selected passages or all extracted text for the full population.")
        if run_kind not in {"sample", "full"}:
            raise WorkspaceProblem("Choose a sample or every-source run.")
        version = self.review_criterion_version(matter_id, criterion_version_id)
        scope_id = (source_set_id or "").strip() or None
        if scope_id is not None and not self.source_set_document_ids(matter_id, scope_id):
            raise WorkspaceProblem("That source set is empty or no longer available.")
        run_id = f"review-run-{uuid.uuid4().hex}"
        now = self._now()
        from .full_text_review import DEFAULT_TEXT_POLICY, TextReviewPolicy
        policy = text_policy or DEFAULT_TEXT_POLICY
        if not isinstance(policy, TextReviewPolicy):
            raise ValueError("Invalid full-text policy")
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor)
            scope_clause = (
                " AND EXISTS (SELECT 1 FROM workbench_source_set_item si WHERE "
                "si.matter_id=c.matter_id AND si.document_id=c.document_id AND si.source_set_id=?)"
                if scope_id is not None else ""
            )
            if review_mode == "full_text":
                from .full_text_review_budget import admit_locked, source_count_locked
                admit_locked(self.connection, matter_id, actor, policy)
                source_count_locked(self.connection, matter_id, policy, scope_clause=scope_clause,
                                    scope_parameters=(scope_id,) if scope_id else ())
            self.connection.execute(
                "INSERT INTO workbench_review_run(run_id,matter_id,actor_id,criterion_id,"
                "criterion_version_id,source_set_id,run_kind,state,stage,message,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'queued','snapshotting','Freezing the searchable review population.',?,?)",
                (run_id, matter_id, actor, version.criterion_id, criterion_version_id,
                 scope_id, run_kind, now, now),
            )
            scope_clause = (
                " AND EXISTS (SELECT 1 FROM workbench_source_set_item si WHERE "
                "si.matter_id=c.matter_id AND si.document_id=c.document_id AND si.source_set_id=?)"
                if scope_id is not None else ""
            )
            basis_parameters: tuple[object, ...] = (matter_id,)
            if scope_id is not None:
                basis_parameters += (scope_id,)
            missing_basis = self.connection.execute(
                "SELECT 1 FROM workbench_source_catalog c "
                "WHERE c.matter_id=? AND c.source_state='ready' "
                "AND c.content_basis_digest=''" + scope_clause + " LIMIT 1",
                basis_parameters,
            ).fetchone()
            if missing_basis is not None:
                raise WorkspaceProblem(
                    "One or more searchable sources need exact source-content tracking. "
                    "Reopen the matter to refresh its sources, then retry this review."
                )
            parameters: tuple[object, ...] = (run_id, now, now, matter_id)
            if scope_id is not None:
                parameters += (scope_id,)
            limit_clause = " ORDER BY c.document_id LIMIT 50" if run_kind == "sample" else " ORDER BY c.document_id"
            self.connection.execute(
                "INSERT INTO workbench_review_decision(run_id,matter_id,ordinal,document_id,"
                "source_version_id,source_basis_digest,action_token,source_name,source_kind,"
                "validation_sample,created_at,updated_at) "
                "SELECT ?,c.matter_id,ROW_NUMBER() OVER (ORDER BY c.document_id),c.document_id,"
                "c.version_id,c.content_basis_digest,c.action_token,c.display_name,c.kind,?, ?, ? "
                "FROM workbench_source_catalog c "
                "WHERE c.matter_id=? AND c.source_state='ready'" + scope_clause + limit_clause,
                (parameters[0], 1 if run_kind == "sample" else 0, *parameters[1:]),
            )
            if review_mode == "full_text":
                from .full_text_review import FullTextReviewLedger
                try:
                    FullTextReviewLedger(self).enable_locked(run_id, policy=policy, scope_clause=scope_clause,
                        scope_parameters=(scope_id,) if scope_id else ())
                except sqlite3.IntegrityError as exc:
                    from .full_text_review_budget import sql_limit
                    limit = sql_limit(exc)
                    if limit:
                        raise limit from exc
                    raise
            snapshot_count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_review_decision WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            if snapshot_count == 0:
                raise WorkspaceProblem("No searchable source is available for this review.")
            if run_kind == "full":
                sample_size = min(snapshot_count, 50)
                # The run id is a durable, non-content seed: the sample is
                # unpredictable before the run, reproducible afterward, and
                # is not biased toward filename/document-id intervals.
                sample_ordinals = set(
                    random.Random(run_id).sample(
                        range(1, snapshot_count + 1), sample_size
                    )
                )
                self.connection.executemany(
                    "UPDATE workbench_review_decision SET validation_sample=1 WHERE run_id=? AND ordinal=?",
                    [(run_id, ordinal) for ordinal in sorted(sample_ordinals)],
                )
            self.connection.execute(
                "UPDATE workbench_review_run SET snapshot_count=?,message=?,updated_at=? WHERE run_id=?",
                (snapshot_count, f"{snapshot_count:,} searchable source(s) frozen for review.", now, run_id),
            )
            self._append_review_event_locked(
                run_id, state="queued", stage="snapshotting",
                message=f"{snapshot_count:,} searchable source(s) frozen for review.",
                reviewed_count=0, snapshot_count=snapshot_count, created_at=now,
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (now, matter_id)
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._review_run(row)

    def review_run(
        self,
        matter_id: str,
        actor_id: str,
        run_id: str,
        *,
        administrator_override: bool = False,
        _snapshot: sqlite3.Connection | None = None,
    ) -> ReviewRunRecord:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        if not _REVIEW_RUN.fullmatch(run_id or ""):
            raise KeyError(run_id)
        with self._lock:
            row = (_snapshot if _snapshot is not None else self.connection).execute(
                "SELECT * FROM workbench_review_run WHERE matter_id=? AND run_id=?",
                (matter_id, run_id),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._review_run(row)

    def review_runs(
        self,
        matter_id: str,
        actor_id: str,
        *,
        limit: int = 100,
        administrator_override: bool = False,
    ) -> tuple[ReviewRunRecord, ...]:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE matter_id=? "
                "ORDER BY created_at DESC,run_id DESC LIMIT ?", (matter_id, bounded)
            ).fetchall()
        return tuple(self._review_run(row) for row in rows)

    def review_runs_for_final_bundle(
        self,
        matter_id: str,
        actor_id: str,
        *,
        maximum: int,
        administrator_override: bool = False,
    ) -> tuple[ReviewRunRecord, ...]:
        """Return at most one over the explicit final-bundle ledger bound."""

        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        bounded = int(maximum)
        if bounded < 1 or bounded > 500:
            raise ValueError("final-bundle every-source-check bound is invalid")
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE matter_id=? "
                "ORDER BY created_at DESC,run_id DESC LIMIT ?",
                (matter_id, bounded + 1),
            ).fetchall()
        return tuple(self._review_run(row) for row in rows)

    def review_runs_for_actor(
        self, actor_id: str, *, limit: int = 150
    ) -> tuple[ReviewRunRecord, ...]:
        """Return recent shared full-review work across active memberships."""

        actor = self._safe_text(actor_id, label="Actor identity", maximum=100)
        bounded = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self.connection.execute(
                "SELECT j.* FROM workbench_review_run j "
                "JOIN workbench_effective_membership mm ON mm.matter_id=j.matter_id "
                "AND mm.principal_id=? AND mm.state='active' "
                "JOIN workbench_matter_lifecycle ml ON ml.matter_id=j.matter_id "
                "AND ml.state='active' "
                "ORDER BY j.updated_at DESC,j.run_id DESC LIMIT ?",
                (actor, bounded),
            ).fetchall()
        return tuple(self._review_run(row) for row in rows)

    def recover_running_review_runs(self) -> int:
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            rows = self.connection.execute(
                "SELECT run_id,cancellation_requested,reviewed_count,snapshot_count "
                "FROM workbench_review_run WHERE state='running' AND matter_id IN (SELECT matter_id FROM workbench_matter_lifecycle WHERE state='active') ORDER BY created_at,run_id"
            ).fetchall()
            for row in rows:
                cancelled = bool(row["cancellation_requested"])
                state = "cancelled" if cancelled else "queued"
                stage = "cancelled" if cancelled else "reviewing"
                message = (
                    "Cancelled during restart recovery." if cancelled
                    else "Queued again after restart. Saved source decisions will be resumed."
                )
                if not cancelled:
                    from .full_text_review_budget import charge_control_locked, TextReviewLimit
                    try:
                        charge_control_locked(self.connection, row["run_id"])
                    except TextReviewLimit as exc:
                        state = stage = "failed"
                        message = str(exc)
                        self.connection.execute("UPDATE workbench_text_review_budget SET limit_reason=? WHERE run_id=?", (message,row["run_id"]))
                self.connection.execute(
                    "UPDATE workbench_review_run SET state=?,stage=?,message=?,worker_id=NULL,"
                    "started_at=NULL,finished_at=?,updated_at=? WHERE run_id=? AND state='running'",
                    (state, stage, message, now if state in {"cancelled", "failed"} else None, now, row["run_id"]),
                )
                self._append_review_event_locked(
                    row["run_id"], state=state, stage=stage, message=message,
                    reviewed_count=int(row["reviewed_count"]), snapshot_count=int(row["snapshot_count"]),
                    created_at=now,
                )
        return len(rows)

    def claim_review_run(self, worker_id: str) -> ReviewRunRecord | None:
        worker = self._safe_text(worker_id, label="Worker identity", maximum=100)
        now = self._now()
        lane_last_claim = (
            "(SELECT MAX(h.last_claimed_at) FROM workbench_review_run h "
            "WHERE h.actor_id=q.actor_id AND h.matter_id=q.matter_id)"
        )
        with self._lock, self.connection:
            selected = self.connection.execute(
                "SELECT q.run_id FROM workbench_review_run q JOIN workbench_matter_lifecycle ml "
                "ON ml.matter_id=q.matter_id WHERE q.state='queued' AND ml.state='active' "
                f"ORDER BY CASE WHEN {lane_last_claim} IS NULL THEN 0 ELSE 1 END,"
                f"{lane_last_claim},q.created_at,q.run_id LIMIT 1"
            ).fetchone()
            if selected is None:
                return None
            changed = self.connection.execute(
                "UPDATE workbench_review_run SET state='running',stage='reviewing',"
                "attempts=attempts+1,worker_id=?,cancellation_requested=0,"
                "message='Reviewing the frozen source population.',started_at=COALESCE(started_at,?),"
                "last_claimed_at=?,finished_at=NULL,updated_at=? WHERE run_id=? AND state='queued'",
                (worker, now, now, now, selected["run_id"]),
            ).rowcount
            if changed != 1:
                return None
            row = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (selected["run_id"],)
            ).fetchone()
            self._append_review_event_locked(
                selected["run_id"], state="running", stage="reviewing",
                message="Reviewing the frozen source population.",
                reviewed_count=int(row["reviewed_count"]), snapshot_count=int(row["snapshot_count"]),
                created_at=now,
            )
        return self._review_run(row)

    def next_review_decision(self, run_id: str) -> ReviewDecisionRecord | None:
        pending = self.pending_review_decisions(run_id, limit=1)
        return pending[0] if pending else None

    def pending_review_decisions(
        self, run_id: str, *, limit: int = 2
    ) -> tuple[ReviewDecisionRecord, ...]:
        if not _REVIEW_RUN.fullmatch(run_id or ""):
            raise KeyError(run_id)
        bounded_limit = min(max(int(limit), 1), 8)
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM workbench_review_decision WHERE run_id=? AND machine_decision='pending' "
                "ORDER BY ordinal LIMIT ?", (run_id, bounded_limit)
            ).fetchall()
        return tuple(self._review_decision(row) for row in rows)

    def review_cancellation_requested(self, run_id: str) -> bool:
        with self._lock:
            row = self.connection.execute(
                "SELECT cancellation_requested,state FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return bool(row["cancellation_requested"] or row["state"] == "cancelled")

    def record_review_decision(
        self, run_id: str, document_id: str, *, decision: str, rationale: str,
        citations: Sequence[Mapping[str, object]] = (), error_message: str = "",
        expected_attempt: int | None = None,
    ) -> ReviewRunRecord:
        if decision not in {"included", "excluded", "needs_attention"}:
            raise ValueError("invalid review decision")
        reason = self._derived_text(
            rationale, label="Decision rationale", maximum=4_000, required=False, multiline=True
        )
        error = self._safe_text(
            error_message, label="Decision error", maximum=500, required=False, multiline=True
        )
        prepared_citations = [dict(item) for item in citations]
        if len(prepared_citations) > 12:
            raise ValueError("review citations are too large")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            frozen = self.connection.execute(
                "SELECT item.matter_id,item.source_version_id,item.source_basis_digest,item.updated_at,"
                "run.actor_id,run.state,run.attempts,run.cancellation_requested,catalog.version_id AS current_version,"
                "catalog.content_basis_digest AS current_basis,catalog.source_state "
                "FROM workbench_review_decision item "
                "JOIN workbench_review_run run ON run.run_id=item.run_id "
                "LEFT JOIN workbench_source_catalog catalog "
                "ON catalog.matter_id=item.matter_id "
                "AND catalog.document_id=item.document_id "
                "WHERE item.run_id=? AND item.document_id=?",
                (run_id, document_id),
            ).fetchone()
            if frozen is None:
                raise KeyError(document_id)
            if expected_attempt is not None and (frozen["attempts"] != expected_attempt or frozen["state"] != "running" or frozen["cancellation_requested"]):
                raise WorkspaceProblem("This review attempt is no longer active.")
            now = self._review_decision_time(frozen["updated_at"])
            if frozen["state"] == "running":
                authorized = self.connection.execute(
                    "SELECT 1 FROM workbench_effective_membership membership "
                    "JOIN workbench_principal principal "
                    "ON principal.principal_id=membership.principal_id "
                    "JOIN workbench_matter_lifecycle lifecycle "
                    "ON lifecycle.matter_id=membership.matter_id "
                    "WHERE membership.matter_id=? AND membership.principal_id=? "
                    "AND membership.state='active' AND principal.active=1 "
                    "AND lifecycle.state='active'",
                    (frozen["matter_id"], frozen["actor_id"]),
                ).fetchone()
                if authorized is None:
                    raise WorkspaceProblem(
                        "Access to this matter was removed during the every-source check."
                    )
            invalid_locator = any(
                citation.get("matter_id") != frozen["matter_id"]
                or citation.get("document_id") != document_id
                or citation.get("source_version_id") != frozen["source_version_id"]
                for citation in prepared_citations
            )
            source_changed = (
                frozen["current_version"] != frozen["source_version_id"]
                or frozen["source_state"] != "ready"
                or not frozen["source_basis_digest"]
                or frozen["current_basis"] != frozen["source_basis_digest"]
            )
            if source_changed or invalid_locator:
                decision = "needs_attention"
                reason = (
                    "This source changed after the source list was frozen, so its replacement was not reviewed."
                )
                error = "Run a new source check to evaluate the current source version."
                prepared_citations = []
            encoded = json.dumps(
                prepared_citations,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(encoded) > 100_000:
                raise ValueError("review citations are too large")
            changed = self.connection.execute(
                "UPDATE workbench_review_decision SET machine_decision=?,rationale=?,citations_json=?,"
                "error_message=?,updated_at=? WHERE run_id=? AND document_id=? "
                "AND machine_decision='pending'",
                (decision, reason, encoded, error, now, run_id, document_id),
            ).rowcount
            if changed != 1:
                row = self.connection.execute(
                    "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(run_id)
                return self._review_run(row)
            changed_run = self.connection.execute(
                "UPDATE workbench_review_run SET reviewed_count=reviewed_count+1,"
                "included_count=included_count+?,excluded_count=excluded_count+?,"
                "attention_count=attention_count+?,updated_at=? "
                "WHERE run_id=? AND state='running'",
                (
                    1 if decision == "included" else 0,
                    1 if decision == "excluded" else 0,
                    1 if decision == "needs_attention" else 0,
                    now,
                    run_id,
                ),
            ).rowcount
            if changed_run != 1:
                raise WorkspaceProblem("This review run is no longer active.")
            updated = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
            reviewed = int(updated["reviewed_count"])
            message = f"Reviewed {reviewed:,} of {int(updated['snapshot_count']):,} frozen sources."
            self.connection.execute(
                "UPDATE workbench_review_run SET message=?,updated_at=? WHERE run_id=?",
                (message, now, run_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
            # Persist coarse progress milestones without creating 10,000 event rows.
            if reviewed == int(updated["snapshot_count"]) or reviewed == 1 or reviewed % 25 == 0:
                self._append_review_event_locked(
                    run_id, state="running", stage="reviewing", message=message,
                    reviewed_count=reviewed, snapshot_count=int(updated["snapshot_count"]),
                    created_at=now,
                )
        return self._review_run(updated)

    def finish_review_run(self, run_id: str, *, expected_attempt: int | None = None) -> ReviewRunRecord:
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if expected_attempt is not None and row["attempts"] != expected_attempt:
                raise WorkspaceProblem("This review attempt is no longer active.")
            if row["state"] == "succeeded":
                return self._review_run(row)
            if row["state"] != "running":
                raise WorkspaceProblem("This review run is no longer active.")
            pending = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_review_decision WHERE run_id=? "
                    "AND machine_decision='pending'", (run_id,)
                ).fetchone()[0]
            )
            if pending:
                raise WorkspaceProblem("The frozen review population is not complete.")
            if int(row["cancellation_requested"]):
                return self.fail_review_run(run_id, "Review cancelled.")
            authorized = self.connection.execute(
                "SELECT 1 FROM workbench_effective_membership membership "
                "JOIN workbench_principal principal "
                "ON principal.principal_id=membership.principal_id "
                "JOIN workbench_matter_lifecycle lifecycle "
                "ON lifecycle.matter_id=membership.matter_id "
                "WHERE membership.matter_id=? AND membership.principal_id=? "
                "AND membership.state='active' AND principal.active=1 "
                "AND lifecycle.state='active'",
                (row["matter_id"], row["actor_id"]),
            ).fetchone()
            if authorized is None:
                raise WorkspaceProblem(
                    "Access to this matter was removed before the every-source check could be completed."
                )
            message = (
                f"Every-source check complete: {int(row['included_count']):,} included, "
                f"{int(row['excluded_count']):,} excluded, "
                f"{int(row['attention_count']):,} need attention."
            )
            self.connection.execute(
                "UPDATE workbench_review_run SET state='succeeded',stage='complete',message=?,"
                "worker_id=NULL,finished_at=?,updated_at=? WHERE run_id=? AND state='running'",
                (message, now, now, run_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
            self._append_review_event_locked(
                run_id, state="succeeded", stage="complete", message=message,
                reviewed_count=int(updated["reviewed_count"]),
                snapshot_count=int(updated["snapshot_count"]), created_at=now,
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, updated["matter_id"]),
            )
        return self._review_run(updated)

    def mark_review_decision_source_changed(
        self, run_id: str, document_id: str
    ) -> ReviewRunRecord:
        """Content-minimize a completed decision whose frozen source changed."""

        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT item.machine_decision,item.updated_at,run.state FROM workbench_review_decision item "
                "JOIN workbench_review_run run ON run.run_id=item.run_id "
                "WHERE item.run_id=? AND item.document_id=?",
                (run_id, document_id),
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            now = self._review_decision_time(row["updated_at"])
            prior = str(row["machine_decision"])
            if row["state"] != "running" or prior == "pending":
                raise WorkspaceProblem("This source check is not ready to be finalized.")
            if prior != "needs_attention":
                changed = self.connection.execute(
                    "UPDATE workbench_review_decision SET machine_decision='needs_attention',"
                    "rationale=?,citations_json='[]',error_message=?,updated_at=? "
                    "WHERE run_id=? AND document_id=? AND machine_decision=?",
                    (
                        "This source changed after the frozen decision was prepared, so its replacement was not reviewed.",
                        "Run a new source check to evaluate the current source version.",
                        now,
                        run_id,
                        document_id,
                        prior,
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkspaceProblem("This source decision changed unexpectedly.")
                self.connection.execute(
                    "UPDATE workbench_review_run SET included_count=included_count-?,"
                    "excluded_count=excluded_count-?,attention_count=attention_count+1,"
                    "updated_at=? WHERE run_id=? AND state='running'",
                    (
                        1 if prior == "included" else 0,
                        1 if prior == "excluded" else 0,
                        now,
                        run_id,
                    ),
                )
            else:
                self.connection.execute(
                    "UPDATE workbench_review_decision SET rationale=?,citations_json='[]',"
                    "error_message=?,updated_at=? "
                    "WHERE run_id=? AND document_id=?",
                    (
                        "This source changed after the frozen decision was prepared, so its replacement was not reviewed.",
                        "Run a new source check to evaluate the current source version.",
                        now,
                        run_id,
                        document_id,
                    ),
                )
            updated = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._review_run(updated)

    def fail_review_run(self, run_id: str, message: str, *, expected_attempt: int | None = None) -> ReviewRunRecord:
        value = self._safe_text(message, label="Review status", maximum=240, required=False)
        now = self._now()
        with self._lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if expected_attempt is not None and row["attempts"] != expected_attempt:
                return self._review_run(row)
            if row["state"] != "running":
                return self._review_run(row)
            cancelled = bool(row["cancellation_requested"]) or value == "Review cancelled."
            state, stage = ("cancelled", "cancelled") if cancelled else ("failed", "failed")
            final_message = "Review cancelled." if cancelled else (value or "Review could not be completed.")
            self.connection.execute(
                "UPDATE workbench_review_run SET state=?,stage=?,message=?,worker_id=NULL,"
                "finished_at=?,updated_at=? WHERE run_id=? AND state='running'",
                (state, stage, final_message, now, now, run_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
            self._append_review_event_locked(
                run_id, state=state, stage=stage, message=final_message,
                reviewed_count=int(updated["reviewed_count"]),
                snapshot_count=int(updated["snapshot_count"]), created_at=now,
            )
        return self._review_run(updated)

    def cancel_review_run(self, matter_id: str, actor_id: str, run_id: str) -> ReviewRunRecord:
        run = self.review_run(matter_id, actor_id, run_id)
        if run.actor_id != actor_id:
            raise WorkspaceProblem("Only the reviewer who started this run can cancel it.")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            run = self.review_run(matter_id, actor_id, run_id)
            if run.state == "running" and run.cancellation_requested:
                return run
            if run.state == "queued":
                state, stage, message, finished = "cancelled", "cancelled", "Review cancelled before it started.", now
            elif run.state == "running":
                state, stage, message, finished = "running", "cancelling", "Cancelling after the current source.", None
            elif run.state == "cancelled":
                return run
            else:
                raise WorkspaceProblem("This review run can no longer be cancelled.")
            self.connection.execute(
                "UPDATE workbench_review_run SET state=?,stage=?,message=?,cancellation_requested=1,"
                "finished_at=?,updated_at=? WHERE run_id=?",
                (state, stage, message, finished, now, run_id),
            )
            self._append_review_event_locked(
                run_id, state=state, stage=stage, message=message,
                reviewed_count=run.reviewed_count, snapshot_count=run.snapshot_count, created_at=now,
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._review_run(row)

    def retry_review_run(self, matter_id: str, actor_id: str, run_id: str) -> ReviewRunRecord:
        run = self.review_run(matter_id, actor_id, run_id)
        if run.actor_id != actor_id:
            raise WorkspaceProblem("Only the reviewer who started this run can retry it.")
        if run.state in {"queued", "running"}:
            return run
        if run.state not in {"failed", "cancelled"}:
            raise WorkspaceProblem("This completed review does not need to be retried.")
        now = self._now()
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.membership(matter_id, actor_id)
            run = self.review_run(matter_id, actor_id, run_id)
            if run.state in {"queued", "running"}:
                return run
            if run.state not in {"failed", "cancelled"}:
                raise WorkspaceProblem("This completed review does not need to be retried.")
            from .full_text_review_budget import policy_for, reacquire_locked
            if self.connection.execute("SELECT 1 FROM workbench_text_review WHERE run_id=?", (run_id,)).fetchone():
                reacquire_locked(self.connection, run, policy_for(self.connection, run_id))
            self.connection.execute(
                "UPDATE workbench_review_run SET state='queued',stage='reviewing',"
                "message='Review queued to resume saved decisions.',worker_id=NULL,"
                "cancellation_requested=0,started_at=NULL,finished_at=NULL,updated_at=? WHERE run_id=?",
                (now, run_id),
            )
            self._append_review_event_locked(
                run_id, state="queued", stage="reviewing",
                message="Review queued to resume saved decisions.",
                reviewed_count=run.reviewed_count, snapshot_count=run.snapshot_count, created_at=now,
            )
            row = self.connection.execute(
                "SELECT * FROM workbench_review_run WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._review_run(row)

    def review_decisions(
        self, matter_id: str, actor_id: str, run_id: str, *, page: int = 1,
        page_size: int = 50, decision_filter: str = "", validation_only: bool = False,
    ) -> ReviewDecisionPageRecord:
        self.review_run(matter_id, actor_id, run_id)
        state = (decision_filter or "").strip()
        if state not in {"", "pending", "included", "excluded", "needs_attention"}:
            raise WorkspaceProblem("Choose a valid review decision filter.")
        page_size_value = min(max(int(page_size), 10), 100)
        clauses = ["run_id=?"]
        parameters: list[object] = [run_id]
        if state:
            clauses.append("machine_decision=?")
            parameters.append(state)
        if validation_only:
            clauses.append("validation_sample=1")
        where = " AND ".join(clauses)
        with self._lock:
            total = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_review_decision WHERE " + where,
                    parameters,
                ).fetchone()[0]
            )
            total_pages = max((total + page_size_value - 1) // page_size_value, 1)
            page_value = min(max(int(page), 1), total_pages)
            rows = self.connection.execute(
                "SELECT * FROM workbench_review_decision WHERE " + where
                + " ORDER BY ordinal LIMIT ? OFFSET ?",
                (*parameters, page_size_value, (page_value - 1) * page_size_value),
            ).fetchall()
            count_rows = self.connection.execute(
                "SELECT machine_decision,COUNT(*) AS count FROM workbench_review_decision "
                "WHERE run_id=? GROUP BY machine_decision", (run_id,)
            ).fetchall()
        counts = {key: 0 for key in ("pending", "included", "excluded", "needs_attention")}
        counts.update({row["machine_decision"]: int(row["count"]) for row in count_rows})
        counts["all"] = sum(counts.values())
        return ReviewDecisionPageRecord(
            tuple(self._review_decision(row) for row in rows), page_value, page_size_value,
            total, total_pages, counts, state, bool(validation_only),
        )

    def review_decision(
        self, matter_id: str, actor_id: str, run_id: str, document_id: str
    ) -> ReviewDecisionRecord:
        self.review_run(matter_id, actor_id, run_id)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM workbench_review_decision WHERE run_id=? AND document_id=?",
                (run_id, self._source_document_id(document_id)),
            ).fetchone()
        if row is None:
            raise KeyError(document_id)
        return self._review_decision(row)

    def review_decisions_for_export(
        self,
        matter_id: str,
        actor_id: str,
        run_id: str,
        *,
        limit: int = 100_000,
        after_ordinal: int = 0,
        administrator_override: bool = False,
        _snapshot: sqlite3.Connection | None = None,
    ) -> tuple[ReviewDecisionRecord, ...]:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        if not _REVIEW_RUN.fullmatch(run_id or ""):
            raise KeyError(run_id)
        bounded = min(max(int(limit), 1), 100_000)
        cursor = max(int(after_ordinal), 0)
        with self._lock:
            run = (_snapshot if _snapshot is not None else self.connection).execute(
                "SELECT 1 FROM workbench_review_run WHERE matter_id=? AND run_id=?",
                (matter_id, run_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            rows = (_snapshot if _snapshot is not None else self.connection).execute(
                "SELECT * FROM workbench_review_decision WHERE run_id=? AND ordinal>? "
                "ORDER BY ordinal LIMIT ?", (run_id, cursor, bounded)
            ).fetchall()
        return tuple(self._review_decision(row) for row in rows)

    def _review_decision_time(self, previous: str) -> str:
        earlier = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        return self._timestamp(max(self.current_time(), earlier + timedelta(microseconds=1)))

    def adjudicate_review_decision(
        self, matter_id: str, actor_id: str, run_id: str, document_id: str, *,
        human_decision: str, expected_updated_at: str, note: str = "",
    ) -> ReviewDecisionRecord:
        if human_decision not in {"agree", "include", "exclude", "uncertain"}:
            raise WorkspaceProblem("Choose agree, include, exclude, or uncertain.")
        comment = self._safe_text(
            note, label="Validation note", maximum=2_000, required=False, multiline=True
        )
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            actor = self.membership(matter_id, actor_id).principal_id
            current = self.review_decision(matter_id, actor, run_id, document_id)
            if not expected_updated_at or expected_updated_at != current.updated_at:
                raise ReviewDecisionConflict(
                    "This source review changed since the page was opened. Compare the saved review before trying again."
                )
            if current.machine_decision == "pending":
                raise WorkspaceProblem("This source decision is still pending. Wait for it to finish before reviewing it.")
            now = self._review_decision_time(current.updated_at)
            self.connection.execute(
                "UPDATE workbench_review_decision SET human_decision=?,human_note=?,reviewed_by=?,"
                "reviewed_at=?,updated_at=? WHERE run_id=? AND matter_id=? AND document_id=?",
                (human_decision, comment, actor, now, now, run_id, matter_id, current.document_id),
            )
            return self.review_decision(matter_id, actor, run_id, current.document_id)

    def review_validation_metrics(
        self,
        matter_id: str,
        actor_id: str,
        run_id: str,
        *,
        administrator_override: bool = False,
        _snapshot: sqlite3.Connection | None = None,
    ) -> Mapping[str, object]:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        if not _REVIEW_RUN.fullmatch(run_id or ""):
            raise KeyError(run_id)
        with self._lock:
            run = (_snapshot if _snapshot is not None else self.connection).execute(
                "SELECT 1 FROM workbench_review_run WHERE matter_id=? AND run_id=?",
                (matter_id, run_id),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            rows = (_snapshot if _snapshot is not None else self.connection).execute(
                "SELECT machine_decision,human_decision FROM workbench_review_decision "
                "WHERE run_id=? AND validation_sample=1 AND human_decision<>''",
                (run_id,),
            ).fetchall()
            sample_total = int(
                (_snapshot if _snapshot is not None else self.connection).execute(
                    "SELECT COUNT(*) FROM workbench_review_decision WHERE run_id=? "
                    "AND validation_sample=1", (run_id,)
                ).fetchone()[0]
            )
        tp = fp = fn = tn = uncertain = unscored = 0
        for row in rows:
            human = row["human_decision"]
            machine = row["machine_decision"]
            if human == "uncertain":
                uncertain += 1
                continue
            if machine not in {"included", "excluded"}:
                unscored += 1
                continue
            actual = machine if human == "agree" else ("included" if human == "include" else "excluded")
            if machine == "included" and actual == "included": tp += 1
            elif machine == "included" and actual == "excluded": fp += 1
            elif machine == "excluded" and actual == "included": fn += 1
            else: tn += 1
        adjudicated = tp + fp + fn + tn
        ratio = lambda numerator, denominator: (numerator / denominator) if denominator else None
        return {
            "sample_total": sample_total,
            "reviewed_total": len(rows),
            "adjudicated_total": adjudicated,
            "uncertain_total": uncertain,
            "unscored_total": unscored,
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "true_negative": tn,
            "precision": ratio(tp, tp + fp),
            "recall": ratio(tp, tp + fn),
            "elusion": ratio(fn, fn + tn),
            "richness": ratio(tp + fn, adjudicated),
            "error_rate": ratio(fp + fn, adjudicated),
        }

    def review_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM workbench_review_run GROUP BY state"
            ).fetchall()
        counts = {state: 0 for state in ("queued", "running", "succeeded", "failed", "cancelled")}
        counts.update({row["state"]: int(row["count"]) for row in rows})
        return counts
