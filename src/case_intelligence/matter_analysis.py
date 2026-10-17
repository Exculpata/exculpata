"""Bounded, deterministic review-map candidates over already extracted units."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Sequence

from .review_bench_v2 import Candidate

MAX_ANALYSIS_UNITS = 2_500
MAX_FINDINGS = 150
MAX_UNIT_CHARS = 6_000
_DATE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+[0-3]?\d(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b|"
    r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:\d{2}|\d{4})\b"
)
_PERSON = re.compile(
    r"\b(?:Officer|Detective|Det\.|Agent|Sergeant|Sgt\.|Lieutenant|Lt\.|"
    r"Captain|Capt\.|Dr\.|Mr\.|Mrs\.|Ms\.|Judge|Attorney)\s+"
    r"[A-Z][A-Za-z'’-]{1,40}(?:\s+[A-Z][A-Za-z'’-]{1,40}){0,2}\b"
)
_PLACE = re.compile(
    r"\b(?:at|near|inside|outside|located at|arrived at)\s+(?:the\s+)?"
    r"(?P<place>[A-Z][A-Za-z0-9'’.-]{1,40}(?:\s+[A-Z0-9][A-Za-z0-9'’.-]{0,40}){0,4})"
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[a-z0-9][a-z0-9'’-]*")
_NEGATION = {"no", "not", "never", "without", "cannot", "can't", "didn't", "wasn't", "weren't", "denied"}
_STOP = {
    "a",
    "an",
    "and",
    "as",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


@dataclass(frozen=True)
class AnalysisFinding:
    kind: str
    signature: str
    title: str
    summary: str
    references: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class AnalysisResult:
    source_count: int
    unit_count: int
    entity_count: int
    capped: bool
    findings: tuple[AnalysisFinding, ...]


def _normalized(value: str) -> str:
    return " ".join(_WORD.findall(value.casefold()))


def _signature(*values: str) -> str:
    return hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()


def _reference(
    candidate: Candidate,
    support_token: Callable[[Candidate], str],
    excerpt: str | None = None,
) -> dict[str, object]:
    return {
        "document_id": candidate.document_id,
        "source_version_id": candidate.source_version_id,
        "source_name": candidate.source_name,
        "location": candidate.citation,
        "unit_number": candidate.page_number,
        "chunk_id": candidate.chunk_id,
        "excerpt_digest": candidate.excerpt_digest,
        "excerpt": (excerpt or candidate.text)[:6_000],
        "support_token": support_token(candidate),
    }


def analyze_candidates(
    candidates: Sequence[Candidate],
    *,
    support_token: Callable[[Candidate], str],
) -> AnalysisResult:
    selected = tuple(candidates[:MAX_ANALYSIS_UNITS])
    capped = len(candidates) > len(selected)
    entities: dict[str, dict[str, tuple[str, Candidate]]] = defaultdict(dict)
    assertion_groups: dict[
        str, dict[bool, dict[str, tuple[Candidate, str]]]
    ] = defaultdict(lambda: {False: {}, True: {}})
    for candidate in selected:
        text = candidate.text[:MAX_UNIT_CHARS]
        matches: list[str] = []
        matches.extend(match.group(0) for match in _PERSON.finditer(text))
        matches.extend(match.group("place") for match in _PLACE.finditer(text))
        matches.extend(match.group(0) for match in _DATE.finditer(text))
        for label in matches:
            key = _normalized(label)
            if key:
                entities[key].setdefault(
                    candidate.document_id, (" ".join(label.split()), candidate)
                )
        for sentence in _SENTENCE.split(text):
            value = " ".join(sentence.split()).strip()
            if not 20 <= len(value) <= 500:
                continue
            words = _WORD.findall(value.casefold())
            negative = any(word in _NEGATION for word in words)
            base = tuple(
                word for word in words if word not in _NEGATION and word not in _STOP
            )
            if len(base) < 5 or not (
                _PERSON.search(value) or _DATE.search(value) or re.search(r"\d", value)
            ):
                continue
            key = " ".join(base)
            assertion_groups[key][negative].setdefault(
                candidate.document_id, (candidate, value)
            )

    pair_entities: dict[tuple[str, str], list[tuple[str, Candidate, Candidate]]] = (
        defaultdict(list)
    )
    for by_document in entities.values():
        document_ids = sorted(by_document)
        for left_index, left_id in enumerate(document_ids):
            for right_id in document_ids[left_index + 1 :]:
                left_label, left = by_document[left_id]
                _, right = by_document[right_id]
                pair_entities[(left_id, right_id)].append((left_label, left, right))

    findings: list[AnalysisFinding] = []
    for (left_id, right_id), shared in sorted(
        pair_entities.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        labels = tuple(dict.fromkeys(item[0] for item in shared))[:8]
        if not labels:
            continue
        left = shared[0][1]
        right = shared[0][2]
        findings.append(
            AnalysisFinding(
                "comparison",
                _signature("comparison", left_id, right_id, *sorted(_normalized(v) for v in labels)),
                f"Compare {left.source_name} and {right.source_name}",
                "These sources reference "
                + ", ".join(labels)
                + ". Compare their context, sequence, and wording. Shared references do not establish that the accounts agree.",
                (
                    _reference(left, support_token),
                    _reference(right, support_token),
                ),
            )
        )
        if len(findings) >= MAX_FINDINGS:
            capped = True
            break

    if len(findings) < MAX_FINDINGS:
        for base, polarity in assertion_groups.items():
            positives = polarity[False]
            negatives = polarity[True]
            pairs = [
                (positive_id, negative_id)
                for positive_id in sorted(positives)
                for negative_id in sorted(negatives)
                if positive_id != negative_id
            ]
            if not pairs:
                continue
            positive_id, negative_id = pairs[0]
            positive, positive_excerpt = positives[positive_id]
            negative, negative_excerpt = negatives[negative_id]
            findings.append(
                AnalysisFinding(
                    "contradiction",
                    _signature(
                        "contradiction",
                        base,
                        *sorted((positive_id, negative_id)),
                    ),
                    "Possible wording conflict across two sources",
                    "These excerpts use opposite polarity around substantially the same terms. Review the surrounding context, speaker attribution, and source quality before treating this as a contradiction.",
                    (
                        _reference(positive, support_token, positive_excerpt),
                        _reference(negative, support_token, negative_excerpt),
                    ),
                )
            )
            if len(findings) >= MAX_FINDINGS:
                capped = True
                break

    return AnalysisResult(
        source_count=len({candidate.document_id for candidate in selected}),
        unit_count=len(selected),
        entity_count=len(entities),
        capped=capped,
        findings=tuple(findings),
    )


def possible_inconsistencies(units, entities, resolve):
    """New typed comparisons share the legacy analysis polarity vocabulary.

    Legacy contradiction findings and their stored review decisions are unchanged.
    """
    from .statement_comparison import attribute_statements, compare, validate_candidates
    statements = attribute_statements(units, entities)
    candidates, dropped = validate_candidates(compare(statements), resolve)
    return statements, candidates, dropped
