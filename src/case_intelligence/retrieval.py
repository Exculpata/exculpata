from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .contracts import (
    ContentOperation,
    PersistedSourceVersion,
    SourceFileAvailability,
    SourceFileRecord,
    SourceLocator,
    SourceReference,
    TicketAction,
    TicketLaunchProjection,
)
from .isolation import AuthorizationError, MatterAccess, MatterStateRegistry, ReferenceResolutionStatus
from .scanner import ScanError, SyntheticScanner
from .sqlite_store import SQLiteStore
from .text_records import PlainTextRecord, resolve_persisted_reference
from .tickets import TicketStore


class RetrievalIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchRequest:
    run_id: str
    matter_id: str
    query: str
    top_k: int = 10

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.query.strip() or len(self.query) > 512:
            raise ValueError("run and bounded nonblank query required")
        if not 1 <= self.top_k <= 20:
            raise ValueError("top_k must be 1 through 20")


@dataclass(frozen=True)
class StaffSearchResult:
    result_id: str
    source_name: str
    excerpt: str
    line_start: int
    line_end: int
    character_start: int
    character_end: int
    representation_navigation_id: str
    segment_navigation_id: str
    open_source_action: bool = True


@dataclass(frozen=True)
class SearchResponse:
    run_id: str
    matter_id: str
    outcome: str
    results: tuple[StaffSearchResult, ...]


@dataclass(frozen=True)
class _Candidate:
    matter_id: str
    source_file_id: str
    source_version_id: str
    representation_id: str
    segment_id: str
    display_name: str
    text: str
    text_sha256: str
    character_start: int
    character_end: int
    line_start: int
    line_end: int
    exact_keys: tuple[str, ...]
    location_label: str | None = None


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", value.casefold(), flags=re.UNICODE))


class RetrievalService:
    def __init__(self, store: SQLiteStore, registry: MatterStateRegistry,
                 access: MatterAccess, tickets: TicketStore, *,
                 candidate_provider: Callable[[str], Iterable[_Candidate]] | None = None) -> None:
        self.store = store
        self.registry = registry
        self.access = access
        self.tickets = tickets
        self.candidate_provider = candidate_provider
        self.lookup_count = 0
        self.scoring_count = 0

    def search(self, actor_id: str, request: SearchRequest) -> SearchResponse:
        self.access.require(actor_id, request.matter_id)

        def perform() -> SearchResponse:
            candidates = tuple(self.candidate_provider(request.matter_id) if self.candidate_provider else self._current(request.matter_id))
            self.lookup_count += 1
            if any(item.matter_id != request.matter_id for item in candidates):
                raise AuthorizationError("mixed candidate set")
            self.scoring_count += 1
            query_folded = request.query.strip().casefold()
            query_terms = _tokens(request.query)
            exact = [item for item in candidates if query_folded in {key.casefold() for key in item.exact_keys}][:50]
            lexical = [item for item in candidates if query_terms and all(term in set(_tokens(item.text)) for term in query_terms)][:50]
            exact_rank = {item.segment_id: rank for rank, item in enumerate(sorted(exact, key=self._identity), 1)}
            lexical_rank = {item.segment_id: rank for rank, item in enumerate(sorted(lexical, key=self._identity), 1)}
            by_id = {item.segment_id: item for item in (*exact, *lexical)}
            if len(by_id) > 100:
                raise RetrievalIntegrityError("fused candidate bound exceeded")
            ranked = sorted(by_id.values(), key=lambda item: (
                item.segment_id not in exact_rank,
                -(1 / (60 + exact_rank[item.segment_id]) if item.segment_id in exact_rank else 0)
                -(1 / (60 + lexical_rank[item.segment_id]) if item.segment_id in lexical_rank else 0),
                exact_rank.get(item.segment_id, 10**9), lexical_rank.get(item.segment_id, 10**9),
                item.source_version_id, item.representation_id, item.segment_id,
            ))[:request.top_k]
            projected = tuple(self._verify_and_project(request, item) for item in ranked)
            return SearchResponse(request.run_id, request.matter_id,
                                  "matched" if projected else "no_match", projected)
        return self.registry.perform_active(request.matter_id, ContentOperation.RETRIEVE, perform)

    def mint_open_source(self, actor_id: str, matter_id: str, segment_navigation_id: str) -> TicketLaunchProjection:
        self.access.require(actor_id, matter_id)

        def select() -> TicketLaunchProjection:
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT sf.availability,sf.current_source_version_id,sf.relative_path,"
                    "s.source_version_id,s.matter_id,sf.source_location_id,l.enabled,w.mapping_revision,"
                    "v.sha256,b.synthetic_root "
                    "FROM segment s JOIN source_file sf ON sf.source_file_id=s.source_file_id AND sf.matter_id=s.matter_id "
                    "JOIN source_version v ON v.source_version_id=s.source_version_id AND v.source_file_id=s.source_file_id AND v.matter_id=s.matter_id "
                    "JOIN source_location l ON l.source_location_id=sf.source_location_id AND l.matter_id=sf.matter_id "
                    "JOIN windows_mapping w ON w.source_location_id=sf.source_location_id AND w.matter_id=sf.matter_id "
                    "JOIN scanner_binding b ON b.source_location_id=sf.source_location_id AND b.matter_id=sf.matter_id "
                    "WHERE s.matter_id=? AND s.segment_id=?",
                    (matter_id, segment_navigation_id),
                ).fetchone()
                if row is None:
                    raise AuthorizationError("selected source is invalid")
                if (row["matter_id"] != matter_id or row["availability"] != "available" or
                    row["current_source_version_id"] != row["source_version_id"] or not row["enabled"]):
                    raise SourceUnavailable("source_unavailable")
                from .contracts import validate_relative_path
                validate_relative_path(row["relative_path"])
                version_id = row["source_version_id"]
                revision = row["mapping_revision"]
                relative_path = row["relative_path"]
                expected_sha256 = row["sha256"]
                synthetic_root = row["synthetic_root"]
            # Freshly validate the selected bytes while the matter RLock prevents
            # inventory/mapping changes. The scanner closes every descriptor.
            try:
                matches = [
                    item for item in SyntheticScanner(Path(synthetic_root)).scan()
                    if item.relative_path == relative_path
                ]
            except ScanError as exc:
                raise SourceUnavailable("source_unavailable") from exc
            if len(matches) != 1 or matches[0].sha256 != expected_sha256:
                raise SourceUnavailable("source_unavailable")
            # SQLite is released; outer registry RLock remains held. TicketStore
            # re-enters that lock and then acquires only its ticket lock.
            return self.tickets.mint(self.access, actor_id, matter_id,
                                     TicketAction.OPEN_ORIGINAL, version_id, revision)
        return self.registry.perform_active(matter_id, ContentOperation.MINT_TICKET, select)

    def _current(self, matter_id: str) -> Iterable[_Candidate]:
        rows = self.store.connection.execute(
            "SELECT s.*,sf.display_name FROM segment s JOIN source_file sf ON sf.source_file_id=s.source_file_id "
            "WHERE s.matter_id=? AND sf.matter_id=? AND sf.availability='available' "
            "AND sf.current_source_version_id=s.source_version_id ORDER BY s.source_version_id,s.representation_id,s.segment_id",
            (matter_id, matter_id),
        )
        for row in rows:
            yield _Candidate(row["matter_id"], row["source_file_id"], row["source_version_id"],
                             row["representation_id"], row["segment_id"], row["display_name"],
                             row["text"], row["text_sha256"], row["character_start"],
                             row["character_end"], row["line_start"], row["line_end"],
                             tuple(json.loads(row["exact_keys_json"])))

    @staticmethod
    def _identity(item: _Candidate):
        return item.source_version_id, item.representation_id, item.segment_id

    def _verify_and_project(self, request: SearchRequest, item: _Candidate) -> StaffSearchResult:
        source_file, version = self._load_source(item.matter_id, item.source_file_id, item.source_version_id)
        record = PlainTextRecord(item.segment_id, item.representation_id, item.matter_id,
                                 item.source_file_id, item.source_version_id, item.text,
                                 item.text_sha256, item.character_start, item.character_end,
                                 item.line_start, item.line_end, item.exact_keys,
                                 SourceLocator(representation_id=item.representation_id,
                                     segment_id=item.segment_id, character_start=item.character_start,
                                     character_end=item.character_end, line_start=item.line_start,
                                     line_end=item.line_end, excerpt_sha256=item.text_sha256))
        reference = SourceReference(reference_id=f"reference-{request.run_id}-{item.segment_id}",
                                    matter_id=item.matter_id, source_version_id=item.source_version_id,
                                    locator=record.locator)
        resolution = resolve_persisted_reference(request.matter_id, reference, source_file, version,
                                                  record=record, excerpt_sha256=item.text_sha256)
        if resolution.status is not ReferenceResolutionStatus.RESOLVED:
            raise RetrievalIntegrityError(f"citation verification failed: {resolution.status.value}")
        excerpt = item.text if len(item.text) <= 240 else item.text[:237] + "..."
        return StaffSearchResult(f"result-{request.run_id}-{item.segment_id}", item.display_name,
                                 excerpt, item.line_start, item.line_end, item.character_start,
                                 item.character_end, item.representation_id, item.segment_id)

    def _load_source(self, matter_id: str, file_id: str, version_id: str):
        row = self.store.connection.execute(
            "SELECT sf.*,v.byte_size,v.sha256,v.stable_device,v.stable_inode,v.stable_mtime_ns,v.discovered_at,"
            "v.relative_path AS version_path,v.display_name AS version_name,v.media_type AS version_media "
            "FROM source_file sf JOIN source_version v ON v.source_file_id=sf.source_file_id "
            "WHERE sf.matter_id=? AND sf.source_file_id=? AND v.source_version_id=?",
            (matter_id, file_id, version_id),
        ).fetchone()
        if row is None:
            return None, None
        source_file = SourceFileRecord(source_file_id=row["source_file_id"], matter_id=row["matter_id"],
            source_location_id=row["source_location_id"], relative_path=row["relative_path"],
            display_name=row["display_name"], media_type=row["media_type"],
            availability=SourceFileAvailability(row["availability"]), first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"], current_source_version_id=row["current_source_version_id"])
        version = PersistedSourceVersion(source_version_id=version_id, source_file_id=file_id,
            matter_id=matter_id, source_location_id=row["source_location_id"], relative_path=row["version_path"],
            display_name=row["version_name"], media_type=row["version_media"], byte_size=row["byte_size"],
            sha256=row["sha256"], stable_device=row["stable_device"], stable_inode=row["stable_inode"],
            stable_mtime_ns=row["stable_mtime_ns"], discovered_at=row["discovered_at"])
        return source_file, version


class SourceUnavailable(PermissionError):
    pass
