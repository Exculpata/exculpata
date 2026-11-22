"""Cited, reviewer-gated case-memory proposals derived from Case Assistant findings."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping, Sequence

from .case_assistant import AssistantInvestigationResult
from .case_assistant_tools import SourceRef

PROPOSAL_CATEGORIES = frozenset({"case_theory", "contradiction", "timeline_event", "disclosure_review"})
CATEGORY_LABELS: Mapping[str, str] = {
    "case_theory": "Case theory candidate",
    "contradiction": "Contradiction candidate",
    "timeline_event": "Timeline event candidate",
    "disclosure_review": "Disclosure review candidate",
}
CATEGORY_STORAGE: Mapping[str, tuple[str, str]] = {
    "case_theory": ("fact", "suggested"),
    "contradiction": ("issue", "disputed"),
    "timeline_event": ("event", "suggested"),
    "disclosure_review": ("issue", "needs_review"),
}
DISCLOSURE_NOTICE = "Candidate for attorney review (Brady/Giglio/impeachment); machine output does not establish a legal duty or fact."
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


@dataclass(frozen=True)
class AssistantProposal:
    proposal_id: str
    category: str
    label: str
    title: str
    detail: str
    date_label: str
    citations: tuple[SourceRef, ...]
    calculation_ids: tuple[str, ...] = ()
    disclosure_note: str = ""


def _proposal_id(category: str, title: str, refs: Sequence[SourceRef]) -> str:
    digest = hashlib.sha256(
        f"{category}|{title}|{'|'.join(f'{r.document_id}:{r.location}' for r in refs)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"prop-{digest}"


def _short_title(text: str, prefix: str = "") -> str:
    clean = " ".join(text.split()).strip()
    if prefix:
        clean = f"{prefix}: {clean}"
    return clean[:150]


def _resolve_citations(raw_cites: object, inspected: Mapping[tuple[str, str], SourceRef]) -> tuple[SourceRef, ...]:
    if not isinstance(raw_cites, Sequence) or isinstance(raw_cites, (str, bytes)):
        return ()
    resolved: list[SourceRef] = []
    for item in raw_cites:
        if not isinstance(item, Mapping):
            return ()
        key = (str(item.get("document_id", "")).strip(), str(item.get("location", "")).strip())
        ref = inspected.get(key)
        if ref is None:
            return ()
        if ref not in resolved:
            resolved.append(ref)
    return tuple(resolved[:12])


def build_verified_proposals(
    result: AssistantInvestigationResult,
    *,
    raw_proposals: Sequence[Mapping[str, object]] = (),
) -> tuple[AssistantProposal, ...]:
    """Build strictly cited, reviewer-gated proposals; drop any uncited or fabricated entry."""
    inspected = {(ref.document_id, ref.location): ref for ref in result.citations}
    proposals: list[AssistantProposal] = []
    seen_ids: set[str] = set()

    def add(category: str, title: str, detail: str, refs: Sequence[SourceRef],
            *, date_label: str = "", calc_ids: Sequence[str] = ()) -> None:
        if category not in PROPOSAL_CATEGORIES or not refs or not title.strip():
            return
        unique_refs = tuple(dict.fromkeys(refs))[:12]
        valid_calcs = tuple(cid for cid in calc_ids if cid in result.receipts)
        pid = _proposal_id(category, title.strip(), unique_refs)
        if pid in seen_ids:
            return
        seen_ids.add(pid)
        note = DISCLOSURE_NOTICE if category == "disclosure_review" else ""
        proposals.append(AssistantProposal(
            proposal_id=pid, category=category, label=CATEGORY_LABELS[category],
            title=title.strip()[:160], detail=detail.strip()[:4_000], date_label=date_label.strip()[:100],
            citations=unique_refs, calculation_ids=valid_calcs, disclosure_note=note,
        ))

    for raw in raw_proposals:
        if not isinstance(raw, Mapping):
            continue
        category = str(raw.get("category", "")).strip()
        refs = _resolve_citations(raw.get("citations"), inspected)
        if category not in PROPOSAL_CATEGORIES or not refs:
            continue
        raw_calcs = raw.get("calculation_ids") if isinstance(raw.get("calculation_ids"), list) else ()
        if any(str(cid) not in result.receipts for cid in raw_calcs):
            continue
        title = str(raw.get("title", "")).strip()
        detail = str(raw.get("detail", "")).strip() or title
        date_label = str(raw.get("date_label", "")).strip()
        if category == "timeline_event" and not date_label:
            found = _ISO_DATE_RE.search(f"{title} {detail}")
            date_label = found.group() if found else ""
        add(category, title, detail, refs, date_label=date_label, calc_ids=[str(c) for c in raw_calcs])

    for finding in result.findings:
        if not finding.citations:
            continue
        calc_notes = [f"{cid}: {result.receipts[cid].expression}" for cid in finding.calculation_ids if cid in result.receipts]
        detail = finding.text + ("\nCalculations: " + "; ".join(calc_notes) if calc_notes else "")
        add("case_theory", _short_title(finding.text), detail, finding.citations, calc_ids=finding.calculation_ids)
        date_match = _ISO_DATE_RE.search(finding.text)
        if date_match:
            add("timeline_event", _short_title(finding.text, date_match.group()), detail,
                finding.citations, date_label=date_match.group(), calc_ids=finding.calculation_ids)
    return tuple(proposals)
