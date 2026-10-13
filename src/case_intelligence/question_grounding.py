"""Bounded question audits; retain raw attempts independently of displayed questions.

These conservative text checks catch specific constructions, not all entailment
errors. An optional model judge can only reject more candidates, never override
the deterministic checks. Neither check invents or rewrites a question.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
import re


class QuestionDraftReceipt(dict):
    """An internal envelope that JSON returned by a model cannot instantiate.

    ``questions`` exposes every original candidate to the unchanged scorer.
    Each attempt still has the eight-question schema limit; the display limit
    remains eight. Extra audit fields never come from model-declared authority.
    """

    def __init__(self, attempts, *, profile, calls, judge_drops=()):
        self.attempts = copy.deepcopy(tuple(attempts))
        self.judge_drops = dict(judge_drops)
        all_questions, boundaries = [], []
        for number, attempt in enumerate(self.attempts, 1):
            start = len(all_questions)
            values = attempt.get("questions") if isinstance(attempt, dict) else None
            if isinstance(values, list):
                all_questions.extend(copy.deepcopy(values))
            boundary = {"attempt": number, "start": start, "end": len(all_questions)}
            if not isinstance(attempt, dict) or set(attempt) != {"questions"} or not isinstance(values, list):
                boundary["invalid_raw"] = copy.deepcopy(attempt)
            boundaries.append(boundary)
        super().__init__(questions=all_questions, attempts=boundaries,
                         profile=copy.deepcopy(profile), calls=copy.deepcopy(calls), drops=[])

    def review(self, evidence):
        from .question_context import retention_summary
        result = audit_attempts(self.attempts, evidence, self.judge_drops)
        self["drops"] = copy.deepcopy(result.drops)
        self["retained_candidate_ids"] = list(result.candidate_ids)
        self["retention"] = retention_summary(self.attempts, result)
        return result


@dataclass(frozen=True)
class QuestionAudit:
    questions: tuple
    candidate_ids: tuple[str, ...]
    drops: list[dict]
    omitted: int


_SAFE_CAPITALS = {"what", "when", "where", "why", "who", "which", "how", "please", "request",
                  "provide", "could", "can", "did", "does", "do", "is", "are", "was", "were",
                  "the", "a", "an", "according", "speaker", "recording", "transcript"}
_ROLES = {"owner", "witness", "interviewee", "defendant", "suspect", "driver", "sender", "recipient"}
_CONDITIONAL = re.compile(r"\b(?:if|whether|might|may|could|would|uncertain|unresolved)\b", re.I)
_NEGATIVE = re.compile(r"\b(?:not|never|cannot|can't|didn't|doesn't|couldn't|hasn't|hadn't)\b", re.I)
_NAME = r"[A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,3}"
_ACTION_FORMS = {
    "had": r"(?:had|has|have|possess(?:ed|es)?)", "has": r"(?:had|has|have|possess(?:ed|es)?)",
    "owned": r"(?:owned|owns|own)", "carried": r"(?:carried|carries|carrying|carry)",
    "left": r"(?:left|leaving|leaves|leave)", "delivered": r"(?:delivered|delivers|delivering|deliver)",
    "received": r"(?:received|receives|receiving|receive)", "took": r"(?:took|takes|taking|take)",
}
_ASSERTED_OBJECT = re.compile(
    rf"\b(?P<object>[a-zA-Z][\w-]*)\s+(?:that\s+)?(?P<name>{_NAME})\s+"
    r"(?P<actions>(?:had|has|owned|carried|left|delivered|received|took)"
    r"(?:\s+(?:or|and)\s+(?:had|has|owned|carried|left|delivered|received|took))*)\b")


def _clauses(text):
    return re.split(r"[.!?;]\s+", " ".join(text.split()))


def _asserted_action_supported(name, action, object_word, excerpts):
    pattern = re.compile(r"\b" + re.escape(name) + r"\b[^.!?;]{0,100}?\b"
                         + _ACTION_FORMS[action] + r"\b(?P<between>(?:\s+[\w'-]+){0,5})\s+"
                         + re.escape(object_word) + r"s?\b", re.I)
    for excerpt in excerpts:
        for clause in _clauses(excerpt):
            if _CONDITIONAL.search(clause) or _NEGATIVE.search(clause):
                continue
            if action in {"owned", "had", "has"}:
                possession = (r"\b" + re.escape(object_word) + r"s?\s+belong(?:ed|s)? to\s+"
                              + re.escape(name) + r"\b")
                possessive = r"\b" + re.escape(name) + r"['’]s\s+" + re.escape(object_word) + r"s?\b"
                if re.search(possession, clause, re.I) or re.search(possessive, clause, re.I):
                    return True
            for match in pattern.finditer(clause):
                between = set(match["between"].casefold().split())
                if not between & {"at", "from", "to", "after", "before", "with", "carrying", "taking"}:
                    return True
    return False


def semantic_drop_reason(question, evidence):
    """Reject detectable assumptions without claiming a complete language proof."""
    from .question_language import (transcript_drop_reason, unstated_plural_pronouns,
                                    without_source_references)
    from .question_context import cross_locator_drop_reason
    text = question.text
    cited = [evidence[identifier] for identifier in question.evidence_ids]
    locator_reason = cross_locator_drop_reason(text, cited)
    if locator_reason:
        return locator_reason
    excerpts = [item.excerpt for item in cited]
    source = " ".join(excerpts).casefold()
    words = set(re.findall(r"\b\w+\b", source))
    if unstated_plural_pronouns(text, words):
        return "unstated_personal_pronoun"
    # Exact cited names/locators may identify the requested source, but their
    # words do not become facts elsewhere in the question. Citation verification
    # has already checked quotations and numbers against excerpts only.
    for token in re.findall(r"\b[A-Z][A-Za-z'-]+\b", without_source_references(text, cited)):
        if re.sub(r"['’]s$", "", token.casefold()) not in words | _SAFE_CAPITALS:
            return "unstated_identity_or_named_detail"
    for role in _ROLES:
        if re.search(r"\b(?:the|this|that) " + role + r"\b", text, re.I) and role not in words:
            return "unestablished_role"
    if (re.search(r"\b(?:what|when|where|why|how) did you (?:observe|see|carry|leave|deliver|receive)\b", text, re.I)
            and not re.search(r"\b(?:if anything|if you|whether you)\b", text, re.I)):
        return "unestablished_interviewee_participation"
    for match in _ASSERTED_OBJECT.finditer(text):
        if match["object"].casefold() in {"did", "does", "can", "could", "would", "if", "whether", "that", "when",
                                                   "showing", "proving", "establishing", "confirming", "said"}:
            continue
        for action in re.split(r"\s+(?:or|and)\s+", match["actions"]):
            if not _asserted_action_supported(match["name"], action, match["object"], excerpts):
                return "unsupported_possession_or_action"
    for match in re.finditer(rf"\b(?P<name>{_NAME})['’]s\s+(?P<object>[A-Za-z][\w-]*)", text):
        if not any(_asserted_action_supported(match["name"], action, match["object"], excerpts)
                   for action in ("owned", "had")):
            return "unsupported_possession"
    # A relation between two named people cannot be borrowed from separate
    # clauses that describe those people in different roles.
    relation = re.compile(rf"\b(?:comparison|meeting|conversation|agreement) between (?P<a>{_NAME}) and (?P<b>{_NAME})\b")
    for match in relation.finditer(text):
        if not any(match["a"].casefold() in clause.casefold() and match["b"].casefold() in clause.casefold()
                   for excerpt in excerpts for clause in _clauses(excerpt)):
            return "unsupported_named_relation"
    uncertain = re.compile(r"\b(?:did not|didn't|does not|cannot|could not|has not) "
                           r"(?:claim|establish|confirm|prove|verify|identify|observe|see)\b(?P<scope>[^.!?;]*)", re.I)
    asserting = re.search(r"\b(?:claimed|confirmed|established|proved|proven|showing|proving|establishing)\b", text, re.I)
    preserves = (re.search(r"\b(?:if|whether|uncertain|uncertainty|unresolved)\b", text, re.I)
                 or uncertain.search(text))
    if asserting and not preserves:
        question_terms = set(re.findall(r"\b\w{4,}\b", text.casefold()))
        for excerpt in excerpts:
            for match in uncertain.finditer(excerpt):
                scope = set(re.findall(r"\b\w{4,}\b", match["scope"].casefold())) - {"that", "same", "this", "with", "from"}
                if len(scope & question_terms) >= 2:
                    return "uncertainty_changed_to_assertion"
    return transcript_drop_reason(text, cited)


def near_duplicate(left, right):
    from .question_drafting import _near_duplicate, question_key, quoted_spans
    a, b = question_key(left.text).split(), question_key(right.text).split()
    def protected(text):
        return {token for token in re.findall(r"\b[A-Z][A-Za-z'-]+\b", text)
                if token.casefold() not in _SAFE_CAPITALS}
    aliases = ({"regarding": "about", "concerning": "about", "verify": "check"}
               if protected(left.text) == protected(right.text)
               and quoted_spans(left.text) == quoted_spans(right.text) else {})
    a, b = [[aliases.get(word, word) for word in row] for row in (a, b)]
    if a == b:
        return True
    # "recording of the camera" and "camera recording" request the same
    # material. Restrict the reorder to this noun phrase; do not sort tokens,
    # which would conflate sender/recipient, negation and other role reversals.
    def record_wording(text):
        return re.sub(r"\brecording of (?:the )?(.+? camera)(?= assigned\b| for\b| from\b|$)",
                      r"\1 recording", text)
    if record_wording(" ".join(a)) == record_wording(" ".join(b)):
        return True
    if _near_duplicate(left, replace(right, evidence_ids=left.evidence_ids)):
        return True
    if a[:3] != b[:3]:
        return False
    comparison = SequenceMatcher(a=a, b=b, autojunk=False)
    changed = {word for op, i, j, k, l in comparison.get_opcodes() if op != "equal"
               for word in (*a[i:j], *b[k:l])}
    return comparison.ratio() >= 0.9 and changed <= {"a", "an", "the", "exactly", "specifically", "in", "particular"}


def citation_drop_reason(value, evidence):
    from .question_drafting import (_digit_tokens, _EVIDENCE_MARKER, _NUMBER, unsupported_quotes)
    from .question_context import without_locator_references
    if not isinstance(value, dict) or set(value) != {"text", "evidence_ids"}:
        return "invalid_question_shape"
    text, identifiers = value["text"], value["evidence_ids"]
    if not isinstance(text, str) or not isinstance(identifiers, (list, tuple)):
        return "invalid_question_shape"
    if any(not isinstance(identifier, str) or identifier not in evidence for identifier in identifiers):
        return "unknown_citation"
    cited = [evidence[identifier] for identifier in identifiers]
    if unsupported_quotes(text, cited):
        return "unsupported_quotation"
    if _EVIDENCE_MARKER.search(text):
        return "citation_label_in_text"
    source = " ".join(item.excerpt for item in cited).casefold()
    factual_text = without_locator_references(text, cited).casefold()
    if (_digit_tokens(factual_text) - _digit_tokens(source)
            or set(_NUMBER.findall(factual_text)) - set(_NUMBER.findall(source))):
        return "uncited_digit_bearing_detail"
    if set(re.findall(r"\b(?:he|him|his|she|her|hers)\b", text.casefold())) - set(source.split()):
        return "unstated_gendered_pronoun"
    return "citation_or_text_verification_failed"


def audit_attempts(attempts, evidence, judge_drops=None):
    from .question_drafting import MAX_DRAFTED_QUESTIONS, verify_question
    evidence_map = {item.evidence_id: item for item in evidence}
    accepted, identifiers, drops = [], [], []
    judge_drops = judge_drops or {}
    for attempt_index, raw in enumerate(attempts):
        values = raw.get("questions") if isinstance(raw, dict) else None
        valid_shape = (isinstance(raw, dict) and set(raw) == {"questions"}
                       and isinstance(values, list) and len(values) <= MAX_DRAFTED_QUESTIONS)
        if not valid_shape:
            drops.append({"attempt": attempt_index + 1, "candidate_id": None,
                          "reason": "invalid_attempt_shape", "raw": copy.deepcopy(raw)})
            continue
        for index, value in enumerate(values):
            candidate_id = f"a{attempt_index + 1}q{index + 1}"
            question = (verify_question(value.get("text"), value.get("evidence_ids"), evidence_map)
                        if isinstance(value, dict) and set(value) == {"text", "evidence_ids"} else None)
            reason = citation_drop_reason(value, evidence_map) if question is None else None
            if reason is None and any(near_duplicate(question, earlier) for earlier in accepted):
                reason = "near_duplicate"
            if reason is None:
                reason = semantic_drop_reason(question, evidence_map)
            if reason is None and candidate_id in judge_drops:
                reason = judge_drops[candidate_id]
            if reason is None and len(accepted) >= MAX_DRAFTED_QUESTIONS:
                reason = "display_limit"
            if reason is not None:
                drops.append({"attempt": attempt_index + 1, "candidate_id": candidate_id,
                              "reason": reason, "raw": copy.deepcopy(value)})
            else:
                accepted.append(question)
                identifiers.append(candidate_id)
    # Keep the existing omission-notice meaning for duplicates; the audit still
    # records every duplicate and every other discarded candidate individually.
    omitted = sum(row["reason"] != "near_duplicate" for row in drops)
    return QuestionAudit(tuple(accepted), tuple(identifiers), drops, omitted)
