from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .review_budget import budget_metadata, budget_description
from .source_locations import SourceScanItem


class WorkspaceProblem(ValueError):
    """Expected staff-safe matter or conversation input failure."""


def is_full_text_synthesis(plan: Mapping[str, object] | None, result: Mapping[str, object] | None) -> bool:
    """Reserve either adapter key, even when its value needs strict rejection."""
    return ("full_text_synthesis_version" in (plan or {})
            or "full_text_synthesis_input" in (result or {}))


class MatterNameConflict(WorkspaceProblem):
    """The name displayed when the edit began is no longer current."""


class ReviewDecisionConflict(WorkspaceProblem):
    """A shared source-validation decision changed after its form was displayed."""


class ReportEditConflict(WorkspaceProblem):
    """A Report or section changed after its form was displayed."""


class NotebookEditConflict(WorkspaceProblem):
    """A case note changed after its form was displayed."""


@dataclass(frozen=True)
class MatterRecord:
    matter_id: str
    slug: str
    display_name: str
    descriptor: str
    owner_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MatterLifecycleRecord:
    matter_id: str
    state: str
    purge_id: str | None
    requested_by: str | None
    requested_at: str | None
    finished_at: str | None
    error_code: str | None
    source_count: int
    conversation_count: int
    message_count: int
    updated_at: str


@dataclass(frozen=True)
class MatterRetentionRecord:
    matter_id: str
    expires_at: str
    purge_after: str
    scheduled_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PrincipalPreferenceRecord:
    principal_id: str
    theme: str
    updated_at: str
    accent: str = "cyan"


@dataclass(frozen=True)
class MatterActivityRecord:
    activity_id: str
    matter_id: str
    principal_id: str
    activity_kind: str
    object_id: str
    locator: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PrincipalRecord:
    principal_id: str
    provider: str
    provider_subject: str
    display_name: str
    login_name: str
    active: int
    created_at: str
    last_seen_at: str


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    token_digest: str
    principal_id: str
    auth_method: str
    application_roles: str
    created_at: str
    last_seen_at: str
    idle_expires_at: str
    absolute_expires_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class MatterMembershipRecord:
    matter_id: str
    principal_id: str
    role: str
    state: str
    granted_by: str
    created_at: str
    updated_at: str
    revoked_at: str | None
    display_name: str
    login_name: str


@dataclass(frozen=True)
class AuditEventRecord:
    event_id: str
    occurred_at: str
    actor_principal_id: str | None
    session_id: str | None
    matter_id: str | None
    request_id: str
    action: str
    outcome: str
    object_type: str | None
    object_id: str | None
    details: Mapping[str, object]


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    matter_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ConversationSummaryRecord:
    conversation_id: str
    matter_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    state: str
    is_pinned: int
    archived_at: str | None


@dataclass(frozen=True)
class ConversationOrganizationRecord:
    conversation_id: str
    matter_id: str
    state: str
    is_pinned: int
    archived_at: str | None
    archived_by: str | None
    updated_by: str | None
    updated_at: str


@dataclass(frozen=True)
class ConversationDeletionRecord:
    conversation_id: str
    message_count: int
    answer_job_count: int
    research_job_count: int
    next_conversation_id: str
    replacement_created: bool


@dataclass(frozen=True)
class MessageRecord:
    message_id: str
    conversation_id: str
    ordinal: int
    role: str
    content: str
    payload: Mapping[str, object]
    created_at: str


@dataclass(frozen=True)
class IngestPlanRecord:
    plan_id: str
    matter_id: str
    source_location_id: str
    source_label: str
    relative_folder: str
    state: str
    supported_count: int
    unsupported_count: int
    total_bytes: int
    created_at: str
    confirmed_at: str | None


@dataclass(frozen=True)
class IngestPlanItemRecord:
    plan_id: str
    ordinal: int
    relative_path: str
    display_name: str
    media_type: str
    byte_size: int
    stable_device: int
    stable_inode: int
    stable_mtime_ns: int
    document_id: str | None

    def scan_item(self) -> SourceScanItem:
        return SourceScanItem(
            self.relative_path,
            self.display_name,
            self.media_type,
            self.byte_size,
            self.stable_device,
            self.stable_inode,
            self.stable_mtime_ns,
        )


@dataclass(frozen=True)
class IngestJobRecord:
    job_id: str
    matter_id: str
    document_id: str
    plan_id: str | None
    source_location_id: str | None
    relative_path: str | None
    state: str
    stage: str
    completed_units: int
    total_units: int
    attempts: int
    worker_id: str | None
    message: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True)
class MediaJobRecord:
    media_job_id: str
    matter_id: str
    document_id: str
    source_version_id: str
    requested_by: str
    state: str
    stage: str
    progress: float
    external_job_id: str | None
    source_sha256: str
    byte_size: int
    media_type: str
    duration_ms: int
    attempts: int
    worker_id: str | None
    degraded: int
    message: str
    warnings: tuple[object, ...]
    quality: Mapping[str, object]
    provenance: Mapping[str, object]
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str

    @property
    def preflight(self) -> Mapping[str, object]:
        value = self.quality.get("preflight")
        return value if isinstance(value, dict) else {}

    @property
    def display_state(self) -> str:
        if self.state == "cancelled" and self.preflight:
            return "playback_only" if self.preflight.get("outcome") == "no_audio" else "needs_review"
        return self.state


@dataclass(frozen=True)
class MediaTranscriptRecord:
    transcript_id: str
    matter_id: str
    document_id: str
    source_version_id: str
    media_job_id: str
    review_state: str
    duration_ms: int
    segment_count: int
    transcript_digest: str
    warnings: tuple[object, ...]
    quality: Mapping[str, object]
    provenance: Mapping[str, object]
    imported_by: str
    imported_at: str
    updated_at: str


@dataclass(frozen=True)
class MediaSummaryRecord:
    transcript_id: str
    matter_id: str
    document_id: str
    source_version_id: str
    state: str
    attempts: int
    payload: Mapping[str, object]
    basis_digest: str
    covered_segment_count: int
    total_segment_count: int
    message: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True)
class TranscriptSegmentRecord:
    segment_id: str
    transcript_id: str
    matter_id: str
    ordinal: int
    external_segment_id: str
    start_ms: int
    end_ms: int
    speaker_cluster: str
    model_text: str
    translated_text: str | None
    confidence: float | None
    low_confidence: int
    overlap: int
    current_text: str
    current_revision: int
    speaker_display_name: str
    speaker_identity_state: str
    speaker_revision: int
    created_at: str


@dataclass(frozen=True)
class SpeakerMappingRecord:
    transcript_id: str
    matter_id: str
    speaker_cluster: str
    display_name: str
    identity_state: str
    revision: int
    segment_count: int
    edited_by: str
    edited_at: str


@dataclass(frozen=True)
class MediaClipRecord:
    clip_id: str
    matter_id: str
    document_id: str
    source_version_id: str
    title: str
    start_ms: int
    end_ms: int
    created_by: str
    created_at: str


@dataclass(frozen=True)
class SourceCollectionRecord:
    collection_id: str
    matter_id: str
    name: str
    kind: str
    created_by: str | None
    created_at: str
    updated_at: str
    source_count: int = 0


@dataclass(frozen=True)
class SourceOrganizationRecord:
    matter_id: str
    document_id: str
    collection_id: str | None
    relative_path: str
    review_state: str
    added_by: str | None
    added_at: str
    updated_by: str | None
    updated_at: str
    source_role: str = "unclassified"


@dataclass(frozen=True)
class SourceCatalogRecord:
    matter_id: str
    document_id: str
    version_id: str
    action_token: str
    display_name: str
    relative_path: str
    media_type: str
    kind: str
    source_state: str
    tone: str
    state_label: str
    count_label: str
    processing_stage: str
    completed_units: int
    total_units: int
    page_count: int
    duration_ms: int
    byte_size: int
    origin: str
    retryable: int
    removable: int
    has_video: int
    content_basis_digest: str
    collection_id: str
    collection_name: str
    review_state: str
    added_at: str
    byte_match_count: int = 0
    source_role: str = "unclassified"


@dataclass(frozen=True)
class SourceCatalogPageRecord:
    items: tuple[SourceCatalogRecord, ...]
    total: int
    stats: Mapping[str, int]
    type_counts: Mapping[str, int]
    review_counts: Mapping[str, int]


@dataclass(frozen=True)
class SourceFolderRecord:
    name: str
    path: str
    source_count: int


@dataclass(frozen=True)
class SourceFolderPageRecord:
    items: tuple[SourceFolderRecord, ...]
    total: int


@dataclass(frozen=True)
class MatterReadinessRecord:
    matter_id: str
    state: str
    total_count: int
    saved_count: int
    extracted_count: int
    searchable_count: int
    processing_count: int
    attention_count: int
    uploading_count: int
    extracting_count: int
    indexing_count: int
    transcribing_count: int
    overview_processing_count: int
    overview_attention_count: int
    progress_percent: int
    updated_at: str
    playback_only_count: int = 0
    recording_review_count: int = 0
    email_count: int = 0
    pdf_count: int = 0

    @property
    def can_query(self) -> bool:
        # Active preparation still holds the matter-wide gate. Terminal source
        # failures do not: reviewers may use the successfully indexed subset
        # as long as Exculpata discloses exactly what was excluded.
        return self.state in {"ready", "attention"} and self.searchable_count > 0

    @property
    def partial_query(self) -> bool:
        return self.can_query and (
            self.attention_count > 0 or self.email_count > 0 or self.pdf_count > 0
        )


@dataclass(frozen=True)
class SourceSetRecord:
    source_set_id: str
    matter_id: str
    name: str
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    source_count: int = 0


@dataclass(frozen=True)
class UploadSessionRecord:
    upload_session_id: str
    matter_id: str
    collection_id: str
    actor_id: str
    state: str
    item_count: int
    total_bytes: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UploadItemRecord:
    upload_item_id: str
    upload_session_id: str
    matter_id: str
    ordinal: int
    display_name: str
    relative_path: str
    media_type: str
    expected_size: int
    received_size: int
    state: str
    document_id: str | None
    message: str
    updated_at: str


@dataclass(frozen=True)
class AnswerJobRecord:
    job_id: str
    matter_id: str
    conversation_id: str
    actor_id: str
    idempotency_key: str
    question: str
    question_message_id: str
    state: str
    stage: str
    attempts: int
    worker_id: str | None
    cancellation_requested: int
    result_message_id: str | None
    message: str
    created_at: str
    started_at: str | None
    last_claimed_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True)
class AnswerEventRecord:
    job_id: str
    ordinal: int
    state: str
    stage: str
    message: str
    created_at: str


@dataclass(frozen=True)
class ResearchJobRecord:
    job_id: str
    matter_id: str
    actor_id: str
    idempotency_key: str
    question: str
    title: str
    source_set_id: str | None
    conversation_id: str | None
    result_message_id: str | None
    state: str
    stage: str
    attempts: int
    worker_id: str | None
    cancellation_requested: int
    plan: Mapping[str, object]
    result: Mapping[str, object]
    total_steps: int
    completed_steps: int
    candidate_count: int
    evidence_count: int
    message: str
    created_at: str
    started_at: str | None
    last_claimed_at: str | None
    finished_at: str | None
    updated_at: str


    @property
    def review_budget(self) -> dict:
        return budget_metadata(self.result, self.plan)

    @property
    def review_budget_description(self) -> str:
        if is_full_text_synthesis(self.plan, self.result):
            return (
                "One saved full-text review for its original criterion. Existing synthesis "
                "limits apply: 32 generation requests and 900 seconds from first start. "
                "Interrupted requests stay charged; input omissions remain in the receipt."
            )
        return budget_description(self.review_budget)


@dataclass(frozen=True)
class ResearchEventRecord:
    job_id: str
    ordinal: int
    state: str
    stage: str
    completed_steps: int
    total_steps: int
    message: str
    created_at: str


@dataclass(frozen=True)
class ReviewCriterionRecord:
    criterion_id: str
    matter_id: str
    title: str
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    version_count: int = 0
    current_version_number: int = 0
    current_version_id: str = ""


@dataclass(frozen=True)
class ReviewCriterionVersionRecord:
    criterion_version_id: str
    criterion_id: str
    matter_id: str
    version_number: int
    instructions: str
    include_guidance: str
    exclude_guidance: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class ReviewRunRecord:
    run_id: str
    matter_id: str
    actor_id: str
    criterion_id: str
    criterion_version_id: str
    source_set_id: str | None
    run_kind: str
    state: str
    stage: str
    attempts: int
    worker_id: str | None
    cancellation_requested: int
    snapshot_count: int
    reviewed_count: int
    included_count: int
    excluded_count: int
    attention_count: int
    message: str
    created_at: str
    started_at: str | None
    last_claimed_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True)
class ReviewDecisionRecord:
    run_id: str
    matter_id: str
    ordinal: int
    document_id: str
    source_version_id: str
    source_basis_digest: str
    action_token: str
    source_name: str
    source_kind: str
    machine_decision: str
    rationale: str
    citations: tuple[Mapping[str, object], ...]
    error_message: str
    validation_sample: int
    human_decision: str
    human_note: str
    reviewed_by: str | None
    reviewed_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewDecisionPageRecord:
    items: tuple[ReviewDecisionRecord, ...]
    page: int
    page_size: int
    total: int
    total_pages: int
    counts: Mapping[str, int]
    decision_filter: str
    validation_only: bool


@dataclass(frozen=True)
class NotebookReferenceRecord:
    reference_id: str
    item_id: str
    matter_id: str
    ordinal: int
    document_id: str
    source_version_id: str
    source_name: str
    location: str
    unit_number: int
    chunk_id: str
    excerpt_digest: str
    excerpt: str
    support_token: str
    created_at: str


@dataclass(frozen=True)
class NotebookItemRecord:
    item_id: str
    matter_id: str
    item_type: str
    status: str
    title: str
    body: str
    date_label: str
    is_pinned: int
    origin: str
    source_conversation_id: str | None
    source_message_id: str | None
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    created_by_name: str = ""
    updated_by_name: str = ""
    reference_count: int = 0


@dataclass(frozen=True)
class NotebookPageRecord:
    items: tuple[NotebookItemRecord, ...]
    page: int
    page_size: int
    total: int
    total_pages: int
    counts: Mapping[str, int]
    type_counts: Mapping[str, int]
    query: str
    item_type: str
    status: str


@dataclass(frozen=True)
class AnalysisRunRecord:
    analysis_id: str
    matter_id: str
    requested_by: str
    state: str
    source_count: int
    unit_count: int
    entity_count: int
    finding_count: int
    capped: int
    message: str
    created_at: str
    finished_at: str | None


@dataclass(frozen=True)
class ReviewFindingRecord:
    finding_id: str
    matter_id: str
    kind: str
    signature: str
    title: str
    summary: str
    status: str
    active: int
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str


@dataclass(frozen=True)
class ReviewFindingReferenceRecord:
    reference_id: str
    finding_id: str
    matter_id: str
    ordinal: int
    document_id: str
    source_version_id: str
    source_name: str
    location: str
    unit_number: int
    chunk_id: str
    excerpt_digest: str
    excerpt: str
    support_token: str
    created_at: str


@dataclass(frozen=True)
class ReportRecord:
    report_id: str
    matter_id: str
    title: str
    purpose: str
    status: str
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    section_count: int = 0


@dataclass(frozen=True)
class ReportSectionRecord:
    section_id: str
    report_id: str
    matter_id: str
    ordinal: int
    heading: str
    body: str
    origin: str
    origin_id: str
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str

    compilation_basis: str = ""

    @property
    def prose_limit(self) -> int:
        suffix = "\n\nReview basis:\n" + self.compilation_basis if self.compilation_basis else ""
        return max(0, 50_000 - len(suffix))

    @property
    def prose(self) -> str:
        suffix = "\n\nReview basis:\n" + self.compilation_basis
        if self.compilation_basis and self.body.endswith(suffix):
            return self.body[:-len(suffix)]
        return self.body


@dataclass(frozen=True)
class ReportCitationRecord:
    citation_id: str
    section_id: str
    report_id: str
    matter_id: str
    ordinal: int
    kind: str
    document_id: str
    source_version_id: str
    source_name: str
    location: str
    support_token: str
    excerpt: str
    media_clip_id: str
    start_ms: int
    end_ms: int
    created_at: str


@dataclass(frozen=True)
class AnswerNotebookContextRecord:
    notebook_item_id: str
    item_type: str
    status: str
    title: str
    body: str
    date_label: str
    content_digest: str
