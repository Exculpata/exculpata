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
DISCLOSURE_NOTICE = "Candidate for attorney review (potential exculpatory or impeachment material); machine output does not establish a legal duty or fact."
DISCLOSURE_SUBTYPES: Mapping[str, str] = {
    "inconsistent_statement": "Inconsistent statement — candidate for review",
    "witness_benefit": "Witness benefit or agreement — candidate for review",
    "alternate_suspect": "Alternate suspect — candidate for review",
    "credibility_issue": "Credibility issue — candidate for review",
}
PROHIBITED_DISCLOSURE_PHRASES: tuple[str, ...] = (
    "is brady material",
    "is giglio material",
    "brady violation",
    "giglio violation",
    "disclosure was required",
    "disclosure violated",
    "must be disclosed",
    "prosecutor violated",
    "constitutional violation",
    "unlawfully withheld",
)
_DISCLOSURE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "inconsistent_statement",
        re.compile(r"\b(?:inconsistent|contradict(?:s|ed|ing|ion)?|changed\s+(?:his|her|their)\s+(?:story|account|statement)|prior\s+statement\s+(?:differs|conflicted))\b", re.IGNORECASE),
        "Passage references a potential statement inconsistency; candidate for attorney review.",
    ),
    (
        "witness_benefit",
        re.compile(r"\b(?:cooperation\s+agreement|plea\s+(?:deal|agreement)|sentencing\s+leniency|immunity\s+(?:agreement|grant)|reduced\s+sentence|in\s+exchange\s+for\s+testimony|benefit\s+for\s+testif(?:ying|y))\b", re.IGNORECASE),
        "Passage references a potential witness benefit or agreement; candidate for attorney review.",
    ),
    (
        "alternate_suspect",
        re.compile(r"\b(?:alternate\s+suspect|other\s+suspect|another\s+(?:person|individual)\s+(?:confessed|admitted|matched)|third[- ]party\s+(?:culpability|suspect))\b", re.IGNORECASE),
        "Passage references a potential alternate suspect; candidate for attorney review.",
    ),
    (
        "credibility_issue",
        re.compile(r"\b(?:prior\s+false\s+(?:report|statement)|credibility\s+(?:concern|issue)|bias\s+against\s+the\s+defendant|fabricated\s+(?:report|account)|disciplinary\s+finding\s+for\s+dishonesty)\b", re.IGNORECASE),
        "Passage references a potential witness credibility concern; candidate for attorney review.",
    ),
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def has_prohibited_disclosure_wording(text: str) -> bool:
    """Fail closed when text contains conclusive Brady/Giglio legal-duty or violation phrasing."""
    lowered = " ".join((text or "").casefold().split())
    return any(phrase in lowered for phrase in PROHIBITED_DISCLOSURE_PHRASES)


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
    subtype: str = ""


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


def _collect_calc_premise_refs(calc_ids: Sequence[str], receipts: Mapping[str, object]) -> list[SourceRef]:
    from .case_assistant import _norm_num

    refs: list[SourceRef] = []
    seen: set[str] = set()
    by_result: dict[str, list[str]] = {}
    for rid, rec in receipts.items():
        by_result.setdefault(_norm_num(rec.result), []).append(rid)
    queue = list(calc_ids)
    while queue:
        cid = queue.pop(0)
        if cid in seen:
            continue
        rec = receipts.get(cid)
        if rec is None:
            continue
        seen.add(cid)
        for inp in rec.inputs:
            refs.extend(inp.source_refs)
            for upstream_id in by_result.get(_norm_num(inp.value), ()):
                if upstream_id not in seen:
                    queue.append(upstream_id)
    return refs


def detect_disclosure_candidates(
    refs: Sequence[SourceRef],
    inspected_texts: Mapping[tuple[str, ...], str],
) -> tuple[AssistantProposal, ...]:
    """Deterministically surface cited disclosure-review candidates across neutral categories."""
    found: list[AssistantProposal] = []
    seen: set[str] = set()
    for ref in refs:
        if not (ref.document_id and ref.source_name and ref.location):
            continue
        text = inspected_texts.get((ref.document_id, ref.location, ref.source_version_id)) or inspected_texts.get((ref.document_id, ref.location), "")
        if not text.strip():
            continue
        for subtype, pattern, reason in _DISCLOSURE_PATTERNS:
            if pattern.search(text):
                label = DISCLOSURE_SUBTYPES[subtype]
                title = _short_title(f"{ref.source_name} ({ref.location})", label)
                pid = _proposal_id("disclosure_review", f"{subtype}:{title}", (ref,))
                if pid in seen:
                    continue
                seen.add(pid)
                found.append(AssistantProposal(
                    proposal_id=pid, category="disclosure_review", label=label,
                    title=title, detail=reason, date_label="", citations=(ref,),
                    calculation_ids=(), disclosure_note=DISCLOSURE_NOTICE, subtype=subtype,
                ))
    return tuple(found)


def build_verified_proposals(
    result: AssistantInvestigationResult,
    *,
    raw_proposals: Sequence[Mapping[str, object]] = (),
    inspected_texts: Mapping[tuple[str, ...], str] | None = None,
) -> tuple[AssistantProposal, ...]:
    """Build strictly cited, reviewer-gated proposals; drop any uncited, fabricated, or conclusive-wording entry."""
    inspected = {(ref.document_id, ref.location): ref for ref in result.citations}
    proposals: list[AssistantProposal] = []
    seen_ids: set[str] = set()

    def add(category: str, title: str, detail: str, refs: Sequence[SourceRef],
            *, date_label: str = "", calc_ids: Sequence[str] = (), subtype: str = "") -> None:
        if category not in PROPOSAL_CATEGORIES or not title.strip():
            return
        if category == "disclosure_review" and has_prohibited_disclosure_wording(f"{title} {detail}"):
            return
        valid_calcs = tuple(cid for cid in calc_ids if cid in result.receipts)
        combined_refs = (*refs, *_collect_calc_premise_refs(valid_calcs, result.receipts))
        if not combined_refs:
            return
        unique_refs = tuple(dict.fromkeys(combined_refs))[:12]
        norm_subtype = subtype if (category == "disclosure_review" and subtype in DISCLOSURE_SUBTYPES) else ""
        pid = _proposal_id(category, title.strip(), unique_refs)
        if pid in seen_ids:
            return
        seen_ids.add(pid)
        note = DISCLOSURE_NOTICE if category == "disclosure_review" else ""
        label = DISCLOSURE_SUBTYPES[norm_subtype] if norm_subtype else CATEGORY_LABELS[category]
        proposals.append(AssistantProposal(
            proposal_id=pid, category=category, label=label,
            title=title.strip()[:160], detail=detail.strip()[:4_000], date_label=date_label.strip()[:100],
            citations=unique_refs, calculation_ids=valid_calcs, disclosure_note=note, subtype=norm_subtype,
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
        subtype = str(raw.get("subtype", "")).strip()
        if category == "timeline_event" and not date_label:
            found = _ISO_DATE_RE.search(f"{title} {detail}")
            date_label = found.group() if found else ""
        add(category, title, detail, refs, date_label=date_label, calc_ids=[str(c) for c in raw_calcs], subtype=subtype)

    effective_texts = inspected_texts if inspected_texts is not None else result.inspected_texts
    if effective_texts:
        for cand in detect_disclosure_candidates(result.citations, effective_texts):
            add(cand.category, cand.title, cand.detail, cand.citations, subtype=cand.subtype)

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
