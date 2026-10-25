from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import re
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, pattern=r"^\S(?:.*\S)?$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    @field_validator("*", mode="after")
    @classmethod
    def reject_naive_datetimes(cls, value: object) -> object:
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("datetime must be timezone-aware")
        if isinstance(value, datetime) and value.utcoffset() != timedelta(0):
            raise ValueError("datetime must be UTC")
        return value


class MatterState(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class LocationKind(StrEnum):
    EVIDENCE_INPUT = "evidence_input"
    WORK_PRODUCT_OUTPUT = "work_product_output"


class AvailabilityState(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    SUPERSEDED = "superseded"


class SourceFileAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"


class IngestionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TicketAction(StrEnum):
    OPEN_ORIGINAL = "open_original"
    REVEAL_ORIGINAL = "reveal_original"
    SAVE_ARTIFACT = "save_artifact"


class ContentOperation(StrEnum):
    INGEST = "ingest"
    RETRIEVE = "retrieve"
    RESOLVE_SOURCE = "resolve_source"
    CASE_TOOL = "case_tool"
    ARTIFACT_READ = "artifact_read"
    MINT_TICKET = "mint_ticket"
    REDEEM_TICKET = "redeem_ticket"
    MODEL_CONTEXT = "model_context"
    VIEW = "view"
    SAVE = "save"


class CollisionOutcome(StrEnum):
    VERSIONED_NAME = "versioned_name"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"


class Matter(StrictModel):
    matter_id: Identifier
    display_name: Annotated[str, Field(min_length=1)]
    state: MatterState
    active_closure_run_id: Identifier | None = None

    @model_validator(mode="after")
    def closure_state_consistency(self) -> "Matter":
        if (self.state is MatterState.CLOSING) != (self.active_closure_run_id is not None):
            raise ValueError("only a closing matter has one active closure run")
        return self


class SourceLocation(StrictModel):
    source_location_id: Identifier
    matter_id: Identifier
    display_name: Annotated[str, Field(min_length=1)]
    kind: LocationKind


class SourceLocationBinding(StrictModel):
    source_location_id: Identifier
    linux_root: Annotated[str, Field(min_length=1)]
    unc_root: Annotated[str, Field(min_length=1)]
    mapping_revision: Annotated[int, Field(ge=1)]


class ArchiveDestination(StrictModel):
    archive_destination_id: Identifier
    display_name: Annotated[str, Field(min_length=1)]
    policy_revision: Identifier


_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_DRIVE = re.compile(r"^[A-Za-z]:")


def _is_windows_device_alias(component: str) -> bool:
    trimmed = component.rstrip(". ")
    stem = trimmed.split(".", maxsplit=1)[0].rstrip(" ")
    return stem.upper() in _DEVICE_NAMES


def validate_relative_path(value: str) -> str:
    if not value or value.startswith(("/", "\\")) or _DRIVE.match(value) or "\\" in value:
        raise ValueError("relative path must be POSIX relative")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("relative path contains control characters")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative path contains invalid component")
    if any(":" in part for part in parts):
        raise ValueError("relative path contains colon component")
    if any(_is_windows_device_alias(part) for part in parts):
        raise ValueError("relative path contains device component")
    if any(part.endswith((".", " ")) for part in parts):
        raise ValueError("relative path contains duplicate or ambiguous trailing dot or space")
    normalized = str(PurePosixPath(value))
    if normalized != value:
        raise ValueError("relative path is not canonical")
    return value


class PageLocatorMetadata(StrictModel):
    page: Annotated[int, Field(ge=1)]
    printed_page: Identifier | None = None
    bates: Identifier | None = None


class LocatorMetadata(StrictModel):
    """Immutable, extraction-supplied bounds for exact citation validation."""

    pages: tuple[PageLocatorMetadata, ...] = ()
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    image_width_px: Annotated[int, Field(ge=1)] | None = None
    image_height_px: Annotated[int, Field(ge=1)] | None = None
    character_count: Annotated[int, Field(ge=0)] | None = None
    line_count: Annotated[int, Field(ge=0)] | None = None
    representation_ids: tuple[Identifier, ...] = ()
    segment_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def metadata_consistency(self) -> "LocatorMetadata":
        if (self.image_width_px is None) != (self.image_height_px is None):
            raise ValueError("image dimensions must be supplied together")
        for values, label in (
            ([item.page for item in self.pages], "page"),
            ([item.printed_page for item in self.pages if item.printed_page], "printed page"),
            ([item.bates for item in self.pages if item.bates], "Bates"),
            (list(self.representation_ids), "representation"),
            (list(self.segment_ids), "segment"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} locator metadata")
        return self


class PersistedSourceVersion(StrictModel):
    source_version_id: Identifier
    source_file_id: Identifier
    matter_id: Identifier
    source_location_id: Identifier
    relative_path: str
    display_name: Annotated[str, Field(min_length=1)]
    media_type: Annotated[str, Field(min_length=1)]
    byte_size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    stable_device: Annotated[int, Field(ge=0)]
    stable_inode: Annotated[int, Field(ge=0)]
    stable_mtime_ns: Annotated[int, Field(ge=0)]
    discovered_at: datetime

    _path = field_validator("relative_path")(validate_relative_path)


class SourceFileRecord(StrictModel):
    source_file_id: Identifier
    matter_id: Identifier
    source_location_id: Identifier
    relative_path: str
    display_name: Annotated[str, Field(min_length=1)]
    media_type: Annotated[str, Field(min_length=1)]
    availability: SourceFileAvailability
    first_seen_at: datetime
    last_seen_at: datetime
    current_source_version_id: Identifier | None = None

    _path = field_validator("relative_path")(validate_relative_path)

    @model_validator(mode="after")
    def seen_order(self) -> "SourceFileRecord":
        if self.last_seen_at < self.first_seen_at:
            raise ValueError("last seen precedes first seen")
        if self.availability is SourceFileAvailability.AVAILABLE and self.current_source_version_id is None:
            raise ValueError("available source file requires a current version")
        return self


class SourceVersion(StrictModel):
    source_version_id: Identifier
    matter_id: Identifier
    source_location_id: Identifier
    relative_path: str
    display_name: Annotated[str, Field(min_length=1)]
    media_type: Annotated[str, Field(min_length=1)]
    byte_size: Annotated[int, Field(ge=0)]
    sha256: Sha256
    discovered_at: datetime
    last_verified_at: datetime
    availability: AvailabilityState
    locator_metadata: LocatorMetadata

    _path = field_validator("relative_path")(validate_relative_path)

    @model_validator(mode="after")
    def verification_order(self) -> "SourceVersion":
        if self.last_verified_at < self.discovered_at:
            raise ValueError("last verification precedes discovery")
        return self


class Representation(StrictModel):
    representation_id: Identifier
    matter_id: Identifier
    source_version_id: Identifier
    kind: Identifier
    processor_version: Identifier
    locator_metadata: LocatorMetadata


class Segment(StrictModel):
    segment_id: Identifier
    matter_id: Identifier
    source_version_id: Identifier
    representation_id: Identifier
    locator_metadata: LocatorMetadata


class SourceLocator(StrictModel):
    page: Annotated[int, Field(ge=1)] | None = None
    printed_page: Annotated[str, Field(min_length=1)] | None = None
    bates_start: Annotated[str, Field(min_length=1)] | None = None
    bates_end: Annotated[str, Field(min_length=1)] | None = None
    timestamp_start_ms: Annotated[int, Field(ge=0)] | None = None
    timestamp_end_ms: Annotated[int, Field(ge=0)] | None = None
    image_region: tuple[Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)]] | None = None
    representation_id: Identifier | None = None
    segment_id: Identifier | None = None
    character_start: Annotated[int, Field(ge=0)] | None = None
    character_end: Annotated[int, Field(ge=0)] | None = None
    line_start: Annotated[int, Field(ge=1)] | None = None
    line_end: Annotated[int, Field(ge=1)] | None = None
    excerpt_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def locator_consistency(self) -> "SourceLocator":
        exact = any((self.page is not None, self.printed_page is not None, self.bates_start is not None, self.timestamp_start_ms is not None, self.image_region is not None, self.representation_id is not None, self.segment_id is not None, self.character_start is not None, self.line_start is not None))
        if not exact:
            raise ValueError("at least one exact locator is required")
        for start, end, label in (
            (self.timestamp_start_ms, self.timestamp_end_ms, "timestamp"),
            (self.character_start, self.character_end, "character"),
            (self.line_start, self.line_end, "line"),
        ):
            if end is not None and start is None:
                raise ValueError(f"{label} end requires start")
            if end is not None and start is not None and end <= start:
                raise ValueError(f"{label} end must exceed start")
        if self.bates_end is not None and self.bates_start is None:
            raise ValueError("Bates end requires start")
        if self.bates_end is not None and self.bates_start is not None:
            first = re.fullmatch(r'(.*?)([0-9]+)', self.bates_start)
            last = re.fullmatch(r'(.*?)([0-9]+)', self.bates_end)
            invalid_order = self.bates_end <= self.bates_start
            if first and last and first[1] == last[1]:
                start_digits, end_digits = first[2].lstrip('0'), last[2].lstrip('0')
                invalid_order = (len(end_digits), end_digits) <= (len(start_digits), start_digits)
            if invalid_order:
                raise ValueError("Bates end must exceed start")
        if self.segment_id is not None and self.representation_id is None:
            raise ValueError("segment requires representation")
        if self.image_region is not None:
            x1, y1, x2, y2 = self.image_region
            if x2 <= x1 or y2 <= y1:
                raise ValueError("image region must have positive area")
        return self


class SourceReference(StrictModel):
    reference_id: Identifier
    matter_id: Identifier
    source_version_id: Identifier
    locator: SourceLocator


class IngestionJob(StrictModel):
    job_id: Identifier
    matter_id: Identifier
    source_version_id: Identifier
    state: IngestionState
    created_at: datetime
    attempt: Annotated[int, Field(ge=1)] = 1


class RetrievalRun(StrictModel):
    run_id: Identifier
    matter_id: Identifier
    query: Annotated[str, Field(min_length=1)]
    created_at: datetime


class RetrievalResult(StrictModel):
    run_id: Identifier
    matter_id: Identifier
    source_version_id: Identifier
    reference: SourceReference
    rank: Annotated[int, Field(ge=1)]
    score: float

    @model_validator(mode="after")
    def same_matter_and_version(self) -> "RetrievalResult":
        if self.reference.matter_id != self.matter_id or self.reference.source_version_id != self.source_version_id:
            raise ValueError("retrieval result and reference must identify the same matter/version")
        return self


class TicketRecord(StrictModel):
    ticket_id: Identifier
    token_sha256: Sha256
    actor_id: Identifier
    matter_id: Identifier
    action: TicketAction
    object_version_id: Identifier
    mapping_revision: Annotated[int, Field(ge=1)]
    issued_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None = None
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def lifetime_order(self) -> "TicketRecord":
        if self.expires_at <= self.issued_at:
            raise ValueError("ticket expiry must follow issue time")
        if self.redeemed_at is not None and not self.issued_at <= self.redeemed_at < self.expires_at:
            raise ValueError("redemption must be within ticket lifetime")
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("revocation precedes issue time")
        return self


class OperationLease(StrictModel):
    lease_id: Identifier
    actor_id: Identifier
    matter_id: Identifier
    action: TicketAction
    object_version_id: Identifier
    mapping_revision: Annotated[int, Field(ge=1)]
    helper_instance_id: Identifier
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def lifetime_order(self) -> "OperationLease":
        if self.expires_at <= self.issued_at:
            raise ValueError("lease expiry must follow issue time")
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("revocation precedes issue time")
        return self


class ArchiveReceipt(StrictModel):
    receipt_id: Identifier
    matter_id: Identifier
    closure_run_id: Identifier
    archive_destination_id: Identifier
    policy_revision: Identifier
    verified_at: datetime
    adapter_attestation: Sha256


class ClosureRecord(StrictModel):
    matter_id: Identifier
    closure_run_id: Identifier
    policy_revision: Identifier
    source_count: Annotated[int, Field(ge=0)]
    artifact_count: Annotated[int, Field(ge=0)]
    requested_at: datetime
    completed_at: datetime
    receipt_id: Identifier
    archive_destination_id: Identifier

    @model_validator(mode="after")
    def completion_order(self) -> "ClosureRecord":
        if self.completed_at < self.requested_at:
            raise ValueError("closure completion precedes request")
        return self


class StaffSourceProjection(StrictModel):
    display_name: str
    media_type: str
    availability: AvailabilityState


class StaffReferenceProjection(StrictModel):
    source_name: str
    page: int | None = None
    printed_page: str | None = None
    bates_start: str | None = None
    bates_end: str | None = None
    timestamp_start_ms: int | None = None
    timestamp_end_ms: int | None = None
    image_region: tuple[float, float, float, float] | None = None
    character_start: int | None = None
    character_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    representation_navigation_id: Identifier | None = None
    segment_navigation_id: Identifier | None = None


class StaffRetrievalProjection(StrictModel):
    source: StaffSourceProjection
    support: StaffReferenceProjection


class TicketLaunchProjection(StrictModel):
    uri: Annotated[str, Field(pattern=rf"^{LEGACY_STEM}://ticket/[A-Za-z0-9_-]+$")]
    expires_at: datetime
