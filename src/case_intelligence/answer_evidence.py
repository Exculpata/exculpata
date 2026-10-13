"""Bound focused-answer inputs without changing their original citation basis."""
from collections import Counter
import re

from .generation import EvidenceItem, MAX_EVIDENCE_CHARS, MAX_EVIDENCE_ITEM_CHARS, MAX_EVIDENCE_ITEMS

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_FILLER = frozenset("a an and are as at be by did do does for from had has have how i in is it its me of on or please say says source sources tell that the their there these they this to was were what when where which who why with would".split())


def _terms(value):
    return {m.group().casefold() for m in _WORD.finditer(value)} - _FILLER


def evidence_window(text, question, context, limit):
    """Return one exact contiguous span, preferring rare current-question terms.

    Match offsets come from the original string, never its case-folded copy.
    A sliding counter bounds scoring cost even for long repetitive passages.
    Nonadjacent ranges are never joined into apparent source text.
    """
    if len(text) <= limit:
        return 0, len(text)
    primary = _terms(question)
    secondary = _terms(context) - primary
    terms = primary | secondary
    matches = [(m.start(), m.end(), m.group().casefold()) for m in _WORD.finditer(text)
               if m.group().casefold() in terms]
    start = 0
    if matches:
        frequency = Counter(word for _, _, word in matches)
        # Center on the current question when it matches. History must not move
        # its only matching sentence to a clipped edge just to include more context.
        anchors = [offset for offset, _, word in matches if word in primary]
        if not anchors:
            anchors = [offset for offset, _, _ in matches]
        starts = sorted({max(0, min(offset - limit // 2, len(text) - limit))
                         for offset in anchors})
        counts = Counter()
        left = right = 0
        best = (-1, -1)
        for candidate in starts:
            while right < len(matches) and matches[right][1] <= candidate + limit:
                counts[matches[right][2]] += 1
                right += 1
            while left < right and matches[left][0] < candidate:
                counts[matches[left][2]] -= 1
                if not counts[matches[left][2]]:
                    del counts[matches[left][2]]
                left += 1
            score = (sum(1 / frequency[word] for word in counts if word in primary),
                     sum(1 / frequency[word] for word in counts if word in secondary))
            if score > best:
                best, start = score, candidate
    end = min(len(text), start + limit)
    # Avoid fabricating partial words at a clipped edge when a boundary is near.
    if start and text[start - 1].isalnum() and text[start].isalnum():
        boundary = re.search(r"\s", text[start:min(end, start + 160)])
        if boundary:
            start += boundary.end()
    if end < len(text) and text[end - 1].isalnum() and text[end].isalnum():
        boundaries = list(re.finditer(r"\s", text[max(start, end - 160):end]))
        if boundaries:
            end = max(start, end - 160) + boundaries[-1].start()
    return start, end


def answer_evidence_packet(question, evidence, *, context=""):
    """Keep every admitted slot within the unchanged item and packet ceilings."""
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        raise ValueError("Too many admitted answer passages")
    remaining = MAX_EVIDENCE_CHARS
    limits = {}
    # Small passages keep their complete text; divide remaining room fairly.
    ordered = sorted(evidence, key=lambda identifier: len(evidence[identifier].excerpt))
    for index, identifier in enumerate(ordered):
        limits[identifier] = min(len(evidence[identifier].excerpt), MAX_EVIDENCE_ITEM_CHARS,
                                 remaining // (len(ordered) - index))
        remaining -= limits[identifier]
    packet, passages = [], []
    for identifier, citation in evidence.items():
        start, end = evidence_window(citation.excerpt, question, context, limits[identifier])
        packet.append(EvidenceItem(identifier, citation.source_name, citation.location,
                                   citation.excerpt[start:end], citation.evidence_kind,
                                   document_id=citation.document_id))
        passages.append(dict(evidence_id=identifier, start=start, end=end,
                             original_chars=len(citation.excerpt),
                             omitted_chars=len(citation.excerpt) - (end - start)))
    omitted = sum(row["omitted_chars"] for row in passages)
    notice = (f"Bounded source windows were supplied for this answer; {omitted:,} characters "
              "from the selected passages were omitted. Windows favor matching question terms; "
              "passages without literal matches use their beginning. Review the full cited "
              "passages for omitted context.") if omitted else ""
    return tuple(packet), dict(version=1, passages=passages, omitted_chars=omitted, notice=notice)


def disclose_evidence_windows(payload, windows):
    payload["evidence_windows"] = windows
    if windows["notice"]:
        payload["review_scope"]["notice"] += " " + windows["notice"]
