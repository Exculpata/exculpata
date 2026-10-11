"""Deterministic review-quality policy shared by retrieval and grounded answers.

The helpers in this module operate only on already matter-scoped candidates and
verified citation metadata.  They never resolve sources, authorize access, or
turn a retrieval miss into a substantive claim.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Mapping, Protocol, Sequence, TypeVar


DOCUMENT_EVIDENCE_KIND = "document"
TRANSCRIPT_EVIDENCE_KIND = "transcript"
_WRITTEN_PATTERN = (
    r"(?:written|documentary|documents?|reports?|emails?|correspondence|"
    r"messages?|notes?|logs?)"
)
_SPOKEN_PATTERN = (
    r"(?:spoken|oral|audio|recordings?|transcripts?|interviews?|"
    r"phone\s+calls?|telephone\s+calls?|testimony|voicemails?)"
)
_WRITTEN_TERMS = re.compile(
    rf"\b{_WRITTEN_PATTERN}\b",
    re.IGNORECASE,
)
_SPOKEN_TERMS = re.compile(
    rf"\b{_SPOKEN_PATTERN}\b",
    re.IGNORECASE,
)
_MODALITY_JOIN_GAP = re.compile(
    r"^\s*(?:[a-z][a-z'-]*\s+){0,2}(?:,\s*)?"
    r"(?:and|with|alongside|as\s+well\s+as|together\s+with|but\s+also)\s+"
    r"(?:(?:the|a|an)\s+)?(?:[a-z][a-z'-]*\s*){0,2}$",
    re.IGNORECASE,
)
_MODALITY_COMPARISON_REQUEST = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)?(?:compare|"
    r"(?:can|could|would)\s+you\s+compare)\b",
    re.IGNORECASE,
)
_MODALITY_COMMAND = (
    r"(?:answer|respond|use|cite|rely|search|review|consult|consider|"
    r"summarize|compare)"
)
_BROAD_SUMMARY = re.compile(
    r"\b(?:broad|overall|matter-wide|case-wide|general)\s+"
    r"(?:summary|overview|orientation)\b|"
    r"\b(?:matter|case|record|evidence)\s+(?:summary|overview|orientation)\b|"
    r"\b(?:summary|overview|orientation)\s+(?:of|for)\s+"
    r"(?:all(?:\s+the)?|this|the)\s+"
    r"(?:matter|case|evidence|record|records|sources?)\b|"
    r"\b(?:summarize|orient\s+me\s+to)\s+(?:this|the)\s+"
    r"(?:matter|case|evidence|record|records|sources?)\b|"
    r"\bwhat\s+(?:does|do)\s+(?:this|the)\s+(?:matter|case|evidence|record)\s+"
    r"(?:show|establish)\s+overall\b|"
    r"\bwhat\s+are\s+(?:the\s+)?key\s+(?:facts|events|points|findings)\s+"
    r"(?:across|in|from)\s+(?:(?:this|the)\s+)?"
    r"(?:matter|case|evidence|record|records|sources?)\b|"
    r"\b(?:summarize|review)\s+(?:all|the)\s+(?:evidence|records|sources?)\b",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(
    r"\b(?=[A-Z0-9-]{4,}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)"
    r"[A-Z0-9]+(?:-[A-Z0-9]+)*\b"
)
_SOURCE_REFERENCE_IDENTIFIER = re.compile(
    r"\b(?:(?:using|from|per)\s+"
    r"(?:(?:the|a|an)\s+)?(?:reports?|documents?|sources?|files?|exhibits?)\s+|"
    r"according\s+to\s+(?:(?:(?:the|a|an)\s+)?"
    r"(?:reports?|documents?|sources?|files?|exhibits?)\s+)?)"
    r"(?P<identifier>\b(?=[A-Z0-9-]{4,}\b)(?=[A-Z0-9-]*[A-Z])"
    r"(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)*\b)",
    re.IGNORECASE,
)
_TEMPORAL_OBJECTIVE = re.compile(
    r"\b(?:at\s+what\s+(?:exact\s+)?time|what\s+(?:exact\s+)?time|when\s+did|"
    r"exact\s+(?:time|timestamp)|precise\s+(?:time|timestamp)|timestamp)\b",
    re.IGNORECASE,
)
_EXACT_CLOCK_OBJECTIVE = re.compile(
    r"\b(?:at\s+what\s+(?:exact\s+)?time|what\s+(?:exact\s+)?time|"
    r"exact\s+(?:time|timestamp)|precise\s+(?:time|timestamp)|timestamp|"
    r"(?:determine|identify|report|give)\s+(?:the\s+)?"
    r"(?:(?:recorded|accepted|precise|exact)\s+)?(?:clock\s+)?time)\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,3})?)?"
    r"(?:\s*(?:a\.?m\.?|p\.?m\.?))?(?!\d)",
    re.IGNORECASE,
)
_CLOCK_WORD = re.compile(r"\b(?:noon|midnight)\b", re.IGNORECASE)
_AUTHORITATIVE_RECORD = re.compile(
    r"\b(?:access|audit|event|transaction|dispatch|system|machine|sensor|badge|camera)\s+"
    r"(?:log|record|register|ledger|index|metadata)\b|"
    r"\b(?:timestamped|time-stamped|machine-generated|system-generated)\b|"
    r"\b(?:log|register|ledger)\s+(?:entry|row)\b",
    re.IGNORECASE,
)
_QUERY_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_EXACT_QUERY_STOP_TERMS = {
    "a",
    "an",
    "at",
    "did",
    "do",
    "does",
    "event",
    "exact",
    "happen",
    "happened",
    "in",
    "is",
    "it",
    "log",
    "occur",
    "occurred",
    "of",
    "on",
    "record",
    "recorded",
    "system",
    "the",
    "time",
    "timestamp",
    "to",
    "was",
    "what",
    "when",
}
_LATER_SUMMARY = re.compile(
    r"\b(?:later|retrospective|subsequent|narrative|overview|summary|brief|recap)\b",
    re.IGNORECASE,
)
_BOILERPLATE_SENTENCE = re.compile(
    r"\b(?:generated|synthetic|demonstration|demo|fixture|sample)\b.*"
    r"\b(?:generated|synthetic|demonstration|demo|fixture|sample|testing)\b|"
    r"\b(?:not|isn't|is\s+not)\s+(?:legal|professional)\s+advice\b|"
    r"\b(?:for\s+(?:demonstration|testing|training)\s+purposes?\s+only)\b|"
    r"\b(?:confidentiality|privilege)\s+(?:notice|disclaimer)\b|"
    r"\b(?:terms\s+of\s+use|privacy\s+notice|all\s+rights\s+reserved)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QuestionIntent:
    required_evidence_kinds: tuple[str, ...] = ()
    excluded_evidence_kinds: tuple[str, ...] = ()
    broad_summary: bool = False


def _explicitly_excludes_modality(value: str, modality_pattern: str) -> bool:
    """Recognize bounded source-selection instructions, not narrative negation."""

    patterns = (
        rf"\b(?:do\s+not|don't)\s+"
        rf"(?:use|cite|search|review|consult|consider|include|rely\s+on)\s+"
        rf"(?:the\s+)?{modality_pattern}\b",
        rf"\b(?:exclude|excluding|omit|omitting|ignore|ignoring|avoid|avoiding)\s+"
        rf"(?:the\s+)?{modality_pattern}\b",
        rf"\b{_MODALITY_COMMAND}\b[^.!?]{{0,80}}\b"
        rf"(?:rather\s+than|instead\s+of)\s+(?:the\s+)?{modality_pattern}\b",
        rf"(?:^|[,;:]|\u2014|\bbut\b)\s*not\s+(?!(?:only|just)\b)(?:the\s+)?{modality_pattern}\b",
    )
    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def _explicitly_joins_modalities(value: str) -> bool:
    """Require the joining words to occur between the two source-kind terms."""

    written = tuple(_WRITTEN_TERMS.finditer(value))
    spoken = tuple(_SPOKEN_TERMS.finditer(value))
    if written and spoken and _MODALITY_COMPARISON_REQUEST.search(value):
        return True
    for written_match in written:
        for spoken_match in spoken:
            left, right = sorted(
                (written_match, spoken_match), key=lambda item: item.start()
            )
            if _MODALITY_JOIN_GAP.fullmatch(value[left.end() : right.start()]):
                return True
    return False


class RankedCandidate(Protocol):
    matter_id: str
    source_name: str
    text: str


class CitationLike(Protocol):
    support_token: str
    document_id: str
    evidence_kind: str
    excerpt: str


CandidateT = TypeVar("CandidateT", bound=RankedCandidate)
CitationT = TypeVar("CitationT", bound=CitationLike)


def classify_question(question: str) -> QuestionIntent:
    """Identify only explicit evidence-shape requests; never infer hidden intent."""

    value = " ".join((question or "").split()).strip()
    has_written = bool(_WRITTEN_TERMS.search(value))
    has_spoken = bool(_SPOKEN_TERMS.search(value))
    excluded: list[str] = []
    if _explicitly_excludes_modality(value, _WRITTEN_PATTERN):
        excluded.append(DOCUMENT_EVIDENCE_KIND)
    if _explicitly_excludes_modality(value, _SPOKEN_PATTERN):
        excluded.append(TRANSCRIPT_EVIDENCE_KIND)
    joined = _explicitly_joins_modalities(value)
    if excluded:
        required = tuple(
            kind
            for kind, mentioned in (
                (DOCUMENT_EVIDENCE_KIND, has_written),
                (TRANSCRIPT_EVIDENCE_KIND, has_spoken),
            )
            if mentioned and kind not in excluded
        )
    else:
        required = (
            (DOCUMENT_EVIDENCE_KIND, TRANSCRIPT_EVIDENCE_KIND)
            if has_written and has_spoken and joined
            else ()
        )
    return QuestionIntent(
        required_evidence_kinds=required,
        excluded_evidence_kinds=tuple(excluded),
        broad_summary=bool(_BROAD_SUMMARY.search(value)),
    )


def _query_subject_terms(query: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _QUERY_TOKEN.findall(query.casefold())
        if len(token) > 1 and token not in _EXACT_QUERY_STOP_TERMS
    )


def _exact_record_features(
    query: str,
    candidate: RankedCandidate,
    *,
    subject_terms: frozenset[str],
) -> tuple[int, int, int, int]:
    haystack = f"{candidate.source_name}\n{candidate.text}"
    identifiers = tuple(dict.fromkeys(_IDENTIFIER.findall(query.upper())))
    candidate_identifiers = frozenset(_IDENTIFIER.findall(haystack.upper()))
    identifier_matches = sum(
        1 for identifier in identifiers if identifier in candidate_identifiers
    )
    clocks = _CLOCK.findall(haystack)
    temporal_precision = 0
    if _TEMPORAL_OBJECTIVE.search(query) and clocks:
        temporal_precision = 2 if any(value.count(":") >= 2 for value in clocks) else 1
    authority = int(bool(_AUTHORITATIVE_RECORD.search(haystack)))
    if _LATER_SUMMARY.search(candidate.source_name):
        authority -= 1
    candidate_terms = set(_QUERY_TOKEN.findall(haystack.casefold()))
    subject_overlap = len(subject_terms.intersection(candidate_terms))
    return identifier_matches, int(subject_overlap > 0), temporal_precision, authority


def rank_exact_record_candidates(
    query: str,
    candidates: Sequence[CandidateT],
    *,
    matter_id: str,
) -> tuple[CandidateT, ...]:
    """Rescue precise first-party records from a weak rerank on exact-record questions.

    This is a stable post-rerank policy over the existing bounded candidate set.
    Scores, text, locators, and candidate identity are left untouched.
    """

    bounded = tuple(candidates)
    if any(item.matter_id != matter_id for item in bounded):
        raise RuntimeError("matter boundary violation in exact-record ranking")
    if not (_IDENTIFIER.search(query.upper()) or _TEMPORAL_OBJECTIVE.search(query)):
        return bounded
    subject_terms = _query_subject_terms(query)
    features = [
        _exact_record_features(query, item, subject_terms=subject_terms)
        for item in bounded
    ]
    # Precision and source authority may refine already relevant candidates;
    # they must never pull an unrelated timestamp above the user's subject.
    if not any(feature[0] or feature[1] for feature in features):
        return bounded
    ranked = sorted(
        enumerate(bounded),
        key=lambda pair: (
            -features[pair[0]][0],
            -features[pair[0]][1],
            -features[pair[0]][2],
            -features[pair[0]][3],
            pair[0],
        ),
    )
    return tuple(item for _index, item in ranked)


def rank_temporal_candidates(
    query: str,
    candidates: Sequence[CandidateT],
    *,
    matter_id: str,
) -> tuple[CandidateT, ...]:
    """Keep subject-matching clock observations in a bounded before/after packet.

    Lexical overlap rewards question filler and the cutoff itself, which can bury
    an observation at a different clock time. Stably promote matching passages
    on the requested side before the primary/anchor limits are applied. This is
    a coverage hint, not a finding about dates, clock accuracy or continuous
    custody. Keep the entire candidate (including attribution and caveats).
    """
    bounded = tuple(candidates)
    if any(item.matter_id != matter_id for item in bounded):
        raise RuntimeError("matter boundary violation in temporal ranking")
    clocks = tuple(_CLOCK.finditer(query))
    if len(clocks) != 1:
        return bounded
    clock = clocks[0]
    relation = re.search(r"\b(before|after)\s*$", query[:clock.start()], re.IGNORECASE)
    if relation is None or re.search(
        r"\b(?:not|never|no)\s*$", query[:relation.start()], re.IGNORECASE
    ):
        return bounded
    # Requiring every subject term prevents a shared filler word or an unrelated
    # timestamp from winning. Unrecognized wording conservatively keeps ranking.
    subject = query[:relation.start()] + query[clock.end():]
    filler = _EXACT_QUERY_STOP_TERMS | {
        "where", "were", "been", "had", "has", "have", "with", "which", "who",
        "can", "could", "you", "please", "tell", "me", "about", "and", "or",
    }
    terms = frozenset(_QUERY_TOKEN.findall(subject.casefold())) - filler
    if not terms:
        return bounded

    def clock_seconds(value: str) -> float | None:
        parts = re.fullmatch(
            r"(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d{1,3})?))?"
            r"\s*(a\.?m\.?|p\.?m\.?)?", value, re.IGNORECASE,
        )
        if parts is None:
            return None
        hour, minute, second, period = parts.groups()
        hours = int(hour)
        if period:
            if not 1 <= hours <= 12:
                return None
            hours = hours % 12 + (12 if period.lower().startswith("p") else 0)
        return hours * 3600 + int(minute) * 60 + float(second or 0)

    cutoff = clock_seconds(clock.group())
    if cutoff is None:
        return bounded
    before = relation.group(1).casefold() == "before"

    def supports_period(item: CandidateT) -> bool:
        # Transcript clocks are playback offsets, not wall-clock observations.
        if getattr(item, "evidence_kind", DOCUMENT_EVIDENCE_KIND) == TRANSCRIPT_EVIDENCE_KIND:
            return False
        if not terms.issubset(_QUERY_TOKEN.findall(item.text.casefold())):
            return False
        times = (clock_seconds(match.group()) for match in _CLOCK.finditer(item.text))
        return any(value is not None and (value < cutoff if before else value > cutoff)
                   for value in times)

    return tuple(sorted(bounded, key=lambda item: not supports_period(item)))


def broad_summary_queries(question: str, *, maximum_chars: int = 512) -> tuple[str, ...]:
    """Return bounded, content-free facets for a broad orientation request."""

    base = " ".join((question or "").split()).strip()
    if not base:
        return ()
    if not classify_question(base).broad_summary:
        return (base[:maximum_chars],)
    variants = (
        base,
        f"{base} chronology dates times sequence events",
        f"{base} people organizations roles written accounts spoken accounts",
        f"{base} contemporaneous records communications system logs",
        f"{base} corroboration conflicts corrections unresolved gaps",
    )
    result: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        bounded = variant[: max(int(maximum_chars), 1)].strip()
        key = bounded.casefold()
        if bounded and key not in seen:
            result.append(bounded)
            seen.add(key)
    return tuple(result)


def _boilerplate_only(text: str) -> bool:
    sentences = tuple(
        value.strip()
        for value in re.split(r"(?:[.!?]+|\n+)", " ".join((text or "").split()))
        if value.strip()
    )
    return bool(sentences) and all(_BOILERPLATE_SENTENCE.search(value) for value in sentences)


def filter_broad_summary_evidence(citations: Sequence[CitationT]) -> tuple[CitationT, ...]:
    """Remove disclaimer-only orientation hits, retaining any substantive passage."""

    return tuple(item for item in citations if not _boilerplate_only(item.excerpt))


def prioritize_evidence_kinds(
    citations: Sequence[CitationT],
    *,
    maximum: int,
    required_kinds: Sequence[str] = (),
) -> tuple[CitationT, ...]:
    """Reserve one stable slot per explicitly requested evidence kind."""

    limit = max(int(maximum), 0)
    if not limit:
        return ()
    rows = tuple(citations)
    selected: list[CitationT] = []
    seen: set[str] = set()

    def append(item: CitationT) -> None:
        if len(selected) < limit and item.support_token not in seen:
            selected.append(item)
            seen.add(item.support_token)

    for kind in tuple(dict.fromkeys(required_kinds)):
        matched = next((item for item in rows if item.evidence_kind == kind), None)
        if matched is not None:
            append(matched)
    for item in rows:
        append(item)
    return tuple(selected)


def modality_coverage(
    question: str,
    evidence: Mapping[str, CitationLike],
    used_evidence_ids: Sequence[str],
) -> dict[str, object]:
    """Describe requested-modality completion without asserting a collection-wide absence."""

    required = classify_question(question).required_evidence_kinds
    if not required:
        return {}
    labels = {DOCUMENT_EVIDENCE_KIND: "written", TRANSCRIPT_EVIDENCE_KIND: "spoken"}
    available = {
        item.evidence_kind for item in evidence.values() if item.evidence_kind in labels
    }
    used = {
        evidence[identifier].evidence_kind
        for identifier in used_evidence_ids
        if identifier in evidence and evidence[identifier].evidence_kind in labels
    }
    missing = tuple(kind for kind in required if kind not in used)
    unavailable = tuple(kind for kind in missing if kind not in available)
    if not missing:
        names = " and ".join(labels[kind] for kind in required)
        notice = (f"This answer cites {names} passages. "
            "Check each claim against the original sources.")
        mode = "complete"
    elif unavailable:
        names = " and ".join(labels[kind] for kind in unavailable)
        notice = (
            f"This result is partial: no matching {names} evidence was retrieved in "
            "the selected passages. Review the generated result and search that source "
            "type directly before treating the comparison as complete."
        )
        mode = "partial"
    else:
        names = " and ".join(labels[kind] for kind in missing)
        notice = (
            f"This result is partial: {names} evidence reached the answer packet, but "
            "no generated claim citing those passages was retained. Review the matching source "
            "or refine the question before treating the comparison as complete."
        )
        mode = "partial"
    return {
        "mode": mode,
        "requested_evidence_kinds": [labels[kind] for kind in required],
        "available_evidence_kinds": [labels[kind] for kind in required if kind in available],
        "used_evidence_kinds": [labels[kind] for kind in required if kind in used],
        "missing_evidence_kinds": [labels[kind] for kind in missing],
        "notice": notice,
    }


def research_step_question(objective: str, query: str) -> str:
    """Keep a broadened retrieval pass subordinate to the saved objective."""

    objective_value = " ".join((objective or "").split()).strip()
    query_value = " ".join((query or "").split()).strip()
    prefix = "Original research objective: "
    bridge = "\nCurrent evidence pass: "
    suffix = "\nReport only source-supported points that advance the original objective."
    room = 2_000 - len(prefix) - len(bridge) - len(suffix)
    objective_value = objective_value[: max(room - min(len(query_value), 400), 1)].rstrip()
    query_value = query_value[: max(room - len(objective_value), 1)].rstrip()
    return f"{prefix}{objective_value}{bridge}{query_value}{suffix}"[:2_000]


def research_synthesis_question(objective: str) -> str:
    """Direct final synthesis back to the saved objective, not the search facets."""

    objective_value = " ".join((objective or "").split()).strip()
    prefix = "Answer the original research objective directly from the supplied evidence: "
    suffix = " Identify any part the supplied evidence does not resolve."
    if len(prefix) + len(objective_value) + len(suffix) > 2_000:
        return objective_value[:2_000]
    return f"{prefix}{objective_value}{suffix}"


def _timed_objective_identifiers(question: str) -> tuple[str, ...]:
    normalized = question.upper()
    matches = tuple(_IDENTIFIER.finditer(normalized))
    identifiers = tuple(dict.fromkeys(match.group(0) for match in matches))
    if len(identifiers) < 2:
        return identifiers
    source_reference_spans = {
        match.span("identifier")
        for match in _SOURCE_REFERENCE_IDENTIFIER.finditer(normalized)
    }
    timed_subjects = tuple(
        identifier
        for identifier in identifiers
        if any(
            match.span() not in source_reference_spans
            for match in matches
            if match.group(0) == identifier
        )
    )
    return timed_subjects or identifiers


def answer_advances_objective(question: str, answer_text: str) -> bool:
    """Check only objective shapes that can be verified without case semantics."""

    if _EXACT_CLOCK_OBJECTIVE.search(question):
        has_clock = bool(_CLOCK.search(answer_text) or _CLOCK_WORD.search(answer_text))
        if not has_clock:
            return False
        identifiers = _timed_objective_identifiers(question)
        if not identifiers:
            return True
        segments = tuple(
            segment.strip()
            for segment in re.split(r"(?:\n+|[;]+|(?<=[.!?])\s+)", answer_text)
            if segment.strip()
        )

        def identifier_has_associated_clock(identifier: str) -> bool:
            for segment in segments:
                identifier_positions = tuple(
                    (match.group(0).casefold(), match.start())
                    for match in _IDENTIFIER.finditer(segment.upper())
                )
                target_positions = tuple(
                    position
                    for value, position in identifier_positions
                    if value == identifier.casefold()
                )
                if not target_positions:
                    continue
                clock_positions = tuple(
                    match.start() for match in _CLOCK.finditer(segment)
                ) + tuple(match.start() for match in _CLOCK_WORD.finditer(segment))
                for clock_position in clock_positions:
                    nearest_distance = min(
                        abs(clock_position - position)
                        for _value, position in identifier_positions
                    )
                    if any(
                        abs(clock_position - target_position) == nearest_distance
                        for target_position in target_positions
                    ):
                        return True
            return False

        return all(
            identifier_has_associated_clock(identifier)
            for identifier in identifiers
        )
    return True


@dataclass(frozen=True)
class QualityEvaluationCase:
    case_id: str
    category: str
    prompt: str
    expected: str
    synthesis_source_withheld: bool = False


QUALITY_EVALUATION_CASES = (
    QualityEvaluationCase(
        "exact-event-time",
        "retrieval_rank",
        "At what exact time did system event EVT-4821 occur?",
        "Prefer the precise contemporaneous event record over a later narrative summary.",
    ),
    QualityEvaluationCase(
        "written-and-spoken",
        "cross_modal_completeness",
        "Using both the written report and spoken recording, what identifier appears?",
        "Retain page and timestamp support, or state which requested modality is partial.",
    ),
    QualityEvaluationCase(
        "written-only-partial",
        "cross_modal_completeness",
        "Compare the written record with what was said in the recording.",
        "Report a clear partial result when no source-verified spoken claim is retained.",
    ),
    QualityEvaluationCase(
        "objective-with-final-withheld",
        "generator_objective",
        "Determine the recorded time for event EVT-4821 from the component records.",
        "Answer the saved objective without relying on or quoting a final narrative report.",
        synthesis_source_withheld=True,
    ),
    QualityEvaluationCase(
        "broad-orientation",
        "broad_summary_scope",
        "Give a broad summary of this matter's evidence.",
        "Use source-diverse facets, omit disclaimer-only passages, and disclose sampled scope.",
    ),
)


def quality_evaluation_fingerprint() -> str:
    payload = [asdict(item) for item in QUALITY_EVALUATION_CASES]
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


QUALITY_EVALUATION_FINGERPRINT = (
    "3f81a91de77b5a209abf630c3d648392724ff949a9090da8c24cfc7977ee8d5d"
)
