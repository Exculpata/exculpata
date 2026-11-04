"""Explicit resource contract and portable accounting for bounded investigations."""
from dataclasses import asdict, dataclass
from typing import ClassVar, Mapping, Sequence


@dataclass(frozen=True)
class ReviewBudget:
    passes: int = 5
    search_seconds: int = 900
    primary_candidates: int = 20
    supplemental_candidates_per_kind: int = 12
    selected_per_pass: int = 12
    unique_evidence: int = 72
    synthesis_inputs: int = 12
    evidence_item_chars: int = 6_000
    evidence_chars: int = 48_000
    output_tokens: int = 1_200
    reasoning_tokens: ClassVar[int] = 4_096

    def __post_init__(self):
        ceilings = (15, 2700, 20, 12, 12, 72, 12, 6_000, 48_000, 1_200)
        for (name, value), ceiling in zip(asdict(self).items(), ceilings):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"Review budget {name} must be between 1 and {ceiling}.")

    def bound_excerpts(self, excerpts: Sequence[str]) -> tuple[tuple[str, ...], int]:
        remaining = self.evidence_chars
        bounded = []
        omitted = 0
        for original in excerpts:
            excerpt = original[:min(self.evidence_item_chars, remaining)]
            omitted += len(original) - len(excerpt)
            remaining -= len(excerpt)
            bounded.append(excerpt)
        return tuple(bounded), omitted

    def metadata(self, **counts):
        return {"version": 2, "status": "known", "requested": asdict(self),
                "effective": asdict(self), "counts": counts, "stop_reason": "running"}


DEFAULT_REVIEW_BUDGET = ReviewBudget()


def validate_primary_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= DEFAULT_REVIEW_BUDGET.primary_candidates:
        raise ValueError("Primary retrieval limit must be between 1 and 20; requests above the effective budget are rejected.")
    return limit


def budget_metadata(result: Mapping, plan: Mapping | None = None) -> dict:
    """Return portable allowlisted counters, never arbitrary saved metadata."""
    value = result.get("budget")
    if not isinstance(value, Mapping):
        value = (plan or {}).get("budget")
    raw_reason = result.get("stop_reason", value.get("stop_reason") if isinstance(value, Mapping) else None)
    reason = raw_reason if raw_reason in {"running", "completed_bounded_plan", "cancelled", "cancelled_before_start", "failed", "pass_budget", "time_budget", "evidence_budget", "no_new_evidence", "queue_exhausted"} else "unknown"
    unknown = {"version": None, "status": "unknown", "requested": None,
               "effective": None, "counts": None, "stop_reason": reason}
    if not isinstance(value, Mapping) or value.get("version") not in {1, 2}:
        return unknown
    try:
        requested = asdict(ReviewBudget(**value["requested"]))
        effective = asdict(ReviewBudget(**value["effective"]))
        if value["version"] == 1:
            # Historical runs did not record or enforce a search-time budget.
            requested.pop("search_seconds", None)
            effective.pop("search_seconds", None)
        elif "search_seconds" not in value["requested"] or "search_seconds" not in value["effective"]:
            return unknown
        counts = value.get("counts", {})
        allowed = ("completed_passes", "discarded_passes", "candidate_occurrences", "unique_evidence", "synthesis_inputs",
                   "truncated_chars", "candidate_sources", "analyzed_unit_occurrences", "unavailable_sources")
        if not isinstance(counts, Mapping):
            return unknown
        safe_counts = {key: counts[key] for key in allowed if key in counts}
        if any(type(count) is not int or count < 0 for count in safe_counts.values()):
            return unknown
    except (KeyError, TypeError, ValueError):
        return unknown
    return {"version": value["version"], "status": "known", "requested": requested, "effective": effective,
            "counts": safe_counts, "stop_reason": reason}


def budget_description(value: Mapping) -> str:
    if value.get("status") != "known":
        return "Review budget unknown: no complete budget metadata was saved for this run. Stop reason: " + str(value.get("stop_reason", "unknown")) + "."
    limits = value["effective"]
    counts = value.get("counts") or {}
    timing = (f"{limits['search_seconds']} seconds for search steps (checked between calls); "
              if "search_seconds" in limits else "search-time budget not recorded; ")
    text = (f"Review limits: {limits['passes']} passes; {timing}{limits['primary_candidates']} primary candidates per pass; "
            f"up to {limits['supplemental_candidates_per_kind']} supplemental candidates per missing requested source kind; "
            f"{limits['selected_per_pass']} selected passages per pass; {limits['unique_evidence']} unique evidence passages; "
            f"{limits['synthesis_inputs']} synthesis inputs per generation; {limits['evidence_item_chars']} characters per passage; "
            f"{limits['evidence_chars']} evidence characters and {limits['output_tokens']} output tokens per generation. "
            + ("Requested limits equal effective limits. " if value["requested"] == limits else "Requested limits differ from effective limits. "))
    if counts:
        text += (f"Completed passes: {counts.get('completed_passes', 0)}; candidate occurrences: {counts.get('candidate_occurrences', 0)}; "
                 f"passes discarded after source changes: {counts.get('discarded_passes', 0)}; "
                 f"unique selected passages: {counts.get('unique_evidence', 0)}; synthesis inputs: {counts.get('synthesis_inputs', 0)}; "
                 f"candidate sources: {counts.get('candidate_sources', 0)}; analyzed unit occurrences: {counts.get('analyzed_unit_occurrences', 0)}; "
                 f"unavailable sources at start: {counts.get('unavailable_sources', 0)}; characters omitted from generation packets: {counts.get('truncated_chars', 0)}. ")
    return text + "Stop reason: " + str(value.get("stop_reason", "running")) + ". These counts do not measure pages read."
