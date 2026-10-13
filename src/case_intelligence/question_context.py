"""Typed citation locators and bounded repair context, never factual evidence."""
from __future__ import annotations

from collections import Counter
import re


def typed_locator(item):
    """Recognize the supported location grammar, not arbitrary metadata digits."""
    if item.evidence_kind == "transcript":
        if re.fullmatch(r"\d{2,}:[0-5]\d:[0-5]\d", item.location):
            return {"kind": "transcript_position", "value": item.location}
    elif item.evidence_kind == "document":
        match = re.fullmatch(r"(Row|Page|Line) ([1-9]\d*)", item.location, re.I)
        if match:
            return {"kind": match[1].lower(), "value": item.location}
    return None


def locator_references(text, cited):
    """Return exact source-bound spans and their supplied evidence IDs."""
    references = []
    for item in cited:
        locator = typed_locator(item)
        if locator is None:
            continue
        nouns = (r"(?:recording|transcript|audio|video|passage|segment|source)"
                 if locator["kind"] == "transcript_position" else
                 r"(?:record|entry|sheet|log|report|document|source|passage|note)s?")
        anchor = rf"(?:\b{nouns}|(?<!\w){re.escape(item.source_name)})"
        pattern = (anchor + r"\s+(?:at|from|in|on|for)\s+(?:the\s+)?(?P<locator>"
                   + re.escape(item.location) + r")(?![\w:-]|\.\d)")
        for match in re.finditer(pattern, text, re.I):
            context = re.split(r"[;?!]", text[:match.start('locator')])[-1]
            named = [(m.start(), source.source_name) for source in cited
                     for m in re.finditer(re.escape(source.source_name), context, re.I)]
            if named and max(named)[1] != item.source_name:
                continue
            references.append((*match.span('locator'), item.evidence_id))
    return references


def without_locator_references(text, cited):
    """Mask reference occurrences, never a numeric value everywhere it occurs.

    Quotes are checked first. A second occurrence as an event time/count still
    needs excerpt support; a named source cannot borrow another source's locator.
    """
    spans = {(start, end) for start, end, _ in locator_references(text, cited)}
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + "cited location" + text[end:]
    return text


def cross_locator_drop_reason(text, cited):
    """Do not attach another cited passage's detail to an explicit location."""
    from .question_drafting import _digit_tokens, unsupported_quotes
    from .question_language import _content

    referenced = {identifier for _, _, identifier in locator_references(text, cited)}
    if not referenced or referenced == {item.evidence_id for item in cited}:
        return None
    located = [item for item in cited if item.evidence_id in referenced]
    elsewhere = [item for item in cited if item.evidence_id not in referenced]
    factual = without_locator_references(text, cited).casefold()
    located_text = " ".join(item.excerpt for item in located)
    other_text = " ".join(item.excerpt for item in elsewhere)
    if (unsupported_quotes(text, located)
            or _digit_tokens(factual) - _digit_tokens(located_text.casefold())
            or _content(text) & (_content(other_text) - _content(located_text))):
        return "cross_locator_detail"
    return None


def speaker_turns(item):
    if item.evidence_kind != "transcript":
        return []
    matches = list(re.finditer(r"\b(Speaker\s+\d+)\s*:", item.excerpt, re.I))
    return [{"label": match[1], "text": item.excerpt[match.end():
             matches[index + 1].start() if index + 1 < len(matches) else len(item.excerpt)].strip()}
            for index, match in enumerate(matches)]


def retention_summary(attempts, audit):
    rows = []
    for number, raw in enumerate(attempts, 1):
        values = raw.get("questions") if isinstance(raw, dict) else None
        rows.append({"attempt": number, "raw_candidates": len(values) if isinstance(values, list) else 0,
                     "retained_candidates": sum(key.startswith(f"a{number}q") for key in audit.candidate_ids),
                     "drop_reasons": dict(Counter(drop["reason"] for drop in audit.drops
                                                   if drop["attempt"] == number)),
                     "valid_batch": (isinstance(raw, dict) and set(raw) == {"questions"}
                                     and isinstance(values, list) and len(values) <= 8)})
    return {"minimum_questions": 3, "retained_questions": len(audit.questions),
            "shortfall": max(0, 3 - len(audit.questions)), "attempts": rows}


_REPAIR_GUIDANCE = {
    "uncited_digit_bearing_detail": "Remove factual digits absent from the cited excerpts; never reuse locator digits as facts.",
    "unstated_identity_or_named_detail": "Remove names/places absent from the cited excerpts; do not borrow another passage's context.",
    "missing_transcript_attribution": "Name the actual Speaker label for one account and preserve that turn's negation/condition; asking which speaker is insufficient.",
    "cross_speaker_detail": "Separate the accounts; do not attach another speaker's detail to this label.",
    "wrong_transcript_speaker_quotation": "Any attributed quotation must occur in that speaker's turn.",
    "transcript_negation_lost": "Keep the original negative claim with its speaker; neither omit it nor turn it into its opposite.",
    "transcript_condition_lost": "Keep the condition with the event; if any records exist does not prove the event occurred.",
    "near_duplicate": "Choose a different source-supported angle, not a paraphrase of an existing question.",
    "cross_locator_detail": "Do not attribute another passage's details to this source location; recheck the record relationship.",
    "citation_or_text_verification_failed": "Recheck the original citation/text contract, including apparent transcript framing and if any for discovery.",
}


def repair_feedback(attempts, audit, evidence):
    """Expose precise defects and cited support without rewriting a candidate."""
    from .question_drafting import _digit_tokens
    from .question_grounding import _SAFE_CAPITALS
    from .question_language import without_source_references

    by_id = {item.evidence_id: item for item in evidence}
    guidance, used = [], set()
    for drop in audit.drops:
        raw = drop["raw"]
        ids = raw.get("evidence_ids") if isinstance(raw, dict) else None
        ids = [key for key in ids if isinstance(key, str) and key in by_id] if isinstance(ids, list) else []
        cited = [by_id[key] for key in ids]
        used.update(ids)
        text = raw.get("text", "") if isinstance(raw, dict) else ""
        text = text if isinstance(text, str) else ""
        source = " ".join(item.excerpt for item in cited).casefold()
        factual_text = without_locator_references(text, cited)
        words = set(re.findall(r"\b\w+\b", source))
        names = [word for word in re.findall(r"\b[A-Z][A-Za-z'-]+\b", without_source_references(text, cited))
                 if word.casefold() not in words | _SAFE_CAPITALS]
        guidance.append({"candidate_id": drop["candidate_id"], "reason": drop["reason"],
                         "cited_evidence_ids": ids,
                         "unsupported_digit_tokens": sorted(_digit_tokens(factual_text.casefold()) - _digit_tokens(source)),
                         "unsupported_named_details": sorted(set(names)),
                         "instruction": _REPAIR_GUIDANCE.get(drop["reason"], "Recheck this candidate against only its cited original passages.")})
    summary = retention_summary(attempts, audit)
    return {"remaining_supported_questions": summary["shortfall"],
            "attempt_retention": summary["attempts"], "repair_guidance": guidance,
            "cited_support": [{"evidence_id": item.evidence_id, "locator": typed_locator(item),
                               "speaker_turns": speaker_turns(item)} for item in evidence if item.evidence_id in used]}
