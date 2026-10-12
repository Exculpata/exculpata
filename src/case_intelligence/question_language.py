"""Narrow wording checks for record references and attributed transcript questions.

These checks supplement citation verification, not a general entailment model.
Metadata can identify a cited source; it never supplies a quotation or number.
"""
from __future__ import annotations

from dataclasses import replace
import re


def without_source_references(text, cited):
    """Set aside exact metadata only in an explicit source-reference position."""
    for item in cited:
        for value in (item.source_name, item.location):
            # Longest exact names are not decomposed into an allow-list of words.
            # A name in an asserted event (rather than after a locator) stays checked.
            pattern = r"\b(?:in|from|of|according to|listed at|located at)\s+(?:the\s+)?" + re.escape(value) + r"(?!\w)"
            text = re.sub(pattern, " in the cited source ", text, flags=re.I)
    return text


def unstated_plural_pronouns(text, source_words):
    """Allow local record anaphora without treating an absent person as known."""
    records = r"(?:records|logs|documents|entries|reports|recordings|files)"
    # These are object references within the same clause, not a blanket exemption
    # for any sentence that happens to mention a record.
    text = re.sub(r"\b" + records + r"\b[^.!?;]{0,60}?\b(?:what|that|which) they "
                  r"(?:contain|show|list|record|document|describe)\b", " records contain ", text, flags=re.I)
    text = re.sub(r"\b" + records + r"\s+and their (?:entries|dates|contents|metadata)\b",
                  " records contents ", text, flags=re.I)
    return (set(re.findall(r"\b\w+\b", text.casefold())) & {"they", "them", "their", "theirs"}) - source_words


_SPEAKER = re.compile(r"\bSpeaker\s+\d+\b", re.I)
_NEGATION = re.compile(r"\b(?:not|never|cannot|can't|didn't|doesn't|couldn't|hasn't|hadn't)\b", re.I)
_CONDITION = re.compile(r"\b(?:if|whether|might|may|could|would|uncertain|unresolved)\b", re.I)
_WORD_FORMS = {"saw": "see", "seen": "see", "seeing": "see", "emailed": "email",
               "sent": "send", "sending": "send", "received": "receive", "receiving": "receive"}


def _content(text):
    from .generation import _STOP_WORDS, _tokens
    generic = {"speaker", "recording", "transcript", "appears", "appear", "says", "said", "say",
               "please", "provide", "request", "records", "record", "clarify", "unidentified",
               "machine", "synthetic", "about", "what", "which", "whether", "never", "not",
               "can", "could", "would", "should", "might", "may", "who", "where", "when", "how"}
    return {_WORD_FORMS.get(word, word) for word in _tokens(text)
            if len(word) > 2 and word not in _STOP_WORDS | generic and not word.isdigit()}


def transcript_drop_reason(text, cited):
    """Keep a labelled speaker's words, negation and conditions with that speaker."""
    from .generation import TRANSCRIPT_EVIDENCE_KIND
    from .question_drafting import unsupported_quotes

    turns = []
    for item in cited:
        if item.evidence_kind != TRANSCRIPT_EVIDENCE_KIND:
            continue
        matches = list(re.finditer(r"\b(Speaker\s+\d+)\s*:", item.excerpt, re.I))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(item.excerpt)
            turns.append((match[1].casefold(), replace(item, excerpt=item.excerpt[match.start():end])))
    if not turns:
        return None
    labels = {label for label, _ in turns}
    requested = {match[0].casefold() for match in _SPEAKER.finditer(text)}
    terms = _content(text)
    discusses_turn = (any(terms & _content(item.excerpt) for _, item in turns)
                      or re.search(r"\bspeakers?\b", text, re.I))
    if requested - labels:
        return "unsupported_transcript_speaker"
    if not requested and not discusses_turn:
        return None
    if len(labels) > 1 and not requested:
        return "missing_transcript_attribution"
    selected = [item for label, item in turns if not requested or label in requested]
    # A quotation cannot be borrowed from a different speaker in the same passage.
    if unsupported_quotes(text, selected):
        return "wrong_transcript_speaker_quotation"
    own_terms = set().union(*(_content(item.excerpt) for item in selected))
    other_terms = set().union(*(_content(item.excerpt) for label, item in turns if label not in requested))
    if requested and terms & (other_terms - own_terms):
        return "cross_speaker_detail"
    for item in selected:
        for clause in re.split(r"[.!?;]\s+", item.excerpt):
            overlap = terms & _content(clause)
            if len(overlap) < 2:
                continue
            if _NEGATION.search(clause) and not _NEGATION.search(text):
                return "transcript_negation_lost"
            if _CONDITION.search(clause) and not _CONDITION.search(text):
                return "transcript_condition_lost"
    return None
