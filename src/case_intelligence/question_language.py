"""Narrow wording checks for record references and attributed transcript questions.

These checks supplement citation verification, not a general entailment model.
Metadata can identify a cited source; it never supplies a factual detail or quotation.
"""
from __future__ import annotations

from dataclasses import replace
import re


def without_source_references(text, cited):
    """Set aside exact metadata only in an explicit source-reference position."""
    from .question_context import transcript_record_references, without_locator_references
    if transcript_record_references(text, cited):
        return "Are there any other versions of the cited transcript?"
    text = without_locator_references(text, cited)
    for item in cited:
        # Only the source-bound appositive recognized above, in a request
        # position. A source name elsewhere remains factual prose to check.
        text = re.sub(r"^(?:request|please provide)\s+(?:the\s+)?" + re.escape(item.source_name)
                      + r"(?:\s+file)?(?=\s*,\s*specifically\s+cited location\b)",
                      "Please provide the cited source", text, flags=re.I)
        for value in (item.source_name, item.location):
            # Exact names are not decomposed into an allow-list of words.
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
_PASSIVE_EMAILED = re.compile(r"\b(?:was|were|is|are|be|been)\s+emailed\b", re.I)
_WORD_FORMS = {"saw": "see", "seen": "see", "seeing": "see", "emailed": "email",
               "sent": "send", "sending": "send", "received": "receive", "receiving": "receive"}


def _content(text):
    from .generation import _STOP_WORDS, _tokens
    generic = {"speaker", "recording", "transcript", "appears", "appear", "says", "said", "say",
               "please", "provide", "request", "records", "record", "clarify", "unidentified",
               "machine", "synthetic", "about", "what", "which", "whether", "never", "not",
               "can", "could", "would", "should", "might", "may", "who", "where", "when", "how"}
    terms = {_WORD_FORMS.get(word, word) for word in _tokens(text)
             if len(word) > 2 and word not in _STOP_WORDS | generic and not word.isdigit()}
    # Passive "was emailed" supports the act of sending. The noun "email"
    # alone does not; quotation matching and polarity remain unchanged.
    if _PASSIVE_EMAILED.search(text):
        terms.add('send')
    return terms


def transcript_drop_reason(text, cited, *, details=None):
    """Keep a labelled speaker's words, negation and conditions with that speaker."""
    from .generation import TRANSCRIPT_EVIDENCE_KIND
    from .question_drafting import unsupported_quotes
    from .question_context import transcript_record_references

    # A whole, source-bound record-existence question makes no speaker claim,
    # even when its source descriptor happens to overlap a speaker's words.
    if transcript_record_references(text, cited):
        return None

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
    if details is not None:
        details.update(requested_speakers=sorted(requested), selected_turns=[item.excerpt for item in selected])
    # A quotation cannot be borrowed from a different speaker in the same passage.
    if bad := unsupported_quotes(text, selected):
        if details is not None:
            details['unsupported_speaker_quotes'] = list(bad)
        return "wrong_transcript_speaker_quotation"
    own_terms = set().union(*(_content(item.excerpt) for item in selected))
    other_terms = set().union(*(_content(item.excerpt) for label, item in turns if label not in requested))
    if details is not None:
        details['conflicting_terms'] = sorted(terms & (other_terms - own_terms))
    if requested and terms & (other_terms - own_terms):
        return "cross_speaker_detail"
    for item in selected:
        for clause in re.split(r"[.!?;]\s+", item.excerpt):
            overlap = terms & _content(clause)
            # The new sending implication must carry the email clause's
            # polarity even in a short question with only that one overlap.
            email_sending = 'send' in overlap and _PASSIVE_EMAILED.search(clause)
            if len(overlap) < 2 and not email_sending:
                continue
            if _NEGATION.search(clause) and not _NEGATION.search(text):
                return "transcript_negation_lost"
            if _CONDITION.search(clause) and not _CONDITION.search(text):
                return "transcript_condition_lost"
    return None
