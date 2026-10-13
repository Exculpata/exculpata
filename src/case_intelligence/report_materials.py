"""Authorized, content-verified saved work for report compilation.

Callers hold the source mutation guard and workspace lock while collecting a
snapshot. Generation happens after both guards have been released. Source
locators are resolved in bulk, with at most one fallback matter traversal for
legacy references that do not include a document identity.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
import re
import time
from typing import Mapping

from .answer_presentation import answer_content, modality_coverage_notice, rejected_answer_notice, review_rejection_notice
from .generation import LEGACY_VERIFICATION_OMISSION_NOTICE, VERIFICATION_OMISSION_NOTICE
from .report_compilation import CompilationMaterial
from .unit_stream import UnitRecordLimit
from .work_product_exports import MAX_EXPORT_TEXT_CHARS, validate_research_basis
from .workspace_store import MAX_REPORT_CITATION_EXCERPT_CHARS, MAX_REPORT_SECTION_CITATIONS, WorkspaceProblem, is_full_text_synthesis

MAX_SELECTIONS = 20
MAX_MATERIALS = 500
MAX_REFERENCES = 10_000
MAX_CITATION_CHARS = MAX_REPORT_CITATION_EXCERPT_CHARS
MAX_NOTEBOOK_PREVIEW_CHARS = 6_000
# Bound the canonical snapshot before hashing or serialization. Reserve half
# the export capacity for report prose, headings, attribution and generated
# findings. The final rendered report must also respect the export limit.
MAX_TOTAL_CITATION_CHARS = MAX_EXPORT_TEXT_CHARS // 2
# These bound validation work, including uncited units in frozen source checks.
# Each collection pass has its own budget; compilation collects once before
# generation and once before the atomic save.
MAX_SOURCE_SCAN_CHARS = 50_000_000
MAX_SOURCE_SCAN_UNITS = 100_000
MAX_SOURCE_SCAN_SERIALIZED_CHARS = 64_000_000
MAX_SOURCE_SCAN_RECORD_CHARS = 8_000_000
MAX_SOURCE_SCAN_SECONDS = 5.0
_STALE = (
    "Some selected source support changed or lacks an exact saved text version. "
    "Review the original source, then ask the question again in a new conversation "
    "and select that new answer or a newly reviewed source note. Merely reopening "
    "the old answer does not refresh its saved support."
)
_SCAN_LIMIT = "The selected saved work exceeds the report source-validation limit. Choose fewer or smaller sources; no report was saved."


def resolve_saved_answer_references(bench, matter, references, *, notebook_preview=False,
                                   source_navigation=False, source_windows=None):
    """Verify saved answer support before capture, under caller-owned guards.

    Reuse Report compilation's exact-text and legacy-token rules so a note or
    direct Report section cannot silently refresh a stale answer's citations.
    Notes retain their bounded display preview and the complete unit's digest;
    their preview is cut only after all saved support has been validated.
    Read-only comparisons may request transient, validated source navigation.
    """
    resolver = _References(bench, matter, notebook_preview=notebook_preview,
                           source_navigation=source_navigation, source_windows=source_windows)
    resolver.prepare(references)
    return tuple(resolver.resolve(value) for value in references)


def resolve_saved_answer_units(bench, matter, references):
    """Verify cited passages in one bounded scan and return their current units.

    The checks are the ones saving a passage uses (notebook-preview mode), so a
    long unit is bound by its complete digest without the report's per-citation
    length limit. Any changed or unavailable passage refuses the whole set.
    Returns (canonical reference, document, unit, unit ordinal) per reference.
    """
    resolver = _References(bench, matter, notebook_preview=True)
    resolver.prepare(references)
    resolved = []
    for value in references:
        reference = resolver.resolve(value)
        document, unit, candidate = resolver.resolved[value["support_token"]]
        resolved.append((reference, document, unit, int(candidate.chunk_id.removeprefix("chunk-"))))
    return tuple(resolved)


def _decision_review_status(machine, human):
    if (machine, human) in {("included", "exclude"), ("excluded", "include")}:
        return "disputed"
    if (machine, human) in {("included", "include"), ("excluded", "exclude"),
                            ("included", "agree"), ("excluded", "agree")}:
        return "confirmed"
    return "needs_review"


class _References:
    def __init__(self, bench, matter, *, notebook_preview=False, source_navigation=False, source_windows=None):
        self.bench, self.matter = bench, matter
        self.notebook_preview = notebook_preview
        self.source_navigation = source_navigation
        self.source_windows = source_windows or {}
        self.store = bench.source_store(matter)
        self.documents = {}
        self.resolved = {}
        self.bases = {}
        self.decision_sources = {}
        self.scanned = set()
        self.reference_counts = Counter()
        self.citation_chars = 0
        self.scan_chars = self.scan_units = 0
        self.serialized_chars = 0
        self.deadline = time.monotonic() + MAX_SOURCE_SCAN_SECONDS

    def check_budget(self):
        if (self.scan_units > MAX_SOURCE_SCAN_UNITS
                or self.scan_chars > MAX_SOURCE_SCAN_CHARS
                or self.serialized_chars > MAX_SOURCE_SCAN_SERIALIZED_CHARS
                or time.monotonic() >= self.deadline):
            raise WorkspaceProblem(_SCAN_LIMIT)

    def charge_read(self, characters):
        self.serialized_chars += characters
        self.check_budget()

    def digest(self, document_id, chunk_id, unit):
        actual = hashlib.sha256(unit.text.encode("utf-8")).hexdigest()
        if unit.excerpt_digest != actual:
            raise WorkspaceProblem(_STALE)
        return actual

    def document(self, document_id):
        if document_id not in self.documents:
            try:
                document = self.store.get(document_id)
                self.documents[document_id] = document
            except (KeyError, OSError, RuntimeError, ValueError) as exc:
                raise WorkspaceProblem(_STALE) from exc
        return self.documents[document_id]

    def _index(self, document_id, wanted):
        self.check_budget()
        document = self.document(document_id)
        if document.state != "ready":
            return
        self.scanned.add(document_id)
        pending = wanted - self.resolved.keys()
        expected_basis = self.decision_sources.get(document_id)
        basis = hashlib.sha256()
        basis.update(('{"source_version":' + json.dumps(document.version_id) + ',"units":[').encode())
        first = True
        try:
            for ordinal, unit in enumerate(self._units(document), 1):
                self.scan_units += 1
                self.scan_chars += len(unit.text)
                self.check_budget()
                if expected_basis is not None:
                    self.digest(document_id, f"chunk-{ordinal}", unit)
                    if not first:
                        basis.update(b",")
                    first = False
                    basis.update(json.dumps({
                        "number": unit.number, "digest": unit.excerpt_digest,
                        "line_start": unit.line_start, "line_end": unit.line_end,
                        "start_ms": unit.start_ms, "end_ms": unit.end_ms,
                    }, separators=(",", ":"), sort_keys=True).encode())
                if pending:
                    candidate = self.bench._candidate(self.matter, document, unit, ordinal)
                    for token in self.bench._support_tokens(candidate) & pending:
                        if token not in self.resolved:
                            if not self.notebook_preview and len(unit.text) > MAX_CITATION_CHARS:
                                raise WorkspaceProblem("A selected source passage exceeds the 6,000-character report limit. Choose a smaller supported passage.")
                            # Charge rendered occurrences, including ledger checks.
                            rendered_chars = (min(len(unit.text), MAX_NOTEBOOK_PREVIEW_CHARS)
                                if self.notebook_preview else len(unit.text))
                            self.citation_chars += rendered_chars * self.reference_counts[token]
                            if self.citation_chars > MAX_TOTAL_CITATION_CHARS:
                                raise WorkspaceProblem("The selected source passages exceed the report's total citation-text limit. Choose fewer items of saved work; source passages cannot be shortened safely.")
                            self.resolved[token] = (document, unit, candidate)
                            pending.remove(token)
                # Drain the container even after finding the last citation:
                # parsed_units previously validated its version and trailer.
        except WorkspaceProblem:
            raise
        except UnitRecordLimit as exc:
            raise WorkspaceProblem(_SCAN_LIMIT) from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise WorkspaceProblem(_STALE) from exc
        if expected_basis is not None:
            basis.update(b"]}")
            self.bases[document_id] = basis.hexdigest()
            if expected_basis != self.bases[document_id]:
                raise WorkspaceProblem(_STALE)

    def _units(self, document):
        self.check_budget()
        iterator = getattr(document, "iter_parsed_units", None)
        if iterator is None:
            raise WorkspaceProblem("This saved source has no bounded text reader. Reopen it before compiling a report.")
        yield from iterator(budget_check=self.check_budget, read_check=self.charge_read,
                            max_record_chars=MAX_SOURCE_SCAN_RECORD_CHARS)
        self.check_budget()

    def prepare(self, references):
        self.check_budget()
        if len(references) > MAX_REFERENCES:
            raise WorkspaceProblem("That selection has too many source references. Choose fewer items of saved work.")
        known = {}
        unknown = set()
        for value in references:
            self.check_budget()
            if not isinstance(value, Mapping):
                raise WorkspaceProblem("A saved finding has unreadable source support.")
            token = value.get("support_token", "")
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{40}", token):
                raise WorkspaceProblem(_STALE)
            self.reference_counts[token] += 1
            if value.get("matter_id") not in (None, "", self.matter.matter_id):
                raise WorkspaceProblem(_STALE)
            document_id = value.get("document_id")
            if document_id:
                if not isinstance(document_id, str) or not re.fullmatch(r"[0-9a-f]{32}", document_id):
                    raise WorkspaceProblem(_STALE)
                known.setdefault(document_id, set()).add(token)
            else:
                unknown.add(token)
        # Collect every wanted token before reading source text. A decision's
        # whole-source basis and all known/legacy citations share one scan.
        for document_id in dict.fromkeys((*known, *self.decision_sources)):
            tokens = known.get(document_id, set())
            self._index(document_id, tokens | unknown)
            if not tokens <= self.resolved.keys():
                raise WorkspaceProblem(_STALE)
        remaining = unknown - self.resolved.keys()
        if remaining:
            for document in self.store.ready_documents():
                if document.document_id in self.scanned:
                    continue
                self._index(document.document_id, remaining)
                remaining -= self.resolved.keys()
                if not remaining:
                    break
        if remaining:
            raise WorkspaceProblem(_STALE)

    def resolve(self, value):
        try:
            document, unit, candidate = self.resolved[value["support_token"]]
        except KeyError as exc:
            raise WorkspaceProblem(_STALE) from exc
        actual_digest = self.digest(document.document_id, candidate.chunk_id, unit)
        canonical = {
            "matter_id": self.matter.matter_id, "document_id": document.document_id,
            "source_version_id": document.version_id, "source_name": document.display_name,
            "location": candidate.citation, "unit_number": unit.number,
            "chunk_id": candidate.chunk_id, "excerpt_digest": actual_digest,
            "excerpt": unit.text, "support_token": value["support_token"],
            "kind": "transcript" if candidate.evidence_kind == "transcript" else "source",
        }
        for key in ("matter_id", "document_id", "source_version_id", "source_name", "location",
                    "unit_number", "chunk_id", "excerpt_digest"):
            if value.get(key) not in (None, "") and value[key] != canonical[key]:
                raise WorkspaceProblem(_STALE)
        for key in ("line_start", "line_end"):
            if key in value and value[key] != getattr(unit, key):
                raise WorkspaceProblem(_STALE)
        if value.get("evidence_kind") not in (None, "", candidate.evidence_kind):
            raise WorkspaceProblem(_STALE)
        if value.get("kind") not in (None, "", canonical["kind"]):
            raise WorkspaceProblem(_STALE)
        frozen_digest = value.get("excerpt_digest") == actual_digest
        frozen_excerpt = value.get("excerpt") == unit.text
        if "excerpt" in value and not frozen_excerpt:
            # Existing notebook previews retain a full-unit digest but display
            # a prefix. The digest verifies the complete canonical source text.
            if not frozen_digest or not isinstance(value["excerpt"], str) or not unit.text.startswith(value["excerpt"]):
                raise WorkspaceProblem(_STALE)
        if candidate.evidence_kind == "transcript" and not (frozen_digest or frozen_excerpt):
            # Transcript v2 tokens identify a moment and survive text edits.
            # Legacy digest-bound tokens remain valid only for their exact text.
            if value["support_token"] != self.bench._legacy_support_token(candidate):
                raise WorkspaceProblem(_STALE)
        if self.source_navigation:
            # Opt-in read-only comparison metadata, never added to stored notes
            # or Reports. These coordinates come from the validated original.
            canonical["source_navigation"] = {
                "ordinal": int(candidate.chunk_id.removeprefix("chunk-")),
                "start_ms": max(int(candidate.line_start or 0), 0)
                    if candidate.evidence_kind == "transcript" else None,
            }
        if self.notebook_preview:
            # The digest and locator above still bind the complete unit, including
            # text after this display prefix. Report callers never use this mode.
            if value["support_token"] in self.source_windows:
                from .answer_evidence import validated_window_text
                canonical["excerpt"] = validated_window_text(unit.text, self.source_windows[value["support_token"]])
            else:
                canonical["excerpt"] = unit.text[:MAX_NOTEBOOK_PREVIEW_CHARS]
        elif len(unit.text) > MAX_CITATION_CHARS:
            raise WorkspaceProblem("A selected source passage exceeds the 6,000-character report limit. Choose a smaller supported passage.")
        return canonical

    def validate_decision_source(self, item):
        self.check_budget()
        document = self.document(item.document_id)
        if document.state != "ready" or document.version_id != item.source_version_id or document.display_name != item.source_name:
            raise WorkspaceProblem(_STALE)
        if not item.source_basis_digest or self.decision_sources.get(item.document_id, item.source_basis_digest) != item.source_basis_digest:
            raise WorkspaceProblem(_STALE)
        self.decision_sources[item.document_id] = item.source_basis_digest


def snapshot_report_materials(bench, matter, actor: str, selections: tuple[str, ...], *,
                             full_text_support_resolver=None) -> tuple[CompilationMaterial, ...]:
    """Resolve saved work under both caller-owned source and workspace guards.

    The optional full-text adapter returns canonical citations for a fully
    validated frozen decision source, or None for a selected-passage run. It
    must validate the source even when the decision has no citations. The
    adapter runs again when compilation finishes, before the fingerprint and
    atomic save. This module has no dependency on full-text storage or jobs.
    """
    bench.workspace.membership(matter.matter_id, actor)
    if not selections or len(selections) > MAX_SELECTIONS or len(set(selections)) != len(selections):
        raise WorkspaceProblem("Choose between 1 and 20 items of saved work.")
    materials = {}
    full_text_material_ids = set()
    full_text_citation_chars = full_text_reference_count = 0
    validation_references = []
    resolver = _References(bench, matter)

    def add(*, material_id, origin, title, text, citations=(), review_status="needs_review", revision="", author="", date_label="", category="note", review_details="", raw_snapshot=None):
        resolver.check_budget()
        if not isinstance(text, str):
            raise WorkspaceProblem("A selected finding has unreadable text.")
        if not text.strip():
            return
        source_snapshot_digest = ""
        if raw_snapshot is not None and raw_snapshot != (text, review_details):
            source_snapshot_digest = hashlib.sha256(json.dumps(raw_snapshot,
                ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        material = CompilationMaterial(material_id=material_id, origin=origin,
            title=title, text=text, citations=tuple(citations), review_status=review_status,
            revision=revision, author=author, date_label=date_label, category=category,
            review_details=review_details, source_snapshot_digest=source_snapshot_digest)
        if material_id in materials:
            if material != materials[material_id]:
                raise WorkspaceProblem("Overlapping selections contain conflicting versions of saved work.")
            return
        if len(materials) >= MAX_MATERIALS:
            raise WorkspaceProblem("That selection contains more than 500 saved findings. Choose fewer items of saved work.")
        materials[material_id] = material

    def references(values, ledger=None):
        if not isinstance(values, list) or any(not isinstance(value, Mapping) for value in values):
            raise WorkspaceProblem("A saved finding has unreadable source support.")
        result = []
        for value in values:
            if ledger is not None:
                frozen = ledger.get(value.get("support_token"))
                if frozen is None:
                    raise WorkspaceProblem(_STALE)
                # Preserve and later validate supplied locator fields; a forged
                # field may not be overwritten by its valid ledger counterpart.
                for key, supplied in value.items():
                    if key in frozen and supplied not in (None, "") and supplied != frozen[key]:
                        raise WorkspaceProblem(_STALE)
                value = {**value, **frozen}
            result.append(dict(value))
        return tuple(result)

    def coverage(payload, prefix, revision, origin, *, generated=False):
        for key, title in (("evidence_notice", "Saved evidence notice"), ("review_scope", "Saved review scope"),
                           ("source_coverage", "Saved source coverage"), ("modality_coverage", "Saved media coverage")):
            value = payload.get(key)
            review_details = ""
            if isinstance(value, str):
                text = value
            elif isinstance(value, Mapping):
                text = str(value.get("notice") or value.get("summary") or "Recorded coverage")
                # Retain original numeric coverage and qualifications rather
                # than replacing them with a generic completeness statement.
                review_details = "Saved coverage values:\n" + json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
            elif value is None:
                continue
            else:
                raise WorkspaceProblem("A saved answer has unreadable coverage information.")
            raw_snapshot = (text, review_details)
            if generated and key == "modality_coverage":
                text = modality_coverage_notice(text)
                if isinstance(value, Mapping):
                    notice_key = "notice" if value.get("notice") else "summary"
                    projected = modality_coverage_notice(value.get(notice_key))
                    if projected != value.get(notice_key) and isinstance(value.get(notice_key), str):
                        review_details = "Coverage values for source review:\n" + json.dumps(
                            {**value, notice_key: projected}, ensure_ascii=False, sort_keys=True)
            add(material_id=f"{prefix}:{key}", origin=origin, title=title, text=text,
                revision=revision, category="coverage", author="AI assistance", review_details=review_details,
                raw_snapshot=raw_snapshot)

    def claims(payload, prefix, title, revision, origin, ledger=None, fallback=""):
        if not isinstance(payload, Mapping):
            raise WorkspaceProblem("A saved answer is malformed. Reopen it before compiling a report.")
        if payload.get('recorded_context_job'):
            raise WorkspaceProblem('Export this answer with its context-supplied receipt. Report compilation cannot yet preserve that notice; choose different saved work.')
        generated = (origin == "research" or payload.get("kind") in {"generated", "not-supported"}
                     or ("kind" not in payload and type(payload.get("answerable")) is bool))
        values = payload.get("claims", [])
        if not isinstance(values, list):
            raise WorkspaceProblem("A saved answer has unreadable findings.")
        for index, value in enumerate(values):
            if not isinstance(value, Mapping) or not isinstance(value.get("text"), str):
                raise WorkspaceProblem("A saved finding is malformed.")
            add(material_id=f"{prefix}:claim:{index}", origin=origin, title=title,
                text=value["text"], citations=references(value.get("citations", []), ledger), revision=revision,
                review_status="suggested", category="fact", author="AI assistance")
        notice = payload.get("verification_notice", "")
        if not isinstance(notice, str):
            raise WorkspaceProblem("A saved answer has an unreadable verification notice.")
        limitation = payload.get("source_limitation") if "source_limitation" in payload else payload.get("limitation")
        omitted = payload.get("omitted_claims", 0)
        if "source_limitation" not in payload and not notice and type(omitted) is int and omitted > 0:
            # Older answer payloads persisted the exact service notice appended
            # to their sourced limitation. Split only that known legacy shape;
            # never infer source support for service-authored text.
            if isinstance(limitation, Mapping) and isinstance(limitation.get("text"), str):
                legacy = limitation["text"]
                for known_notice in (LEGACY_VERIFICATION_OMISSION_NOTICE, VERIFICATION_OMISSION_NOTICE):
                    if legacy == known_notice or legacy.endswith(" " + known_notice):
                        sourced = legacy[:-len(known_notice)].rstrip()
                        limitation = {**limitation, "text": sourced} if sourced else None
                        notice = known_notice
                        break
        if limitation is not None:
            if not isinstance(limitation, Mapping) or not isinstance(limitation.get("text"), str):
                raise WorkspaceProblem("A saved answer has an unreadable qualification.")
            add(material_id=f"{prefix}:limitation", origin=origin, title="Saved qualification",
                text=limitation["text"], citations=references(limitation.get("citations", []), ledger),
                revision=revision, category="gap", author="AI assistance")
        if notice.strip():
            add(material_id=f"{prefix}:verification_notice", origin=origin, title="Saved verification notice",
                text=VERIFICATION_OMISSION_NOTICE if generated and notice == LEGACY_VERIFICATION_OMISSION_NOTICE else notice,
                revision=revision, category="coverage", author="AI assistance", raw_snapshot=(notice, ""))
        missing = payload.get("missing_information")
        if missing is not None and not isinstance(missing, str):
            raise WorkspaceProblem("A saved answer has unreadable missing-information notes.")
        if isinstance(missing, str) and missing.strip():
            presented = rejected_answer_notice(missing) if generated and (
                payload.get("kind") == "not-supported" or payload.get("answerable") is False) else missing
            add(material_id=f"{prefix}:gap", origin=origin, title="Unanswered question",
                text=presented, citations=references(payload.get("source_matches", []), ledger),
                revision=revision, category="gap", author="AI assistance", raw_snapshot=(missing, ""))
        elif not values and limitation is None:
            text = payload.get("introduction") or fallback
            if text:
                if not isinstance(text, str):
                    raise WorkspaceProblem("A selected finding has unreadable text.")
                introduction = payload.get("introduction", "")
                presented = answer_content(text, introduction) if payload.get("kind") == "generated" and isinstance(introduction, str) else text
                add(material_id=f"{prefix}:gap", origin=origin, title="Saved response requiring review",
                    text=presented, citations=references(payload.get("source_matches", []), ledger),
                    revision=revision, category="gap", author="AI assistance", raw_snapshot=(text, ""))
        coverage(payload, prefix, revision, origin, generated=generated)

    for selection in selections:
        kind, separator, identifier = selection.partition(":")
        if not separator:
            raise WorkspaceProblem("Choose saved work from this matter.")
        if kind == "research":
            job = bench.workspace.research_job(matter.matter_id, actor, identifier)
            if is_full_text_synthesis(job.plan, job.result):
                raise WorkspaceProblem("Export this full-text synthesis directly. Copying it into a Report is not supported yet.")
            if job.state != "succeeded":
                raise WorkspaceProblem("Choose an investigation that has finished.")
            validate_research_basis(matter, job)
            evidence = job.result.get("evidence", [])
            ledger = {}
            for value in evidence:
                token = value.get("support_token")
                if token in ledger and ledger[token] != value:
                    raise WorkspaceProblem(_STALE)
                ledger[token] = value
            validation_references.extend(ledger.values())
            claims(job.result["answer"], job.job_id, job.title, job.updated_at, "research", ledger)
            for index, entry in enumerate(job.result.get("passes", [])):
                if isinstance(entry.get("answer"), Mapping):
                    claims(entry["answer"], f"{job.job_id}:pass:{index}", entry.get("query", job.title), job.updated_at, "research", ledger)
                elif entry.get("text"):
                    add(material_id=f"{job.job_id}:pass-gap:{index}", origin="research", title=entry.get("query") or "Unanswered question",
                        text=rejected_answer_notice(entry["text"]) if isinstance(entry["text"], str) else entry["text"],
                        citations=references(entry.get("citations", []), ledger),
                        revision=job.updated_at, category="gap", author="AI assistance", raw_snapshot=(entry["text"], ""))
            for index, gap in enumerate(job.result.get("gaps", [])):
                if not isinstance(gap, Mapping):
                    raise WorkspaceProblem("An investigation has unreadable recorded gaps.")
                note = gap.get("note") or "No finding was saved."
                add(material_id=f"{job.job_id}:recorded-gap:{index}", origin="research",
                    title=gap.get("query") or "Unanswered question", text=rejected_answer_notice(note) if isinstance(note, str) else note,
                    revision=job.updated_at, category="gap", author="AI assistance", raw_snapshot=(note, ""))
            coverage_value = job.result.get("coverage", {})
            coverage({"review_scope": coverage_value}, f"{job.job_id}:scope", job.updated_at, "research")
        elif kind == "conversation":
            conversation = bench.workspace.get_conversation_any(matter.matter_id, identifier)
            for message in bench.workspace.messages(matter.matter_id, conversation.conversation_id):
                if message.role == "assistant":
                    claims(message.payload, message.message_id, conversation.title, message.created_at, "assistant", fallback=message.content)
                elif message.role == "user":
                    add(material_id=f"{message.message_id}:user", origin="human", title=f"Conversation context: {conversation.title}",
                        text=message.content, revision=message.created_at, author="Conversation participant",
                        review_status="needs_review", category="conversation_note")
        elif kind in {"notes", "note"}:
            if kind == "notes":
                if identifier != "active":
                    raise WorkspaceProblem("Choose the current team notes.")
                items = bench.workspace.all_notebook_items(matter.matter_id, actor, include_dismissed=False, limit=MAX_MATERIALS + 1)
            else:
                items = (bench.workspace.notebook_item(matter.matter_id, actor, identifier),)
            for item in items:
                if item.status == "dismissed":
                    raise WorkspaceProblem("A selected note was dismissed. Choose current review work.")
                refs = bench.workspace.notebook_references(matter.matter_id, actor, item.item_id)
                add(material_id=item.item_id, origin="human", title=item.title,
                    text=item.body or item.title, citations=tuple(asdict(ref) for ref in refs),
                    review_status=item.status, revision=item.updated_at, author=item.updated_by_name or item.created_by_name,
                    date_label=item.date_label, category=item.item_type)
        elif kind == "review":
            run = bench.workspace.review_run(matter.matter_id, actor, identifier)
            if run.state != "succeeded":
                raise WorkspaceProblem("Choose a source check that has finished.")
            counts = Counter()
            reviewed = total = 0
            full_text = False
            for item in bench.workspace.iter_review_decisions_for_report(matter.matter_id, actor, run.run_id):
                total += 1
                counts[item.machine_decision] += 1
                verified_citations = (full_text_support_resolver(matter, run, item)
                    if full_text_support_resolver is not None else None)
                if verified_citations is None:
                    if any(value.get("locator_kind") for value in item.citations):
                        raise WorkspaceProblem("This saved full-text check needs its source resolver before it can be copied into a Report.")
                    resolver.validate_decision_source(item)
                else:
                    if not isinstance(verified_citations, (tuple, list)) or len(verified_citations) > MAX_REPORT_SECTION_CITATIONS:
                        raise WorkspaceProblem("A report section can cite up to 100 passages.")
                    if len(verified_citations) != len(item.citations):
                        raise WorkspaceProblem("The saved review citation resolver did not preserve every selected passage.")
                    for value in verified_citations:
                        excerpt = value.get("excerpt") if isinstance(value, Mapping) else None
                        if not isinstance(excerpt, str) or not excerpt.strip():
                            raise WorkspaceProblem("A resolved full-text citation needs its complete source passage.")
                        if len(excerpt) > MAX_CITATION_CHARS:
                            raise WorkspaceProblem("A selected source passage exceeds the 6,000-character report limit. Export the original full-text ledger or select smaller supported work.")
                    # Full-text hydration returns workflow locator fields.
                    # Normalize to the same Report identity shape as the legacy
                    # resolver so mixed selections retain evidence-kind notices.
                    verified_citations = tuple({
                        **{key: value.get(key, "") for key in (
                            "matter_id", "document_id", "source_version_id", "source_name", "location",
                            "unit_number", "chunk_id", "excerpt_digest", "excerpt", "support_token")},
                        "kind": "transcript" if value.get("evidence_kind") == "transcript" or value.get("kind") == "transcript" else "source",
                    } for value in verified_citations)
                    full_text = True
                human = bool(item.human_decision)
                reviewed += human
                reviewer = bench.workspace.get_principal(item.reviewed_by).display_name if item.reviewed_by else "Team reviewer"
                raw_text = f"Machine screening: {item.machine_decision}.\n{item.rationale}"
                text = f"Machine screening: {item.machine_decision}.\n{review_rejection_notice(item.rationale)}"
                if verified_citations is not None:
                    text = "Saved full extracted-text screening summary; the complete reviewed ranges remain in the original ledger.\n" + text
                    raw_text = "Saved full extracted-text screening summary; the complete reviewed ranges remain in the original ledger.\n" + raw_text
                if item.error_message:
                    text += f"\nSaved screening limitation: {review_rejection_notice(item.error_message)}"
                    raw_text += f"\nSaved screening limitation: {item.error_message}"
                if human:
                    text += f"\nHuman decision: {item.human_decision}.\nHuman note: {item.human_note or 'None saved.'}"
                    raw_text += f"\nHuman decision: {item.human_decision}.\nHuman note: {item.human_note or 'None saved.'}"
                else:
                    text += "\nThis saved machine decision has not been reviewed by a person."
                    raw_text += "\nThis saved machine decision has not been reviewed by a person."
                material_id = f"{run.run_id}:{item.document_id}"
                if verified_citations is not None and material_id not in materials:
                    full_text_citation_chars += sum(len(value["excerpt"]) for value in verified_citations)
                    full_text_reference_count += len(verified_citations)
                    if full_text_citation_chars > MAX_TOTAL_CITATION_CHARS:
                        raise WorkspaceProblem("The selected source passages exceed the report's total citation-text limit. Choose fewer items of saved work.")
                    if full_text_reference_count > MAX_REFERENCES:
                        raise WorkspaceProblem("That selection has too many source references. Choose fewer items of saved work.")
                review_details = f"Frozen source-content basis: {item.source_basis_digest}" if verified_citations is not None else ""
                add(material_id=material_id, origin="human" if human else "source_review_ai",
                    title=item.source_name, text=text, citations=references(list(item.citations if verified_citations is None else verified_citations)),
                    review_status=_decision_review_status(item.machine_decision, item.human_decision),
                    revision=item.updated_at, author=reviewer if human else "AI screening",
                    category="decision" if human else "gap",
                    review_details=review_details, raw_snapshot=(raw_text, review_details))
                if verified_citations is not None:
                    full_text_material_ids.add(material_id)
            if total != run.snapshot_count:
                raise WorkspaceProblem("The saved source check is incomplete. Reopen it before compiling.")
            add(material_id=f"{run.run_id}:coverage", origin="source_review_ai", title="Saved source-check coverage",
                text=("Full extracted-text check: this Report copies saved source summaries and bounded supporting passages, not the complete range ledger.\n" if full_text else "") +
                     f"Saved decisions: {total}. Human-reviewed: {reviewed}. Awaiting human review: {total - reviewed}.\n" +
                     "; ".join(f"{key}: {count}" for key, count in sorted(counts.items())),
                revision=run.updated_at, category="coverage", author="AI screening")
        else:
            raise WorkspaceProblem("Choose saved work from this matter.")
    if not materials:
        raise WorkspaceProblem("There are no saved findings in that selection yet. Save an AI answer or add a review note first.")
    # Trusted adapters have already streamed and frozen-validated their source
    # population. Do not reload it through the legacy materializing resolver.
    verified_references = [ref for item in materials.values() if item.material_id in full_text_material_ids for ref in item.citations]
    resolver.citation_chars = full_text_citation_chars
    all_references = validation_references + [ref for item in materials.values() if item.material_id not in full_text_material_ids for ref in item.citations]
    if len(all_references) + len(verified_references) > MAX_REFERENCES:
        raise WorkspaceProblem("That selection has too many source references. Choose fewer items of saved work.")
    resolver.prepare(all_references)
    for value in validation_references:
        resolver.resolve(value)
    result = tuple(item if item.material_id in full_text_material_ids else
                   replace(item, citations=tuple(resolver.resolve(ref) for ref in item.citations)) for item in materials.values())
    resolver.check_budget()
    return result


def material_snapshot_fingerprint(matter_id: str, selections: tuple[str, ...], materials) -> str:
    value = {"matter": matter_id, "selections": selections, "materials": [asdict(item) for item in materials]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_compiled_material_citations(materials, sections):
    """Check draft citations against the current snapshot under its held guards.

    The caller must retain both source and workspace guards from snapshot
    collection through this check and the atomic save. Exact dictionary equality
    preserves every canonical identity field and passage; a generated citation
    cannot introduce a source outside the selected, freshly validated material.
    """
    current = {}
    for material in materials:
        for citation in material.citations:
            key = (citation.get("document_id"), citation.get("support_token"))
            if key in current and current[key] != citation:
                raise WorkspaceProblem("The selected source locator has conflicting current citations.")
            current[key] = citation
    for section in sections:
        for citation in section["citations"]:
            key = (citation.get("document_id"), citation.get("support_token"))
            if current.get(key) != citation:
                raise WorkspaceProblem("A compiled citation is outside the current validated selection. Retry the report from current saved work.")
