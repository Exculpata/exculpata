"""Portable, matter-scoped work-product exports generated entirely in memory."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape as xml_escape
from typing import Mapping, Sequence

from .answer_presentation import GENERATED_REVIEW_NOTICE, answer_content, answer_introduction, answer_limitation, modality_coverage_notice, research_content, rejected_answer_content, rejected_answer_notice, review_rejection_notice
from .branding import PRODUCT_NAME
from .derived_text import presentation_text
from .pdf_coverage import PDF_SAVED_RESULT_NOTICE
from .workspace_store import (
    ConversationRecord,
    MatterRecord,
    MessageRecord,
    NotebookItemRecord,
    NotebookReferenceRecord,
    ReportCitationRecord,
    ReportRecord,
    ReportSectionRecord,
    ResearchJobRecord,
    ReviewCriterionRecord,
    ReviewCriterionVersionRecord,
    ReviewDecisionRecord,
    ReviewRunRecord,
    SourceCatalogRecord,
    is_full_text_synthesis,
)

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"
ZIP_MEDIA_TYPE = "application/zip"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
JSON_MEDIA_TYPE = "application/json; charset=utf-8"
MAX_EXPORT_TEXT_CHARS = 10_000_000
MAX_DOCX_XML_BYTES = 100 * 1024 * 1024
MAX_DOCX_RUNS = 250_000
MAX_WORKFLOW_EXPORT_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_READABLE_REVIEW_DECISIONS = 500
_TEXT_PRESENTATION_NOTICE = (
    "Unsupported control or Unicode characters were replaced with spaces for this export. "
    "Original source text and saved citation references are unchanged."
)
_DOCX_LINE_ENDING_NOTICE = (
    "Carriage-return line endings were normalized to line breaks in this Word document. "
    "Original source text and saved citation references are unchanged."
)
_DOCX_CAPACITY_MESSAGE = (
    "This Word export contains too much text or formatting to prepare safely. "
    "Export a smaller selection or choose Markdown."
)
_DOCX_STYLE_MAP = {
    "title": "Title", "subtitle": "Subtitle", "heading1": "Heading1",
    "bullet": "ListBullet", "citation": "Citation", "note": "Note",
    "metadata": "Metadata", "footer": "Footer",
}
_DOCX_TEXT_RUN_OPEN = '<w:r><w:t xml:space="preserve">'
_DOCX_TEXT_RUN_CLOSE = "</w:t></w:r>"
_DOCX_TAB_RUN = "<w:r><w:tab/></w:r>"
_DOCX_BREAK_RUN = "<w:r><w:br/></w:r>"
_DOCX_DOCUMENT_OPEN = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body>'
)
_DOCX_DOCUMENT_CLOSE = (
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
    '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080"/></w:sectPr>'
    "</w:body></w:document>"
)

_PORTABLE_REVIEW_METRICS = (
    "sample_total",
    "reviewed_total",
    "adjudicated_total",
    "uncertain_total",
    "unscored_total",
    "true_positive",
    "false_positive",
    "false_negative",
    "true_negative",
    "precision",
    "recall",
    "elusion",
    "richness",
    "error_rate",
)


class ExportProblem(ValueError):
    """A bounded export problem safe to show to an authorized staff member."""


@dataclass(frozen=True)
class ExportArtifact:
    body: bytes
    media_type: str
    filename: str


@dataclass(frozen=True)
class ExportBlock:
    text: str
    style: str = "normal"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_file_stem(value: str, fallback: str = "recordbench-export") -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode(
        "ascii", errors="ignore"
    ).decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-.")[:90]
    if not stem or stem.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        stem = fallback
    return stem


def _plain(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def _staff_label(value: object) -> str:
    """Render a stored workflow value without exposing its machine spelling."""

    return _plain(value).replace("_", " ").title()


def _staff_actor_label(display_name: object) -> str:
    """Retain readable attribution without falling back to a principal key."""

    return _plain(display_name) or "Staff member"


def _source_excerpt(value: object) -> str:
    """Keep structured source snapshots exact; reject limits without truncation."""

    if not isinstance(value, str):
        return ""
    if len(value) > MAX_WORKFLOW_EXPORT_BYTES:
        raise ExportProblem("A source excerpt exceeds the export limit. Export a smaller selection.")
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ExportProblem(
            "A source excerpt contains invalid Unicode. Review its saved support before exporting again."
        ) from exc
    if encoded_size > MAX_WORKFLOW_EXPORT_BYTES:
        raise ExportProblem("A source excerpt exceeds the export limit. Export a smaller selection.")
    return value


def _portable_review_citation(value: object) -> dict[str, str] | None:
    """Whitelist the fields needed to resolve exported every-source support."""

    if not isinstance(value, Mapping):
        return None
    citation = {
        "source_name": _plain(value.get("source_name")),
        "location": _plain(value.get("location")),
        "excerpt": _source_excerpt(value.get("excerpt")),
    }
    if not citation["source_name"] or not citation["location"]:
        return None
    return citation


def _portable_review_citations(values: object) -> tuple[dict[str, str], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(
        citation
        for value in values
        if (citation := _portable_review_citation(value)) is not None
    )


def _review_citation_text(citation: Mapping[str, str]) -> str:
    return " — ".join(
        part
        for part in (
            citation.get("source_name", ""),
            citation.get("location", ""),
            _plain(citation.get("excerpt", "")),
        )
        if part
    )


def _validate_review_export_scope(
    matter: MatterRecord,
    criterion: ReviewCriterionRecord,
    version: ReviewCriterionVersionRecord,
    run: ReviewRunRecord,
    decisions: Sequence[ReviewDecisionRecord],
    *, frozen_text_sources: Sequence[SourceCatalogRecord] | None = None,
) -> None:
    if (
        criterion.matter_id != matter.matter_id
        or version.matter_id != matter.matter_id
        or run.matter_id != matter.matter_id
        or version.criterion_id != criterion.criterion_id
        or run.criterion_id != criterion.criterion_id
        or run.criterion_version_id != version.criterion_version_id
    ):
        raise ExportProblem("The source check crossed a matter boundary.")
    frozen = {}
    for source in frozen_text_sources or ():
        if source.matter_id != matter.matter_id or source.document_id in frozen:
            raise ExportProblem("The frozen text-review source catalog could not be resolved.")
        frozen[source.document_id] = source
    for item in decisions:
        if item.matter_id != matter.matter_id or item.run_id != run.run_id:
            raise ExportProblem("The source check crossed a matter boundary.")
        if item.machine_decision == "included" and not item.citations:
            raise ExportProblem(
                "An included source-check decision has no exact source support."
            )
        for citation in item.citations:
            if not isinstance(citation, Mapping):
                raise ExportProblem("A source-check citation is invalid.")
            source_name = _plain(citation.get("source_name"))
            location = _plain(citation.get("location"))
            excerpt = _plain(citation.get("excerpt"))
            support_token = _plain(citation.get("support_token"))
            excerpt_digest = _plain(citation.get("excerpt_digest"))
            chunk_id = _plain(citation.get("chunk_id"))
            unit_number = citation.get("unit_number")
            evidence_kind = _plain(citation.get("evidence_kind"))
            if (
                not source_name
                or not location
                or (not excerpt and frozen_text_sources is None)
                or not re.fullmatch(r"[0-9a-f]{40}", support_token)
                or not re.fullmatch(r"[0-9a-f]{64}", excerpt_digest)
                or not chunk_id
                or isinstance(unit_number, bool)
                or not isinstance(unit_number, int)
                or unit_number < 1
                or evidence_kind not in {"document", "transcript"}
            ):
                raise ExportProblem(
                    "A source-check citation does not have an exact location."
                )
            if frozen_text_sources is not None:
                source = frozen.get(item.document_id)
                if (source is None or source.source_state != "ready"
                        or source.version_id != item.source_version_id
                        or source.display_name != item.source_name
                        or source.content_basis_digest != item.source_basis_digest
                        or citation.get("locator_kind") != "text_unit"
                        or type(citation.get("unit_ordinal")) is not int
                        or citation["unit_ordinal"] < 1
                        or not re.fullmatch(r"[0-9a-f]{64}", _plain(citation.get("unit_digest")))
                        or excerpt or not item.error_message):
                    raise ExportProblem("A saved text-review location does not match the frozen source catalog.")
            if (
                _plain(citation.get("matter_id")) != matter.matter_id
                or _plain(citation.get("document_id")) != item.document_id
                or _plain(citation.get("source_version_id"))
                != item.source_version_id
                or source_name != item.source_name
            ):
                raise ExportProblem("A source-check citation did not match its source.")


def _validate_research_export_scope(
    matter: MatterRecord,
    job: ResearchJobRecord,
    result: Mapping[str, object],
) -> None:
    """Bind every derived investigation citation to its matter evidence ledger."""

    if job.matter_id != matter.matter_id:
        raise ExportProblem("Investigation export crossed a matter boundary.")
    if is_full_text_synthesis(job.plan, job.result) or is_full_text_synthesis(job.plan, result):
        from .full_text_synthesis import validate_job_input, input_notice
        try:
            receipt = validate_job_input(job, result)
            if result.get("coverage", {}).get("notice") != input_notice(receipt):
                raise ValueError("The full-text synthesis no longer matches its frozen review basis.")
        except (ValueError, KeyError, TypeError) as exc:
            raise ExportProblem("The full-text synthesis provenance or omission receipt is invalid.") from exc
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        raise ExportProblem("The investigation evidence ledger is invalid.")
    ledger: dict[str, Mapping[str, object]] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ExportProblem("The investigation evidence ledger is invalid.")
        matter_id = _plain(item.get("matter_id"))
        token = _plain(item.get("support_token"))
        excerpt_value = item.get("excerpt")
        excerpt = excerpt_value if isinstance(excerpt_value, str) else ""
        excerpt_digest = _plain(item.get("excerpt_digest"))
        chunk_id = _plain(item.get("chunk_id"))
        unit_number = item.get("unit_number")
        line_start = item.get("line_start")
        line_end = item.get("line_end")
        required = (
            _plain(item.get("document_id")),
            _plain(item.get("source_version_id")),
            _plain(item.get("source_name")),
            _plain(item.get("location")),
            _plain(excerpt),
            _plain(item.get("evidence_kind")),
        )
        if matter_id != matter.matter_id:
            raise ExportProblem("Investigation export crossed a matter boundary.")
        optional_lines_are_valid = all(
            value is None
            or (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            )
            for value in (line_start, line_end)
        )
        line_range_is_valid = (
            (line_start is None and line_end is None)
            or (
                line_start is not None
                and line_end is not None
                and line_end >= line_start
            )
        )
        if (
            not re.fullmatch(r"[0-9a-f]{40}", token)
            or not all(required)
            or token in ledger
            or not re.fullmatch(r"[0-9a-f]{64}", excerpt_digest)
            or excerpt_digest != hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            or not chunk_id
            or isinstance(unit_number, bool)
            or not isinstance(unit_number, int)
            or unit_number < 1
            or not optional_lines_are_valid
            or not line_range_is_valid
        ):
            raise ExportProblem("The investigation evidence ledger is invalid.")
        if required[-1] not in {"document", "transcript"}:
            raise ExportProblem("The investigation evidence ledger is invalid.")
        ledger[token] = item

    if job.plan.get("planner_version") == 1:
        from .investigation_planner import initial_query, validate_proposal
        source_text = {token: str(source["excerpt"]) for token, source in ledger.items()}
        seed_pending = True
        admitted_passages = 0
        for step in result.get("passes", []):
            if not isinstance(step, Mapping):
                raise ExportProblem("The investigation search record is invalid.")
            outcome = step.get("retrieval_outcome")
            hits = step.get("hit_count")
            selected = step.get("selected_passages")
            new_evidence = step.get("new_evidence")
            candidates = step.get("candidate_passages")
            candidate_sources = step.get("candidate_sources")
            analyzed = step.get("analyzed_units")
            counts = (selected, new_evidence, candidates, candidate_sources, analyzed)
            valid = all(type(value) is int and value >= 0 for value in counts)
            if (not valid or not isinstance(outcome, str) or not isinstance(step.get("status"), str)
                    or selected != new_evidence or analyzed > selected or candidate_sources > candidates):
                raise ExportProblem("The investigation search outcome counters are invalid.")
            if outcome == "unavailable":
                valid = (hits is None and candidates == selected == candidate_sources == 0
                         and step.get("status") == "retrieval_unavailable" and "answer" not in step)
            elif type(hits) is int and hits >= 0 and candidates == hits and selected <= hits:
                valid = (
                    (outcome == "zero_hits" and hits == selected == 0 and step.get("status") == "gap" and "answer" not in step)
                    or (outcome == "no_new_evidence" and hits > 0 and selected == 0 and step.get("status") == "duplicate" and "answer" not in step)
                    or (outcome == "new_evidence" and hits >= selected > 0
                        and step.get("status") in {"supported", "gap", "needs_review"})
                )
                valid = valid and (candidate_sources > 0 if hits else candidate_sources == 0)
            else:
                valid = False
            if not valid:
                raise ExportProblem("The investigation search outcome counters are invalid.")
            admitted_passages += new_evidence
            if seed_pending:
                if (step.get("query") != initial_query(job.question)
                        or step.get("reason") != "Initial reviewer question."
                        or step.get("motivating_support_token") or step.get("anchor")):
                    raise ExportProblem("The initial investigation search does not match its question.")
                seed_pending = (step.get("retrieval_outcome") == "unavailable"
                                and step.get("status") == "retrieval_unavailable"
                                and step.get("hit_count") is None
                                and step.get("selected_passages") == 0)
            elif validate_proposal({"query": step.get("query"), "anchor": step.get("anchor"),
                                    "reason": step.get("reason"), "support_token": step.get("motivating_support_token")}, source_text) is None:
                raise ExportProblem("An investigation search has no matching source-backed reason.")
        if admitted_passages != len(ledger):
            raise ExportProblem("The investigation search counts do not match its evidence ledger.")
        for proposal in result.get("pending_searches", []):
            if validate_proposal(proposal, source_text) is None:
                raise ExportProblem("An investigation proposal has no matching source-backed reason.")

    def validate_citation(value: object) -> None:
        if not isinstance(value, Mapping):
            raise ExportProblem("An investigation citation is invalid.")
        token = _plain(value.get("support_token"))
        source_name = _plain(value.get("source_name"))
        location = _plain(value.get("location"))
        evidence_kind = _plain(value.get("evidence_kind"))
        source = ledger.get(token)
        if (
            source is None
            or not source_name
            or not location
            or not evidence_kind
            or source_name != _plain(source.get("source_name"))
            or location != _plain(source.get("location"))
            or evidence_kind != _plain(source.get("evidence_kind"))
        ):
            raise ExportProblem(
                "An investigation citation did not match its evidence ledger."
            )
        for key in ("matter_id", "document_id", "source_version_id"):
            supplied = value.get(key)
            if supplied not in (None, "") and _plain(supplied) != _plain(source.get(key)):
                raise ExportProblem(
                    "An investigation citation did not match its evidence ledger."
                )
        supplied_excerpt = value.get("excerpt")
        if supplied_excerpt not in (None, "") and supplied_excerpt != source.get("excerpt"):
            raise ExportProblem(
                "An investigation citation did not match its evidence ledger."
            )

    def validate_citation_list(value: object) -> int:
        if not isinstance(value, list):
            raise ExportProblem("Investigation citations are invalid.")
        for citation in value:
            validate_citation(citation)
        return len(value)

    def validate_answer(value: object) -> tuple[int, str]:
        if not isinstance(value, Mapping):
            raise ExportProblem("The investigation answer is invalid.")
        if not isinstance(value.get("answerable"), bool):
            raise ExportProblem("The investigation answer is invalid.")
        introduction = value.get("introduction", "")
        missing_information = value.get("missing_information", "")
        if not isinstance(introduction, str) or not isinstance(
            missing_information, str
        ):
            raise ExportProblem("The investigation answer is invalid.")
        citation_count = 0
        claims = value.get("claims", [])
        if not isinstance(claims, list):
            raise ExportProblem("The investigation answer is invalid.")
        claim_texts: list[str] = []
        for claim in claims:
            if not isinstance(claim, Mapping) or not isinstance(
                claim.get("text", ""), str
            ):
                raise ExportProblem("The investigation answer is invalid.")
            claim_texts.append(str(claim.get("text", "")))
            citation_count += validate_citation_list(claim.get("citations", []))
        limitation = value.get("limitation")
        limitation_text = ""
        if limitation is not None:
            if not isinstance(limitation, Mapping) or not isinstance(
                limitation.get("text", ""), str
            ):
                raise ExportProblem("The investigation answer is invalid.")
            limitation_text = str(limitation.get("text", ""))
            citation_count += validate_citation_list(
                limitation.get("citations", [])
            )
        if "source_matches" in value:
            citation_count += validate_citation_list(value.get("source_matches"))
        if value.get("answerable") and citation_count == 0:
            raise ExportProblem("The investigation answer has no exact source support.")
        if value.get("answerable"):
            parts = [introduction, *claim_texts]
            if limitation is not None:
                parts.append(f"Limitation: {limitation_text}")
            answer_text = "\n".join(part for part in parts if part)
        else:
            answer_text = missing_information or introduction
        return citation_count, answer_text

    answer = result.get("answer")
    if answer is None:
        raise ExportProblem("The investigation answer is invalid.")
    _answer_citations, answer_text = validate_answer(answer)
    if result.get("summary") != answer_text:
        raise ExportProblem(
            "The investigation synthesis does not match its saved answer."
        )
    hierarchy = result.get("hierarchical_synthesis")
    if hierarchy is not None or job.plan.get("synthesis_version") == 1:
        from .hierarchical_synthesis import validate_state, synthesis_answer
        try:
            validate_state(hierarchy, result.get("passes", []), evidence, final=True)
            expected = synthesis_answer(hierarchy, evidence)
            if result["answer"].get("evidence_notice", "") != expected.evidence_notice:
                raise ValueError("The synthesis lost its evidence caution.")
            if result.get("summary") != expected.text:
                raise ValueError("Final synthesis differs from its saved matter sections.")
            actual_claims = result["answer"]["claims"]
            if len(actual_claims) != len(expected.claims):
                raise ValueError("Final claims differ from the synthesis checkpoint.")
            for actual, claim in zip(actual_claims, expected.claims):
                tokens = [evidence[int(identifier[1:]) - 1]["support_token"] for identifier in claim.evidence_ids]
                if actual["text"] != claim.text or [item["support_token"] for item in actual["citations"]] != tokens:
                    raise ValueError("Final claim lost its intermediate-to-original support.")
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise ExportProblem("The hierarchical synthesis provenance is invalid.") from exc
    passes = result.get("passes", [])
    if not isinstance(passes, list):
        raise ExportProblem("The investigation findings are invalid.")
    for item in passes:
        if not isinstance(item, Mapping):
            raise ExportProblem("The investigation findings are invalid.")
        citation_count = 0
        if "answer" in item:
            answer_citations, pass_text = validate_answer(item.get("answer"))
            citation_count += answer_citations
            if item.get("text") != pass_text:
                raise ExportProblem(
                    "An investigation finding does not match its saved answer."
                )
        else:
            expected_text = {
                "gap": "No searchable passage matched this part of the research plan.",
                "duplicate": "No new passage was selected from this search.",
                "retrieval_unavailable": "Retrieval was unavailable; this search does not establish zero hits.",
                "needs_review": (
                    "Potential passages were found, but this research step did not "
                    "produce a source-verified finding."
                ),
            }.get(item.get("status"))
            if expected_text is None or item.get("text") != expected_text:
                raise ExportProblem(
                    "An investigation finding does not match its verified result."
                )
        if "citations" in item:
            citation_count += validate_citation_list(item.get("citations"))
        if item.get("status") == "supported" and citation_count == 0:
            raise ExportProblem(
                "An investigation finding has no exact source support."
            )


def validate_research_basis(matter: MatterRecord, job: ResearchJobRecord) -> None:
    """Check saved findings and summary against the exact ledger before reuse."""
    _validate_research_export_scope(matter, job, job.result)


def _validate_report_export_scope(
    matter: MatterRecord,
    report: ReportRecord,
    sections: Sequence[
        tuple[ReportSectionRecord, Sequence[ReportCitationRecord]]
    ],
) -> None:
    if report.matter_id != matter.matter_id:
        raise ExportProblem("The report crossed a matter boundary.")
    source_names: dict[tuple[str, str], str] = {}
    for section, citations in sections:
        if (
            section.matter_id != matter.matter_id
            or section.report_id != report.report_id
        ):
            raise ExportProblem("The report crossed a matter boundary.")
        for citation in citations:
            if (
                citation.matter_id != matter.matter_id
                or citation.report_id != report.report_id
                or citation.section_id != section.section_id
            ):
                raise ExportProblem("A report citation crossed a matter boundary.")
            if (
                not re.fullmatch(r"[0-9a-f]{32}", citation.document_id or "")
                or not re.fullmatch(r"[0-9a-f]{32}", citation.source_version_id or "")
                or not _plain(citation.source_name)
                or not _plain(citation.location)
            ):
                raise ExportProblem("A report citation has no exact source location.")
            source_key = (citation.document_id, citation.source_version_id)
            prior_name = source_names.setdefault(source_key, citation.source_name)
            if prior_name != citation.source_name:
                raise ExportProblem("A report citation did not match its source.")
            if citation.kind in {"source", "transcript"}:
                if (
                    not re.fullmatch(r"[0-9a-f]{40}", citation.support_token or "")
                    or citation.media_clip_id
                ):
                    raise ExportProblem("A report citation did not match its source.")
            elif citation.kind == "media_clip":
                if (
                    not re.fullmatch(
                        r"media-clip-[0-9a-f]{32}", citation.media_clip_id or ""
                    )
                    or citation.support_token
                    or citation.start_ms < 0
                    or citation.end_ms <= citation.start_ms
                ):
                    raise ExportProblem("A report citation did not match its source.")
            else:
                raise ExportProblem("A report citation has an invalid type.")


def _portable_media_review_status(value: object) -> str:
    return "Staff edits saved" if _plain(value) == "human_reviewed" else "Unconfirmed"


def _portable_overview_coverage(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        covered = int(value.get("covered_segment_count") or 0)
        total = int(value.get("total_segment_count") or 0)
    except (TypeError, ValueError) as exc:
        raise ExportProblem("Transcript overview coverage is invalid.") from exc
    return {
        "scope": "Full transcript"
        if value.get("mode") == "full"
        else "Representative passages",
        "covered_passages": covered,
        "total_passages": total,
    }


def _citations(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source = _plain(item.get("source_name"))
        location = _plain(item.get("location"))
        if source and location:
            result.append(f"{source} — {location}")
        elif source:
            result.append(source)
    return tuple(result)


def _answer_blocks(message: MessageRecord) -> list[ExportBlock]:
    payload = message.payload
    kind = payload.get("kind")
    blocks: list[ExportBlock] = []
    if payload.get('context_supplied'):
        blocks.append(ExportBlock('Context supplied for this answer — as submitted', 'heading1'))
        blocks.append(ExportBlock(payload['context_supplied']['notice'], 'note'))
        # Complete JSON in the answer export preserves exact serialized requests,
        # versions, attribution and omission reasons without inventing citations.
        blocks.append(ExportBlock(json.dumps(payload['context_supplied'], ensure_ascii=False, indent=2), 'note'))
    source_coverage = payload.get("source_coverage")
    if (
        isinstance(source_coverage, Mapping)
        and source_coverage.get("mode") == "partial"
    ):
        coverage_notice = _plain(source_coverage.get("notice"))
        if coverage_notice:
            blocks.append(ExportBlock(f"Source coverage: {coverage_notice}", "note"))
    review_scope = payload.get("review_scope")
    if isinstance(review_scope, Mapping):
        scope_notice = _plain(review_scope.get("notice"))
        if scope_notice:
            prefix = (
                "Completeness caution"
                if review_scope.get("collection_wide_request") is True
                else "Answer basis"
            )
            blocks.append(ExportBlock(f"{prefix}: {scope_notice}", "note"))
    modality = payload.get("modality_coverage")
    if isinstance(modality, Mapping):
        notice = _plain(modality_coverage_notice(modality.get("notice")))
        if notice:
            blocks.append(ExportBlock(f"Requested source coverage: {notice}", "note"))
    if kind == "generated":
        blocks.append(ExportBlock(GENERATED_REVIEW_NOTICE, "note"))
        introduction = answer_introduction(_plain(payload.get("introduction")))
        if introduction:
            blocks.append(ExportBlock(introduction))
        evidence_notice = _plain(payload.get("evidence_notice"))
        if evidence_notice:
            blocks.append(ExportBlock(evidence_notice, "note"))
        claims = payload.get("claims")
        if isinstance(claims, list):
            for claim in claims:
                if not isinstance(claim, Mapping):
                    continue
                text = _plain(claim.get("text"))
                if text:
                    blocks.append(ExportBlock(text, "bullet"))
                for citation in _citations(claim.get("citations")):
                    blocks.append(ExportBlock(f"Source: {citation}", "citation"))
        limitation = payload.get("limitation")
        if isinstance(limitation, Mapping):
            text = _plain(answer_limitation(payload))
            if text:
                blocks.append(ExportBlock(f"Limitation: {text}", "note"))
            for citation in _citations(limitation.get("citations")):
                blocks.append(ExportBlock(f"Source: {citation}", "citation"))
    elif kind == "not-supported":
        text = _plain(rejected_answer_notice(payload.get("missing_information"))) or rejected_answer_notice(message.content)
        blocks.append(ExportBlock(text, "note"))
        for citation in _citations(payload.get("source_matches")):
            blocks.append(ExportBlock(f"Related source passage: {citation}", "citation"))
    else:
        blocks.append(ExportBlock(message.content, "note"))
    if not blocks:
        blocks.append(ExportBlock(message.content))
    return blocks


def answer_blocks(
    matter: MatterRecord,
    conversation: ConversationRecord,
    answer: MessageRecord,
    question: MessageRecord | None = None,
    *,
    exported_at: str | None = None,
    presentation_notice: str = PDF_SAVED_RESULT_NOTICE,
) -> tuple[ExportBlock, ...]:
    if (
        conversation.matter_id != matter.matter_id
        or answer.role != "assistant"
        or answer.conversation_id != conversation.conversation_id
        or (
            question is not None
            and question.conversation_id != conversation.conversation_id
        )
    ):
        raise ExportProblem("That saved item is not an assistant answer in this conversation.")
    blocks = [
        ExportBlock(matter.display_name, "title"),
        ExportBlock(conversation.title, "subtitle"),
        ExportBlock(f"Exported from {PRODUCT_NAME} at {exported_at or _now()}.", "metadata"),
    ]
    if question is not None:
        blocks.extend(
            (
                ExportBlock("Question", "heading1"),
                ExportBlock(question.content),
            )
        )
    blocks.append(ExportBlock(f"{PRODUCT_NAME} answer", "heading1"))
    if presentation_notice:
        blocks.append(ExportBlock(presentation_notice, "note"))
    blocks.extend(_answer_blocks(answer))
    blocks.append(
        ExportBlock(
            "Review this work product against the cited source material before relying on it.",
            "footer",
        )
    )
    return tuple(blocks)


def conversation_blocks(
    matter: MatterRecord,
    conversation: ConversationRecord,
    messages: Sequence[MessageRecord],
    *,
    exported_at: str | None = None,
    presentation_notice: str = PDF_SAVED_RESULT_NOTICE,
) -> tuple[ExportBlock, ...]:
    if (
        conversation.matter_id != matter.matter_id
        or any(
            message.conversation_id != conversation.conversation_id
            for message in messages
        )
    ):
        raise ExportProblem("Conversation export crossed a matter boundary.")
    blocks = [
        ExportBlock(matter.display_name, "title"),
        ExportBlock(conversation.title, "subtitle"),
        ExportBlock(f"Exported from {PRODUCT_NAME} at {exported_at or _now()}.", "metadata"),
    ]
    question_number = 0
    if presentation_notice:
        blocks.append(ExportBlock(presentation_notice, "note"))
    answer_number = 0
    for message in messages:
        if message.role == "user":
            question_number += 1
            blocks.append(ExportBlock(f"Question {question_number}", "heading1"))
            blocks.append(ExportBlock(message.content))
        else:
            answer_number += 1
            blocks.append(
                ExportBlock(f"{PRODUCT_NAME} answer {answer_number}", "heading1")
            )
            blocks.extend(_answer_blocks(message))
    if not messages:
        blocks.append(ExportBlock("This conversation does not contain any messages yet.", "note"))
    blocks.append(
        ExportBlock(
            "Review generated work product against the cited source material before relying on it.",
            "footer",
        )
    )
    return tuple(blocks)


def _validate_notebook_scope(
    matter: MatterRecord,
    entries: Sequence[tuple[NotebookItemRecord, Sequence[NotebookReferenceRecord]]],
) -> None:
    for item, references in entries:
        if item.matter_id != matter.matter_id:
            raise ExportProblem("Notebook export crossed a matter boundary.")
        if any(
            reference.matter_id != matter.matter_id
            or reference.item_id != item.item_id
            for reference in references
        ):
            raise ExportProblem("Notebook export crossed a matter boundary.")


def notebook_blocks(
    matter: MatterRecord,
    entries: Sequence[tuple[NotebookItemRecord, Sequence[NotebookReferenceRecord]]],
    *,
    exported_at: str | None = None,
    heading: str = "Matter notebook",
) -> tuple[ExportBlock, ...]:
    _validate_notebook_scope(matter, entries)
    blocks = [
        ExportBlock(matter.display_name, "title"),
        ExportBlock(heading, "subtitle"),
        ExportBlock(f"Exported from {PRODUCT_NAME} at {exported_at or _now()}.", "metadata"),
    ]
    for index, (item, references) in enumerate(entries, 1):
        blocks.append(ExportBlock(f"{index}. {item.title}", "heading1"))
        blocks.append(
            ExportBlock(
                " · ".join(
                    value
                    for value in (
                        item.item_type.replace("_", " ").title(),
                        item.status.replace("_", " ").title(),
                        "Pinned" if item.is_pinned else "",
                    )
                    if value
                ),
                "metadata",
            )
        )
        if item.date_label:
            blocks.append(ExportBlock(f"Date: {item.date_label}"))
        if item.body:
            blocks.append(ExportBlock(item.body))
        blocks.append(
            ExportBlock(
                f"Created by {_staff_actor_label(item.created_by_name)} at {item.created_at}; "
                f"last updated by {_staff_actor_label(item.updated_by_name)} at {item.updated_at}.",
                "metadata",
            )
        )
        for reference in references:
            blocks.append(
                ExportBlock(
                    f"Source: {reference.source_name} — {reference.location}",
                    "citation",
                )
            )
            blocks.append(ExportBlock(reference.excerpt, "citation"))
    if not entries:
        blocks.append(ExportBlock("No notebook items were present.", "note"))
    blocks.append(
        ExportBlock(
            "Suggested and disputed notebook items require review against their cited source material.",
            "footer",
        )
    )
    return tuple(blocks)


def _csv_safe(value: object) -> str:
    text = presentation_text(_plain(value))
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def notebook_csv(
    entries: Sequence[tuple[NotebookItemRecord, Sequence[NotebookReferenceRecord]]]
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        (
            "Type", "Status", "Title", "Date", "Details", "Pinned", "Origin",
            "Created by", "Created at", "Updated by", "Updated at", "Sources",
        )
    )
    for item, references in entries:
        writer.writerow(
            (
                _csv_safe(_staff_label(item.item_type)),
                _csv_safe(_staff_label(item.status)),
                _csv_safe(item.title),
                _csv_safe(item.date_label),
                _csv_safe(item.body),
                "yes" if item.is_pinned else "no",
                _csv_safe(_staff_label(item.origin)),
                _csv_safe(_staff_actor_label(item.created_by_name)),
                item.created_at,
                _csv_safe(_staff_actor_label(item.updated_by_name)),
                item.updated_at,
                _csv_safe(
                    "; ".join(
                        f"{reference.source_name} — {reference.location}"
                        for reference in references
                    )
                ),
            )
        )
    encoded = ("\ufeff" + output.getvalue()).encode("utf-8")
    if len(encoded) > MAX_EXPORT_TEXT_CHARS:
        raise ExportProblem("This notebook is too large to export as one CSV file.")
    return encoded


def matter_report_blocks(
    matter: MatterRecord,
    conversations: Sequence[tuple[ConversationRecord, Sequence[MessageRecord]]],
    sources: Sequence[Mapping[str, object]],
    notebook_count: int = 0,
    transcript_count: int = 0,
    *,
    exported_at: str,
) -> tuple[ExportBlock, ...]:
    message_count = sum(len(messages) for _, messages in conversations)
    blocks = [
        ExportBlock(matter.display_name, "title"),
        ExportBlock("Matter work-product report", "subtitle"),
        ExportBlock(f"Exported from {PRODUCT_NAME} at {exported_at}.", "metadata"),
    ]
    if matter.descriptor:
        blocks.append(ExportBlock(matter.descriptor))
    blocks.extend(
        (
            ExportBlock("Bundle contents", "heading1"),
            ExportBlock(f"{len(conversations)} saved conversation(s)", "bullet"),
            ExportBlock(f"{message_count} saved message(s)", "bullet"),
            ExportBlock(f"{len(sources)} source inventory record(s)", "bullet"),
            ExportBlock(f"{notebook_count} matter notebook item(s)", "bullet"),
            ExportBlock(f"{transcript_count} reviewed transcript export(s)", "bullet"),
            ExportBlock(
                "Original source files are not included. This bundle contains work product and source references only.",
                "note",
            ),
            ExportBlock("Source inventory", "heading1"),
        )
    )
    if any(source.get("kind") == "PDF" for source in sources):
        from .pdf_coverage import PDF_COVERAGE_NOTICE

        blocks.append(ExportBlock(PDF_COVERAGE_NOTICE, "note"))
    if sources:
        for source in sources:
            label = _plain(source.get("name")) or "Unnamed source"
            kind = _plain(source.get("kind"))
            state = _plain(source.get("state"))
            count_label = _plain(source.get("count_label"))
            details = " · ".join(value for value in (kind, count_label, state) if value)
            blocks.append(ExportBlock(f"{label}{' — ' + details if details else ''}", "bullet"))
    else:
        blocks.append(ExportBlock("No source inventory records were present.", "note"))
    blocks.append(ExportBlock("Conversation index", "heading1"))
    for index, (conversation, messages) in enumerate(conversations, 1):
        blocks.append(
            ExportBlock(
                f"{index}. {conversation.title} — {len(messages)} message(s), last activity {conversation.updated_at}",
                "bullet",
            )
        )
    if not conversations:
        blocks.append(ExportBlock("No saved conversations were present.", "note"))
    return tuple(blocks)


def _markdown_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in ("*", "_", "[", "]", "<", ">", "#", "`", "|"):
        escaped = escaped.replace(character, "\\" + character)
    return escaped


def blocks_to_markdown(blocks: Sequence[ExportBlock]) -> bytes:
    parts: list[str] = []
    total = 0
    normalized = False
    for block in blocks:
        presented = presentation_text(block.text)
        normalized = normalized or presented != block.text
        value = _markdown_escape(presented)
        total += len(value)
        if total > MAX_EXPORT_TEXT_CHARS:
            raise ExportProblem(
                "This export is too large to prepare at once. Export the conversations individually."
            )
        if block.style == "title":
            rendered = f"# {value}"
        elif block.style == "subtitle":
            rendered = f"## {value}"
        elif block.style == "heading1":
            rendered = f"### {value}"
        elif block.style == "bullet":
            rendered = f"- {value}"
        elif block.style in {"citation", "metadata", "footer"}:
            rendered = f"> {value.replace(chr(10), chr(10) + '> ')}"
        elif block.style == "note":
            rendered = f"**Note:** {value}"
        else:
            rendered = value
        parts.append(rendered)
    if normalized:
        if total + len(_TEXT_PRESENTATION_NOTICE) > MAX_EXPORT_TEXT_CHARS:
            raise ExportProblem(
                "This export is too large to prepare at once. Export the conversations individually."
            )
        parts.append(f"**Note:** {_TEXT_PRESENTATION_NOTICE}")
    return ("\n\n".join(parts).rstrip() + "\n").encode("utf-8")


def _xml_text(value: str) -> str:
    """Escape derived text only after replacing characters XML cannot carry.

    HTML escaping alone leaves XML-invalid controls and surrogate code points
    intact. Keep this defense at serialization so historical stored citations
    and their digest/locator basis remain unchanged.
    """

    # XML normalizes literal CR (including CRLF) before exposing text to a
    # reader. A reference preserves the supported character through parsing;
    # introduce it after escaping so literal source text such as "&#13;" stays
    # literal instead of becoming a character reference of its own.
    return xml_escape(presentation_text(value), quote=False).replace("\r", "&#13;")


def _paragraph_open(block: ExportBlock) -> str:
    style = _DOCX_STYLE_MAP.get(block.style, "Normal")
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'


def _xml_text_byte_count(value: str, *, body: bool = False) -> int:
    """Count escaped UTF-8 bytes without constructing an expanded XML string."""

    total = 0
    for start in range(0, len(value), 16_384):
        part = presentation_text(value[start:start + 16_384])
        total += (len(part.encode("utf-8")) + 4 * part.count("&")
                  + 3 * (part.count("<") + part.count(">")))
        if body:
            # Body delimiters become runs, counted separately. Removing both
            # CR/LF bytes also handles a CRLF pair split across these chunks.
            total -= part.count("\t") + part.count("\r") + part.count("\n")
        else:
            total += 4 * part.count("\r")  # _xml_text emits &#13;.
        if total > MAX_DOCX_XML_BYTES:
            raise ExportProblem(_DOCX_CAPACITY_MESSAGE)
    return total


def _check_docx_capacity(
    blocks: Sequence[ExportBlock], *, title: str, created_at: str, fixed_xml_bytes: int,
) -> None:
    """Reject expanded XML and run-list growth before allocating either one."""

    xml_bytes = fixed_xml_bytes
    for value in (title, PRODUCT_NAME, created_at):
        # One input character needs at least one escaped byte, even when the
        # presentation policy replaces it. Refuse huge metadata before copying.
        if xml_bytes + len(value) > MAX_DOCX_XML_BYTES:
            raise ExportProblem(_DOCX_CAPACITY_MESSAGE)
        xml_bytes += _xml_text_byte_count(value)
    runs = 0
    for block in blocks:
        tabs = block.text.count("\t")
        breaks = (block.text.count("\n") + block.text.count("\r")
                  - block.text.count("\r\n"))
        text_runs = 1 + tabs + breaks
        runs += text_runs + tabs + breaks
        if runs > MAX_DOCX_RUNS:
            raise ExportProblem(_DOCX_CAPACITY_MESSAGE)
        xml_bytes += (
            len(_paragraph_open(block)) + len("</w:p>")
            + text_runs * (len(_DOCX_TEXT_RUN_OPEN) + len(_DOCX_TEXT_RUN_CLOSE))
            + tabs * len(_DOCX_TAB_RUN) + breaks * len(_DOCX_BREAK_RUN)
        )
        if xml_bytes > MAX_DOCX_XML_BYTES:
            raise ExportProblem(_DOCX_CAPACITY_MESSAGE)
        xml_bytes += _xml_text_byte_count(block.text, body=True)
    if xml_bytes > MAX_DOCX_XML_BYTES:
        raise ExportProblem(_DOCX_CAPACITY_MESSAGE)


def _paragraph_xml(block: ExportBlock) -> str:
    runs: list[str] = []
    lines = block.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if index:
            runs.append(_DOCX_BREAK_RUN)
        for part_index, part in enumerate(line.split("\t")):
            if part_index:
                runs.append(_DOCX_TAB_RUN)
            runs.append(
                _DOCX_TEXT_RUN_OPEN
                + _xml_text(part)
                + _DOCX_TEXT_RUN_CLOSE
            )
    return _paragraph_open(block) + "".join(runs) + "</w:p>"


def blocks_to_docx(
    blocks: Sequence[ExportBlock], *, title: str, created_at: str
) -> bytes:
    total = sum(len(block.text) for block in blocks)
    if total > MAX_EXPORT_TEXT_CHARS:
        raise ExportProblem(
            "This export is too large to prepare at once. Export the conversations individually."
        )
    normalized = (
        presentation_text(title) != title
        or presentation_text(created_at) != created_at
        or any(presentation_text(block.text) != block.text for block in blocks)
    )
    notices: list[ExportBlock] = []
    if normalized:
        notices.append(ExportBlock(_TEXT_PRESENTATION_NOTICE, "note"))
    if any("\r" in block.text for block in blocks):
        notices.append(ExportBlock(_DOCX_LINE_ENDING_NOTICE, "note"))
    if notices:
        if total + sum(len(notice.text) for notice in notices) > MAX_EXPORT_TEXT_CHARS:
            raise ExportProblem(
                "This export is too large to prepare at once. Export the conversations individually."
            )
        blocks = (*blocks, *notices)
    # Larger title glyphs can consume an implicit half-inch tab stop in Word
    # readers. Explicit one-inch stops keep title word boundaries visible.
    styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Subtitle"/><w:pPr><w:tabs><w:tab w:val="left" w:pos="1440"/><w:tab w:val="left" w:pos="2880"/><w:tab w:val="left" w:pos="4320"/><w:tab w:val="left" w:pos="5760"/><w:tab w:val="left" w:pos="7200"/><w:tab w:val="left" w:pos="8640"/><w:tab w:val="left" w:pos="10080"/></w:tabs></w:pPr><w:rPr><w:b/><w:color w:val="0A1630"/><w:sz w:val="38"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="46566E"/><w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="280" w:after="100"/></w:pPr><w:rPr><w:b/><w:color w:val="0A1630"/><w:sz w:val="27"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="420" w:hanging="240"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Citation"><w:name w:val="Citation"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="420"/><w:spacing w:after="80"/></w:pPr><w:rPr><w:color w:val="1C4A82"/><w:sz w:val="19"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Note"><w:name w:val="Note"/><w:basedOn w:val="Normal"/><w:rPr><w:i/><w:color w:val="6A554B"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Metadata"><w:name w:val="Metadata"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="687487"/><w:sz w:val="18"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="Footer"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="300"/></w:pPr><w:rPr><w:i/><w:color w:val="687487"/><w:sz w:val="18"/></w:rPr></w:style>
</w:styles>"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""
    package_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""
    document_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
    core_template = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator>
<dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>
</cp:coreProperties>"""
    fixed_parts = (
        content_types, package_rels, document_rels, styles,
        core_template.format(title="", creator="", created_at=""),
        _DOCX_DOCUMENT_OPEN, _DOCX_DOCUMENT_CLOSE,
    )
    _check_docx_capacity(
        blocks, title=title, created_at=created_at,
        fixed_xml_bytes=sum(len(part.encode("utf-8")) for part in fixed_parts),
    )
    document = (_DOCX_DOCUMENT_OPEN + "".join(_paragraph_xml(block) for block in blocks)
                + _DOCX_DOCUMENT_CLOSE)
    core = core_template.format(
        title=_xml_text(title), creator=_xml_text(PRODUCT_NAME), created_at=_xml_text(created_at),
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
    return output.getvalue()


def _artifact(
    blocks: Sequence[ExportBlock], *, stem: str, format_name: str, created_at: str
) -> ExportArtifact:
    if format_name == "markdown":
        return ExportArtifact(blocks_to_markdown(blocks), MARKDOWN_MEDIA_TYPE, stem + ".md")
    if format_name == "docx":
        return ExportArtifact(
            blocks_to_docx(blocks, title=blocks[0].text, created_at=created_at),
            DOCX_MEDIA_TYPE,
            stem + ".docx",
        )
    raise ExportProblem("Choose Word or Markdown for this export.")


def export_answer(
    matter: MatterRecord,
    conversation: ConversationRecord,
    answer: MessageRecord,
    question: MessageRecord | None,
    format_name: str,
    *,
    exported_at: str | None = None,
    presentation_notice: str = PDF_SAVED_RESULT_NOTICE,
) -> ExportArtifact:
    created = exported_at or _now()
    blocks = answer_blocks(matter, conversation, answer, question, exported_at=created,
                           presentation_notice=presentation_notice)
    stem = safe_file_stem(f"{matter.display_name}-{conversation.title}-answer")
    return _artifact(blocks, stem=stem, format_name=format_name, created_at=created)


def export_conversation(
    matter: MatterRecord,
    conversation: ConversationRecord,
    messages: Sequence[MessageRecord],
    format_name: str,
    *,
    exported_at: str | None = None,
    presentation_notice: str = PDF_SAVED_RESULT_NOTICE,
) -> ExportArtifact:
    created = exported_at or _now()
    blocks = conversation_blocks(matter, conversation, messages, exported_at=created,
                                 presentation_notice=presentation_notice)
    stem = safe_file_stem(f"{matter.display_name}-{conversation.title}")
    return _artifact(blocks, stem=stem, format_name=format_name, created_at=created)


def export_notebook(
    matter: MatterRecord,
    entries: Sequence[tuple[NotebookItemRecord, Sequence[NotebookReferenceRecord]]],
    format_name: str,
    *,
    exported_at: str | None = None,
    heading: str = "Matter notebook",
    stem: str | None = None,
) -> ExportArtifact:
    created = exported_at or _now()
    _validate_notebook_scope(matter, entries)
    filename_stem = safe_file_stem(stem or f"{matter.display_name}-notebook")
    if format_name == "csv":
        return ExportArtifact(notebook_csv(entries), CSV_MEDIA_TYPE, filename_stem + ".csv")
    blocks = notebook_blocks(matter, entries, exported_at=created, heading=heading)
    return _artifact(blocks, stem=filename_stem, format_name=format_name, created_at=created)


def export_report(
    matter: MatterRecord,
    report: ReportRecord,
    sections: Sequence[
        tuple[ReportSectionRecord, Sequence[ReportCitationRecord]]
    ],
    format_name: str,
    *,
    exported_at: str | None = None,
    presentation_notice: str = PDF_SAVED_RESULT_NOTICE,
) -> ExportArtifact:
    created = exported_at or _now()
    _validate_report_export_scope(matter, report, sections)
    blocks: list[ExportBlock] = [
        ExportBlock(report.title, "title"),
        ExportBlock(matter.display_name, "subtitle"),
        ExportBlock(
            f"{report.status.title()} work product exported from {PRODUCT_NAME} at {created}.",
            "metadata",
        ),
    ]
    if report.purpose:
        blocks.extend(
            (ExportBlock("Purpose", "heading1"), ExportBlock(report.purpose))
        )
    if presentation_notice:
        blocks.append(ExportBlock(presentation_notice, "note"))
    for index, (section, citations) in enumerate(sections, 1):
        blocks.append(ExportBlock(f"{index}. {section.heading}", "heading1"))
        if section.body:
            blocks.append(ExportBlock(section.body))
        for citation_index, citation in enumerate(citations, 1):
            blocks.append(
                ExportBlock(
                    f"Source {citation_index}: {citation.source_name} — {citation.location}",
                    "citation",
                )
            )
            if citation.excerpt:
                blocks.append(ExportBlock(citation.excerpt, "citation"))
            if citation.kind == "media_clip":
                blocks.append(
                    ExportBlock(
                        "Timestamped recording segment; export or play the saved clip in the matter to verify context.",
                        "note",
                    )
                )
    if not sections:
        blocks.append(ExportBlock("This report does not contain sections yet.", "note"))
    blocks.append(
        ExportBlock(
            "This is review work product. Verify every statement against the cited source material before relying on it.",
            "footer",
        )
    )
    stem = safe_file_stem(f"{matter.display_name}-{report.title}")
    return _artifact(tuple(blocks), stem=stem, format_name=format_name, created_at=created)


def export_research(
    matter: MatterRecord,
    job: ResearchJobRecord,
    format_name: str,
    *,
    exported_at: str | None = None,
    presentation_notice: str = PDF_SAVED_RESULT_NOTICE,
) -> ExportArtifact:
    """Export one durable research run with its evidence and coverage ledger."""

    created = exported_at or _now()
    stem = safe_file_stem(f"{matter.display_name}-{job.title}-investigation")
    result = dict(job.result)
    _validate_research_export_scope(matter, job, result)
    if format_name == "json":
        def mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
            if not isinstance(value, list):
                return ()
            return tuple(item for item in value if isinstance(item, Mapping))

        def safe_citation(value: object) -> dict[str, object] | None:
            if not isinstance(value, Mapping):
                return None
            source_name = _plain(value.get("source_name"))
            location = _plain(value.get("location"))
            if not source_name or not location:
                return None
            kind = _plain(value.get("evidence_kind")).casefold()
            citation: dict[str, object] = {
                "source": source_name,
                "location": location,
                "source_type": "Spoken" if kind == "transcript" else "Written",
            }
            excerpt = _source_excerpt(value.get("excerpt"))
            if excerpt:
                citation["excerpt"] = excerpt
            if "hierarchical_synthesis" in result:
                original = next((item for item in result["evidence"]
                                 if item["support_token"] == value.get("support_token")), None)
                if original is not None:
                    # Portable identity joins claims to the exported source ledger,
                    # without exporting an application action token or route.
                    identity = json.dumps({key: original[key] for key in (
                        "document_id", "source_version_id", "chunk_id", "unit_number",
                        "line_start", "line_end", "excerpt_digest", "evidence_kind")}, sort_keys=True)
                    citation["citation_id"] = "citation-" + hashlib.sha256(identity.encode()).hexdigest()
                    citation["version"] = original["source_version_id"]
            return citation

        safe_answer: dict[str, object] = {}
        answer = result.get("answer")
        if isinstance(answer, Mapping):
            claims: list[dict[str, object]] = []
            for claim in mapping_items(answer.get("claims")):
                sources = tuple(
                    safe
                    for citation in mapping_items(claim.get("citations"))
                    if (safe := safe_citation(citation)) is not None
                )
                claims.append(
                    {
                        "text": _plain(claim.get("text")),
                        "sources": sources,
                    }
                )
            safe_answer = {
                "outcome": "Supported" if answer.get("answerable") else "Not supported",
                "introduction": answer_introduction(_plain(answer.get("introduction"))),
                "claims": claims,
                "missing_information": _plain(rejected_answer_notice(answer.get("missing_information"))) if answer.get("answerable") is False else _plain(answer.get("missing_information")),
                "evidence_notice": _plain(answer.get("evidence_notice")),
            }
            limitation = answer.get("limitation")
            if isinstance(limitation, Mapping):
                safe_answer["limitation"] = {
                    "text": _plain(answer_limitation(answer)),
                    "sources": tuple(
                        safe
                        for citation in mapping_items(limitation.get("citations"))
                        if (safe := safe_citation(citation)) is not None
                    ),
                }
            modality = answer.get("modality_coverage")
            if isinstance(modality, Mapping):
                safe_answer["requested_source_coverage"] = {
                    "result": "Complete"
                    if modality.get("mode") == "complete"
                    else "Partial",
                    "notice": _plain(modality_coverage_notice(modality.get("notice"))),
                }

        coverage = result.get("coverage")
        safe_coverage: dict[str, object] = {}
        if isinstance(coverage, Mapping):
            safe_coverage = {
                "focused_searches": int(coverage.get("search_pass_count") or 0),
                "passages_considered": int(
                    coverage.get("candidate_passage_count") or 0
                ),
                "supporting_passages": int(
                    coverage.get("evidence_passage_count") or 0
                ),
                "supporting_sources": int(
                    coverage.get("evidence_source_count") or 0
                ),
                "scope": "One frozen full-text review" if coverage.get("scope") == "full_text_run" else "Selected source set"
                if coverage.get("scope") == "source_set"
                else "All searchable sources",
                "notice": _plain(coverage.get("notice")),
            }
        findings = []
        def motivating_source(token):
            return next((safe_citation(source) for source in mapping_items(result.get("evidence"))
                         if token and source.get("support_token") == token), None)

        for item in mapping_items(result.get("passes")):
            findings.append(
                {
                    "search": _plain(item.get("query")),
                    "reason": _plain(item.get("reason")),
                    "retrieval_outcome": _plain(item.get("retrieval_outcome")),
                    "hit_count": item.get("hit_count"),
                    "selected_passages": item.get("selected_passages"),
                    "motivating_source": motivating_source(item.get("motivating_support_token")),
                    "status": _plain(item.get("status")).replace("_", " ").title(),
                    "finding": _plain(research_content(item.get("text"), item.get("answer"))),
                }
            )
        supporting_sources = tuple(
            safe
            for citation in mapping_items(result.get("evidence"))
            if (safe := safe_citation(citation)) is not None
        )
        hierarchy = result.get("hierarchical_synthesis")
        safe_hierarchy = None
        if isinstance(hierarchy, Mapping):
            sources_by_token = {item["support_token"]: item for item in result["evidence"]}
            safe_hierarchy = {key: hierarchy[key] for key in (
                "version", "policy", "requests_spent", "stop_reason", "partial",
                "omitted_groups", "rejected_findings", "truncated_characters")}
            safe_hierarchy["uncited_sources"] = [safe_citation(sources_by_token[token])
                                                   for token in hierarchy["uncited_evidence"]]
            for level in ("issue", "matter"):
                safe_hierarchy[level] = [{
                    "id": node["id"], "parents": node["parents"], "state": node["state"],
                    "omitted_claims": node["omitted_claims"],
                    "claims": [{"text": claim["text"], "parents": claim["parents"],
                                "sources": [safe_citation(sources_by_token[token]) for token in claim["support_tokens"]]}
                               for claim in node["claims"]],
                } for node in hierarchy[level]]
        body = json.dumps(
            {
                "product": "RecordBench",
                "exported_at": created,
                "matter": {"name": matter.display_name},
                **({"presentation_notice": presentation_notice} if presentation_notice else {}),
                "investigation": {
                    "review_notice": GENERATED_REVIEW_NOTICE,
                    "title": job.title,
                    "question": job.question,
                    "status": job.state.replace("_", " ").title(),
                    "created_at": job.created_at,
                    "finished_at": job.finished_at,
                    "synthesis": _plain(research_content(result.get("summary"), result.get("answer"))),
                    "answer": safe_answer,
                    "coverage": safe_coverage,
                    "review_budget": job.review_budget,
                    "review_budget_description": job.review_budget_description,
                    **({"hierarchical_synthesis": safe_hierarchy} if safe_hierarchy is not None else {}),
                    **({"full_text_synthesis_input": result["full_text_synthesis_input"]}
                       if "full_text_synthesis_input" in result else {}),
                    "findings": findings,
                    "unsearched_proposals": [
                        {"search": _plain(item.get("query")), "reason": _plain(item.get("reason")),
                         "motivating_source": motivating_source(item.get("support_token"))}
                        for item in mapping_items(result.get("pending_searches"))
                    ],
                    "supporting_sources": supporting_sources,
                },
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        if len(body) > MAX_EXPORT_TEXT_CHARS:
            raise ExportProblem("This research run is too large to export as one JSON file.")
        return ExportArtifact(body, JSON_MEDIA_TYPE, stem + ".json")
    blocks: list[ExportBlock] = [
        ExportBlock(job.title, "title"),
        ExportBlock(matter.display_name, "subtitle"),
        ExportBlock(f"Investigation exported from {PRODUCT_NAME} at {created}.", "metadata"),
        ExportBlock("Research question", "heading1"),
        ExportBlock(job.question),
    ]
    blocks.append(ExportBlock(job.review_budget_description, "note"))
    if presentation_notice:
        blocks.append(ExportBlock(presentation_notice, "note"))
    if "full_text_synthesis_input" in result:
        from .full_text_synthesis import input_notice
        receipt = result["full_text_synthesis_input"]
        blocks.append(ExportBlock("Frozen full-text review input", "heading1"))
        blocks.append(ExportBlock(input_notice(receipt), "note"))
        blocks.append(ExportBlock(f"Review {receipt['run_id']} · criterion version {receipt['criterion_version_id']}", "metadata"))
        blocks.append(ExportBlock(f"Input version {receipt['version']} · snapshot {receipt['snapshot_digest']} · human decision revision {receipt['decision_revision_digest']}", "metadata"))
        blocks.append(ExportBlock(receipt["human_decisions"]["notice"], "note"))
        blocks.append(ExportBlock("Complete input receipt", "heading2"))
        blocks.append(ExportBlock(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2), "metadata"))
    summary = _plain(research_content(result.get("summary"), result.get("answer")))
    if summary:
        blocks.extend((
            ExportBlock("Generated synthesis · needs review", "heading1"),
            ExportBlock(GENERATED_REVIEW_NOTICE, "note"),
            ExportBlock(summary),
        ))
    evidence_notice = _plain((result.get("answer") or {}).get("evidence_notice"))
    if evidence_notice:
        blocks.append(ExportBlock(evidence_notice, "note"))
    modality = (result.get("answer") or {}).get("modality_coverage")
    if isinstance(modality, Mapping):
        notice = _plain(modality_coverage_notice(modality.get("notice")))
        if notice:
            blocks.append(ExportBlock(f"Requested source coverage: {notice}", "note"))
    hierarchy = result.get("hierarchical_synthesis")
    if isinstance(hierarchy, Mapping):
        from .hierarchical_synthesis import synthesis_notice
        blocks.append(ExportBlock("Synthesis checkpoints", "heading1"))
        blocks.append(ExportBlock(synthesis_notice(hierarchy), "note"))
        for level in ("issue", "matter"):
            for node in hierarchy[level]:
                blocks.append(ExportBlock(f"{level.title()} section {node['id']} — {node['state']}", "heading2"))
                for claim in node["claims"]:
                    blocks.append(ExportBlock(claim["text"]))
                    blocks.append(ExportBlock("Derived through: " + ", ".join(claim["parents"]), "metadata"))
                    for token in claim["support_tokens"]:
                        source = next(item for item in result["evidence"] if item["support_token"] == token)
                        blocks.append(ExportBlock(f"{source['source_name']} — {source['location']} (version {source['source_version_id']})", "citation"))
    coverage = result.get("coverage")
    if isinstance(coverage, Mapping):
        blocks.append(ExportBlock("Coverage", "heading1"))
        for label, key in (
            ("Search passes", "search_pass_count"),
            ("Passages considered", "candidate_passage_count"),
            ("Supporting passages", "evidence_passage_count"),
            ("Distinct evidence sources", "evidence_source_count"),
        ):
            blocks.append(ExportBlock(f"{label}: {coverage.get(key, 0)}", "bullet"))
        notice = _plain(coverage.get("notice"))
        if notice:
            blocks.append(ExportBlock(notice, "note"))
    passes = result.get("passes")
    if isinstance(passes, list):
        blocks.append(ExportBlock("Research findings", "heading1"))
        for index, item in enumerate(passes, 1):
            if not isinstance(item, Mapping):
                continue
            blocks.append(ExportBlock(f"{index}. {_plain(item.get('query'))}", "heading1"))
            if item.get("reason"):
                blocks.append(ExportBlock(_plain(item["reason"]), "note"))
            token = item.get("motivating_support_token")
            for source in result.get("evidence", []):
                if token and source.get("support_token") == token:
                    blocks.append(ExportBlock(f"Search motivated by: {_plain(source.get('source_name'))} — {_plain(source.get('location'))}", "citation"))
            blocks.append(ExportBlock(_plain(research_content(item.get("text"), item.get("answer"))) or "No supported finding.", "normal"))
    pending = result.get("pending_searches")
    if isinstance(pending, list) and pending:
        blocks.append(ExportBlock("Unsearched proposals", "heading1"))
        for item in pending:
            if not isinstance(item, Mapping):
                continue
            blocks.append(ExportBlock(_plain(item.get("query")), "heading2"))
            blocks.append(ExportBlock(_plain(item.get("reason")), "note"))
            for source in result.get("evidence", []):
                if source.get("support_token") == item.get("support_token"):
                    blocks.append(ExportBlock(f"Search motivated by: {_plain(source.get('source_name'))} — {_plain(source.get('location'))}", "citation"))
    evidence = result.get("evidence")
    if isinstance(evidence, list):
        blocks.append(ExportBlock("Supporting sources", "heading1"))
        for index, item in enumerate(evidence, 1):
            if not isinstance(item, Mapping):
                continue
            source = _plain(item.get("source_name"))
            location = _plain(item.get("location"))
            blocks.append(ExportBlock(f"{index}. {source} — {location}", "citation"))
            excerpt = _plain(item.get("excerpt"))
            if excerpt:
                blocks.append(ExportBlock(excerpt, "citation"))
    blocks.append(
        ExportBlock(
            "This investigation is limited to selected search passages; it did not check every source. Verify cited passages before relying on it.",
            "footer",
        )
    )
    return _artifact(tuple(blocks), stem=stem, format_name=format_name, created_at=created)


def export_full_review(
    matter: MatterRecord,
    criterion: ReviewCriterionRecord,
    version: ReviewCriterionVersionRecord,
    run: ReviewRunRecord,
    decisions: Sequence[ReviewDecisionRecord],
    metrics: Mapping[str, object],
    format_name: str,
    *,
    exported_at: str | None = None,
    frozen_text_sources: Sequence[SourceCatalogRecord] | None = None,
) -> ExportArtifact:
    """Export a frozen every-source check without portable internal identifiers."""

    created = exported_at or _now()
    _validate_review_export_scope(matter, criterion, version, run, decisions,
        frozen_text_sources=frozen_text_sources)
    stem = safe_file_stem(f"{matter.display_name}-{criterion.title}-source-check")
    if format_name == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(
            (
                "Ordinal", "Source", "Type", "RecordBench label", "Rationale",
                "Cited support", "Needs attention", "Validation sample",
                "Staff decision", "Staff note", "Reviewed at",
            )
        )
        for item in decisions:
            citations = _portable_review_citations(item.citations)
            writer.writerow(
                (
                    item.ordinal,
                    _csv_safe(item.source_name),
                    _csv_safe(item.source_kind),
                    _staff_label(item.machine_decision),
                    _csv_safe(review_rejection_notice(item.rationale)),
                    _csv_safe(
                        "; ".join(_review_citation_text(citation) for citation in citations)
                    ),
                    _csv_safe(review_rejection_notice(item.error_message)),
                    "yes" if item.validation_sample else "no",
                    _staff_label(item.human_decision),
                    _csv_safe(item.human_note),
                    item.reviewed_at or "",
                )
            )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        if len(body) > MAX_WORKFLOW_EXPORT_BYTES:
            raise ExportProblem("This review is too large to export as one CSV file.")
        return ExportArtifact(body, CSV_MEDIA_TYPE, stem + ".csv")
    if format_name == "json":
        body = json.dumps(
            {
                "schema": "recordbench-source-check-v1",
                "product": "RecordBench",
                "exported_at": created,
                "matter": {"name": matter.display_name},
                "criterion": {
                    "title": criterion.title,
                    "version": version.version_number,
                    "instructions": version.instructions,
                    "include_guidance": version.include_guidance,
                    "exclude_guidance": version.exclude_guidance,
                },
                "check": {
                    "scope": "Every source"
                    if run.run_kind == "full"
                    else "Representative sample",
                    "status": _staff_label(run.state),
                    "snapshot_count": run.snapshot_count,
                    "reviewed_count": run.reviewed_count,
                    "included_count": run.included_count,
                    "excluded_count": run.excluded_count,
                    "attention_count": run.attention_count,
                    "created_at": run.created_at,
                    "finished_at": run.finished_at,
                },
                "validation_metrics": {
                    key: metrics.get(key) for key in _PORTABLE_REVIEW_METRICS
                },
                "decisions": [
                    {
                        "ordinal": item.ordinal,
                        "source_name": item.source_name,
                        "source_kind": item.source_kind,
                        "recordbench_label": _staff_label(item.machine_decision),
                        "rationale": review_rejection_notice(item.rationale),
                        "citations": _portable_review_citations(item.citations),
                        "attention_note": review_rejection_notice(item.error_message),
                        "validation_sample": bool(item.validation_sample),
                        "staff_decision": _staff_label(item.human_decision),
                        "staff_note": item.human_note,
                        "reviewed_at": item.reviewed_at,
                    }
                    for item in decisions
                ],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        if len(body) > MAX_WORKFLOW_EXPORT_BYTES:
            raise ExportProblem("This review is too large to export as one JSON file.")
        return ExportArtifact(body, JSON_MEDIA_TYPE, stem + ".json")
    blocks: list[ExportBlock] = [
        ExportBlock(criterion.title, "title"),
        ExportBlock(matter.display_name, "subtitle"),
        ExportBlock(
            f"Every-source check v{version.version_number} exported from {PRODUCT_NAME} at {created}.",
            "metadata",
        ),
        ExportBlock("Saved criterion", "heading1"),
        ExportBlock(version.instructions),
        ExportBlock("Check totals", "heading1"),
        ExportBlock(f"Frozen searchable population: {run.snapshot_count:,}", "bullet"),
        ExportBlock(f"Included: {run.included_count:,}", "bullet"),
        ExportBlock(f"Not identified: {run.excluded_count:,}", "bullet"),
        ExportBlock(f"Needs attention: {run.attention_count:,}", "bullet"),
        ExportBlock("Human validation", "heading1"),
        ExportBlock(
            f"{metrics.get('reviewed_total', 0)} of {metrics.get('sample_total', 0)} sampled decisions reviewed.",
            "normal",
        ),
    ]
    for label, key in (
        ("Precision", "precision"), ("Recall", "recall"),
        ("Elusion", "elusion"), ("Richness", "richness"), ("Error rate", "error_rate"),
    ):
        value = metrics.get(key)
        blocks.append(
            ExportBlock(f"{label}: {'not yet defined' if value is None else f'{float(value):.1%}'}", "bullet")
        )
    blocks.append(ExportBlock("Source decisions", "heading1"))
    readable_decisions = decisions[:MAX_READABLE_REVIEW_DECISIONS]
    if len(decisions) > len(readable_decisions):
        blocks.append(
            ExportBlock(
                f"This readable summary includes the first {len(readable_decisions):,} of "
                f"{len(decisions):,} source decisions. Use the complete CSV or JSON export "
                "for the full every-source ledger.",
                "note",
            )
        )
    for item in readable_decisions:
        blocks.append(
            ExportBlock(
                f"{item.ordinal}. {item.source_name} — {item.machine_decision.replace('_', ' ').title()}",
                "heading1",
            )
        )
        if item.rationale:
            blocks.append(ExportBlock(review_rejection_notice(item.rationale)))
        if item.human_decision:
            blocks.append(
                ExportBlock(
                    f"Human validation: {item.human_decision.replace('_', ' ').title()}"
                    + (f" — {item.human_note}" if item.human_note else ""),
                    "note",
                )
            )
        for citation in item.citations:
            portable_citation = _portable_review_citation(citation)
            if portable_citation is None:
                continue
            blocks.append(
                ExportBlock(
                    f"Source: {_review_citation_text(portable_citation)}",
                    "citation",
                )
            )
    blocks.append(
        ExportBlock(
            "Exculpata labels do not replace attorney or reviewer judgment. Preserve the criterion version, validate a representative sample, and verify source support.",
            "footer",
        )
    )
    return _artifact(tuple(blocks), stem=stem, format_name=format_name, created_at=created)


def _portable_message(message: MessageRecord) -> dict[str, object]:
    result: dict[str, object] = {
        "role": message.role,
        "created_at": message.created_at,
        "content": message.content,
    }
    if message.role == "assistant":
        payload = message.payload
        if payload.get("kind") == "generated":
            result["content"] = answer_content(message.content, _plain(payload.get("introduction")), payload)
        if payload.get("kind") == "not-supported":
            result["content"] = rejected_answer_content(message.content, payload)
        source_coverage = payload.get("source_coverage")
        portable_coverage: dict[str, object] = {}
        if isinstance(source_coverage, Mapping):
            portable_coverage = {
                "mode": "partial"
                if source_coverage.get("mode") == "partial"
                else "complete",
                "searchable_count": int(source_coverage.get("searchable_count") or 0),
                "total_count": int(source_coverage.get("total_count") or 0),
                "excluded_count": int(source_coverage.get("excluded_count") or 0),
                "notice": _plain(source_coverage.get("notice")),
            }
        review_scope = payload.get("review_scope")
        portable_scope: dict[str, object] = {}
        if isinstance(review_scope, Mapping):
            scope_mode = _plain(review_scope.get("mode"))
            if scope_mode not in {
                "focused",
                "focused_orientation",
                "broader_orientation",
            }:
                scope_mode = "focused"
            portable_scope = {
                "mode": scope_mode,
                "collection_wide_request": bool(
                    review_scope.get("collection_wide_request")
                ),
                "searchable_source_count": int(
                    review_scope.get("searchable_source_count") or 0
                ),
                "candidate_passage_count": int(
                    review_scope.get("candidate_passage_count") or 0
                ),
                "candidate_source_count": int(
                    review_scope.get("candidate_source_count") or 0
                ),
                "cited_passage_count": int(
                    review_scope.get("cited_passage_count") or 0
                ),
                "cited_source_count": int(
                    review_scope.get("cited_source_count") or 0
                ),
                "notice": _plain(review_scope.get("notice")),
            }
            if review_scope.get("broad_summary_request"):
                portable_scope["broad_summary_request"] = True
        modality_coverage = payload.get("modality_coverage")
        portable_modality: dict[str, object] = {}
        if isinstance(modality_coverage, Mapping):
            portable_modality = {
                "result": "complete"
                if modality_coverage.get("mode") == "complete"
                else "partial",
                "requested": [
                    _plain(item)
                    for item in modality_coverage.get(
                        "requested_evidence_kinds", []
                    )
                    if _plain(item)
                ]
                if isinstance(
                    modality_coverage.get("requested_evidence_kinds"), list
                )
                else [],
                "used": [
                    _plain(item)
                    for item in modality_coverage.get("used_evidence_kinds", [])
                    if _plain(item)
                ]
                if isinstance(modality_coverage.get("used_evidence_kinds"), list)
                else [],
                "missing": [
                    _plain(item)
                    for item in modality_coverage.get("missing_evidence_kinds", [])
                    if _plain(item)
                ]
                if isinstance(
                    modality_coverage.get("missing_evidence_kinds"), list
                )
                else [],
                "notice": _plain(modality_coverage_notice(modality_coverage.get("notice"))),
            }
        result["answer"] = {
            "review_notice": GENERATED_REVIEW_NOTICE if payload.get("kind") == "generated" else "",
            "kind": payload.get("kind") if payload.get("kind") in {"generated", "not-supported", "error"} else "saved",
            "introduction": answer_introduction(_plain(payload.get("introduction"))),
            "claims": [
                {
                    "text": _plain(claim.get("text")),
                    "citations": list(_citations(claim.get("citations"))),
                }
                for claim in payload.get("claims", [])
                if isinstance(claim, Mapping)
            ]
            if isinstance(payload.get("claims"), list)
            else [],
            "limitation": _plain(answer_limitation(payload)) if payload.get("kind") == "generated" else _plain(payload.get("limitation", {}).get("text"))
            if isinstance(payload.get("limitation"), Mapping)
            else "",
            "missing_information": _plain(rejected_answer_notice(payload.get("missing_information"))) if payload.get("kind") == "not-supported" else _plain(payload.get("missing_information")),
            "evidence_notice": _plain(payload.get("evidence_notice")),
            "source_matches": list(_citations(payload.get("source_matches"))),
            "source_coverage": portable_coverage,
            "review_scope": portable_scope,
            "requested_source_coverage": portable_modality,
            "context_supplied": payload.get("context_supplied"),
        }
    return result


def export_matter_bundle(
    matter: MatterRecord,
    conversations: Sequence[tuple[ConversationRecord, Sequence[MessageRecord]]],
    sources: Sequence[Mapping[str, object]],
    notebook: Sequence[
        tuple[NotebookItemRecord, Sequence[NotebookReferenceRecord]]
    ] = (),
    media_work_product: Sequence[Mapping[str, object]] = (),
    additional_work_product: Sequence[Mapping[str, object]] = (),
    *,
    saved_reports: Sequence[Mapping[str, object]] = (),
    exported_at: str | None = None,
) -> ExportArtifact:
    created = exported_at or _now()
    presentation_notice = PDF_SAVED_RESULT_NOTICE
    report = matter_report_blocks(
        matter,
        conversations,
        sources,
        len(notebook),
        len(media_work_product),
        exported_at=created,
    )
    report_markdown = blocks_to_markdown(report)
    report_docx = blocks_to_docx(report, title=matter.display_name, created_at=created)
    conversation_files: list[dict[str, object]] = []
    portable_conversations: list[dict[str, object]] = []
    output = io.BytesIO()
    uncompressed_bytes = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        def write_member(path: str, body: bytes) -> None:
            nonlocal uncompressed_bytes
            projected = uncompressed_bytes + len(body)
            if projected > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                raise ExportProblem(
                    "This bundle is too large to prepare at once. Export the conversations individually."
                )
            archive.writestr(path, body)
            uncompressed_bytes = projected

        write_member("matter-report.md", report_markdown)
        write_member("matter-report.docx", report_docx)
        write_member(
            "README.txt",
            (
                f"{PRODUCT_NAME} work-product bundle\n\n"
                "This package contains saved conversations, generated work product, "
                "and source inventory references. It does not contain original source files.\n"
                "Matter notebook exports include staff-authored and review-state work product.\n"
                "Saved draft and final Reports are included as editable Markdown and Word "
                "documents under reports/ and listed in manifest.json.\n"
                "Transcript exports, available transcript overviews, and a clip inventory are "
                "included when media was reviewed; recording and clip bytes are not included.\n"
                "Transcript overviews are fallible orientation and are not findings about what occurred.\n"
                "Completed investigation and every-source ledgers are included under "
                "investigations/ and source-checks/.\n"
                "Verify generated statements against the cited records before relying on them.\n"
            ).encode("utf-8"),
        )
        used_names: set[str] = set()
        for index, (conversation, messages) in enumerate(conversations, 1):
            base = f"{index:03d}-{safe_file_stem(conversation.title, 'conversation')}"
            candidate = base
            suffix = 2
            while candidate.casefold() in used_names:
                candidate = f"{base}-{suffix}"
                suffix += 1
            used_names.add(candidate.casefold())
            blocks = conversation_blocks(
                matter, conversation, messages, exported_at=created,
                presentation_notice=presentation_notice,
            )
            markdown_path = f"conversations/{candidate}.md"
            docx_path = f"conversations/{candidate}.docx"
            write_member(markdown_path, blocks_to_markdown(blocks))
            write_member(
                docx_path,
                blocks_to_docx(blocks, title=conversation.title, created_at=created),
            )
            conversation_files.append(
                {
                    "title": conversation.title,
                    "message_count": len(messages),
                    "updated_at": conversation.updated_at,
                    "markdown": markdown_path,
                    "docx": docx_path,
                }
            )
            portable_conversations.append(
                {
                    "title": conversation.title,
                    "created_at": conversation.created_at,
                    "updated_at": conversation.updated_at,
                    "messages": [_portable_message(message) for message in messages],
                    **({"presentation_notice": presentation_notice} if presentation_notice else {}),
                }
            )
        notebook_report = notebook_blocks(
            matter, notebook, exported_at=created
        )
        write_member("notebook/matter-notebook.md", blocks_to_markdown(notebook_report))
        write_member(
            "notebook/matter-notebook.docx",
            blocks_to_docx(notebook_report, title="Matter notebook", created_at=created),
        )
        write_member("notebook/matter-notebook.csv", notebook_csv(notebook))
        portable_notebook = [
            {
                "type": _staff_label(item.item_type),
                "status": _staff_label(item.status),
                "title": item.title,
                "details": item.body,
                "date": item.date_label,
                "pinned": bool(item.is_pinned),
                "origin": _staff_label(item.origin),
                "created_by": _staff_actor_label(item.created_by_name),
                "created_at": item.created_at,
                "updated_by": _staff_actor_label(item.updated_by_name),
                "updated_at": item.updated_at,
                "sources": [
                    {
                        "name": reference.source_name,
                        "location": reference.location,
                        "excerpt": _source_excerpt(reference.excerpt),
                    }
                    for reference in references
                ],
            }
            for item, references in notebook
        ]
        write_member(
            "notebook/notebook.json",
            json.dumps(
                {"schema": "recordbench-notebook-v1", "items": portable_notebook},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )
        media_manifest: list[dict[str, object]] = []
        clip_inventory: list[dict[str, object]] = []
        summary_count = 0
        used_media_names: set[str] = set()
        for index, item in enumerate(media_work_product, 1):
            if item.get("matter_id") != matter.matter_id:
                raise ExportProblem("Transcript export crossed a matter boundary.")
            source_name = _plain(item.get("source_name")) or f"Recording {index}"
            source_kind = _plain(item.get("source_kind")) or "Transcript"
            base = f"{index:03d}-{safe_file_stem(source_name, 'recording')}"
            candidate = base
            suffix = 2
            while candidate.casefold() in used_media_names:
                candidate = f"{base}-{suffix}"
                suffix += 1
            used_media_names.add(candidate.casefold())
            paths: dict[str, str] = {}
            for format_name, extension in (("markdown", ".md"), ("srt", ".srt"), ("json", ".json")):
                body = item.get(format_name)
                if not isinstance(body, bytes):
                    raise ExportProblem("A transcript export could not be prepared safely.")
                path = f"transcripts/{candidate}{extension}"
                write_member(path, body)
                paths[format_name] = path
            summary_path = ""
            summary_body = item.get("summary_markdown")
            if summary_body is not None:
                if not isinstance(summary_body, bytes):
                    raise ExportProblem("A transcript overview could not be prepared safely.")
                summary_path = f"transcript-overviews/{candidate}.md"
                write_member(summary_path, summary_body)
                summary_count += 1
            summary_coverage = item.get("summary_coverage")
            if summary_coverage is not None and not isinstance(summary_coverage, Mapping):
                raise ExportProblem("Transcript overview coverage is invalid.")
            clips = item.get("clips")
            if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)):
                raise ExportProblem("A media clip inventory could not be prepared safely.")
            for clip in clips:
                if not isinstance(clip, Mapping):
                    raise ExportProblem("A media clip inventory could not be prepared safely.")
                clip_inventory.append(
                    {
                        "source": source_name,
                        "title": _plain(clip.get("title")),
                        "start": _plain(clip.get("start")),
                        "end": _plain(clip.get("end")),
                        "created_at": _plain(clip.get("created_at")),
                    }
                )
            media_manifest.append(
                {
                    "source_name": source_name,
                    "source_kind": source_kind,
                    "review_status": _portable_media_review_status(
                        item.get("review_state")
                    ),
                    "segment_count": int(item.get("segment_count") or 0),
                    "exports": paths,
                    "overview": summary_path or None,
                    "overview_coverage": _portable_overview_coverage(
                        summary_coverage
                    ),
                    "clip_count": len(clips),
                }
            )
        clip_output = io.StringIO(newline="")
        clip_writer = csv.DictWriter(
            clip_output,
            fieldnames=("source", "title", "start", "end", "created_at"),
            lineterminator="\r\n",
        )
        clip_writer.writeheader()
        for clip in clip_inventory:
            clip_writer.writerow({key: _csv_safe(value) for key, value in clip.items()})
        write_member(
            "media/clip-inventory.csv",
            ("\ufeff" + clip_output.getvalue()).encode("utf-8"),
        )
        write_member(
            "media/media-work-product.json",
            json.dumps(
                {
                    "schema": "recordbench-media-work-product-v2",
                    "original_media_included": False,
                    "overview_count": summary_count,
                    "transcripts": media_manifest,
                    "clips": clip_inventory,
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )
        report_files: list[dict[str, object]] = []
        for index, item in enumerate(saved_reports, 1):
            if item.get("matter_id") != matter.matter_id:
                raise ExportProblem("Report export crossed a matter boundary.")
            title = _plain(item.get("title"))
            status = item.get("status")
            section_count = item.get("section_count")
            if (
                not title
                or status not in {"draft", "final"}
                or not isinstance(section_count, int)
                or section_count < 0
            ):
                raise ExportProblem("A saved Report could not be packaged safely.")
            base = f"reports/{index:03d}-{safe_file_stem(title, 'report')}"
            entry: dict[str, object] = {
                "title": title,
                "status": status,
                "section_count": section_count,
                "updated_at": _plain(item.get("updated_at")),
            }
            for format_name, extension in (("markdown", "md"), ("docx", "docx")):
                body = item.get(format_name)
                if not isinstance(body, bytes):
                    raise ExportProblem("A saved Report could not be packaged safely.")
                path = f"{base}.{extension}"
                write_member(path, body)
                entry[format_name] = path
            report_files.append(entry)
        additional_manifest: list[dict[str, object]] = []
        seen_additional_paths: set[str] = set()
        for item in additional_work_product:
            path = str(item.get("path") or "")
            body = item.get("body")
            kind = _plain(item.get("kind"))
            if (
                not re.fullmatch(
                    r"(?:investigations|source-checks|intake|entities|assertions|context)/"
                    r"[A-Za-z0-9][A-Za-z0-9._/-]{0,240}",
                    path,
                )
                or kind not in {"investigation", "source_check", "intake_receipt", "full_text_review", "entity", "entity_discovery", "assertion", "context_selection", "answer_context"}
                or ".." in path.split("/")
                or path.casefold() in seen_additional_paths
                or not isinstance(body, bytes)
            ):
                raise ExportProblem("Additional work product could not be packaged safely.")
            seen_additional_paths.add(path.casefold())
            write_member(path, body)
            additional_manifest.append(
                {"kind": kind, "path": path, "bytes": len(body)}
            )
        manifest = {
            "schema": "recordbench-work-product-bundle-v1",
            "exported_at": created,
            "matter": {
                "name": matter.display_name,
                "description": matter.descriptor,
            },
            "original_source_files_included": False,
            "source_count": len(sources),
            "conversation_count": len(conversations),
            "notebook_count": len(notebook),
            "transcript_count": len(media_work_product),
            "transcript_overview_count": summary_count,
            "clip_count": len(clip_inventory),
            "report_count": len(report_files),
            "reports": report_files,
            "additional_work_product_count": len(additional_manifest),
            "additional_work_product": additional_manifest,
            "conversations": conversation_files,
        }
        write_member(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        write_member(
            "conversations.json",
            json.dumps(
                {"schema": "recordbench-conversations-v1", "conversations": portable_conversations},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )
    if output.tell() > MAX_BUNDLE_UNCOMPRESSED_BYTES:
        raise ExportProblem(
            "This bundle is too large to prepare at once. Export the conversations individually."
        )
    return ExportArtifact(
        output.getvalue(),
        ZIP_MEDIA_TYPE,
        safe_file_stem(f"{matter.display_name}-work-product") + ".zip",
    )
