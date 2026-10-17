"""Versioned quantity policy, derived from source text and actual pipeline outcomes.

No model-provided claim of insufficient evidence can grant an exception. Short
output describes what the pipeline produced, not everything a source could support.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import re

TARGET_QUESTIONS = 3
MIN_QUESTIONS_PER_USABLE_CASE = 1
EVALUATION_FORMAT = f"{LEGACY_STEM}-question-draft-evaluation-v2"
ACCEPTANCE_POLICY = {
    "id": "grounded-question-target-v2",
    "supersedes": f"{LEGACY_STEM}-question-draft-evaluation-v1",
    "decision": "https://github.com/Exculpata/exculpata/pull/142#issuecomment-6154342607",
    "rationale": "Three is a target, not a quota: retain useful supported short output without filler.",
    "empty_source_rule": "Only absent or whitespace-only excerpt text; never inferred from rejected or empty output.",
    "failure_rule": "Usable sources need at least one grounded question and completed drafting/judging calls.",
    "historical_results": "Earlier v1 receipts retain their original failed result and three-question minimum.",
}


def has_usable_content(evidence):
    """A deliberately narrow empty-source exception, not an OCR/meaning heuristic.

Uncertain text, numbers, punctuation and transcript placeholders may be useful.
They do not qualify merely because a model could not draft from them.
"""
    return any(item.excerpt.strip() for item in evidence)


def _invalid_candidate_schema(value):
    """Check raw protocol fields before normalization; do not relax source checks."""
    from .question_drafting import QUESTION_SCHEMA

    fields = QUESTION_SCHEMA["properties"]["questions"]["items"]["properties"]
    if not isinstance(value, dict) or set(value) != set(fields):
        return True
    text, identifiers = value["text"], value["evidence_ids"]
    text_rule, ids_rule = fields["text"], fields["evidence_ids"]
    return (not isinstance(text, str)
            or not text_rule["minLength"] <= len(text) <= text_rule["maxLength"]
            or not isinstance(identifiers, list)
            or not ids_rule["minItems"] <= len(identifiers) <= ids_rule["maxItems"]
            or any(not isinstance(identifier, str)
                   or re.fullmatch(ids_rule["items"]["pattern"], identifier) is None
                   for identifier in identifiers))


def question_outcome(raw, evidence, *, failure=None):
    """Classify useful output independently of a count target and preserve failures."""
    from .question_grounding import QuestionDraftReceipt, audit_attempts

    usable = bool(has_usable_content(evidence))
    if not usable:
        return dict(status="no_usable_content", useful_output=False, usable_source=False,
                    target_questions=TARGET_QUESTIONS, shown_questions=0, generation_complete=True,
                    problems=[], notice="These passages contain no usable source text, so no questions were drafted.")
    envelope = isinstance(raw, QuestionDraftReceipt)
    attempts = raw.attempts if envelope else (raw,)
    # Audit normalization may retain a grounded question from a schema-invalid
    # raw candidate. Preserve that existing display behavior, but never call the
    # malformed protocol a completed generation or a successful evaluation.
    invalid_candidate = any(
        _invalid_candidate_schema(value)
        for attempt in attempts if isinstance(attempt, dict)
        for value in (attempt.get("questions") if isinstance(attempt.get("questions"), list) else ()))
    audit = audit_attempts(attempts, evidence, raw.judge_drops if envelope else None)
    count = len(audit.questions)
    problems = {drop["reason"] for drop in audit.drops
                if drop["reason"] in {"invalid_attempt_shape", "invalid_question_shape",
                                      "judge_unavailable", "judge_invalid_decisions"}}
    if invalid_candidate:
        problems.add("invalid_question_shape")
    if failure:
        problems.add(failure)
    for call in raw.get("calls", ()) if envelope else ():
        if call.get("stage") == "planning":
            continue  # Planning is optional; its failure cannot spend the final answer reserve.
        if call.get("error"):
            problems.add(call["error"])
        if call.get("finish_reason") not in (None, "stop", "eos"):
            problems.add("incomplete_model_response")
    if problems:
        status = "generation_incomplete" if count else "generation_failed"
        notice = ("Drafting did not complete successfully. Some model responses failed or were incomplete. "
                  "The grounded questions shown can be reviewed; this is not evidence that the source supports no more."
                  if count else "Drafting failed or returned an invalid response. No questions are shown. Try again.")
    elif not count:
        filtered = bool(audit.drops)
        status = "no_grounded_output" if filtered else "empty_generation"
        notice = ("No drafted question passed the citation and text checks. This does not mean the source lacks usable content."
                  if filtered else "The answer service returned no questions from these usable passages. Try again or review the sources.")
    elif count < TARGET_QUESTIONS:
        amount = "one grounded question was" if count == 1 else "two grounded questions were"
        if any(drop["reason"] != "near_duplicate" for drop in audit.drops):
            status = "filtered_short_output"
            notice = f"Only {amount} retained after source checks; other drafts were omitted."
        elif audit.drops:
            status = "deduplicated_short_output"
            notice = f"Only {amount} produced; repeated drafts were removed."
        else:
            status = "supported_short_output"
            notice = f"Only {amount} produced from these passages. No filler was added to reach the target of three."
    else:
        status, notice = "grounded_output", ""
    return dict(status=status, useful_output=count > 0, usable_source=True,
                target_questions=TARGET_QUESTIONS, shown_questions=count,
                generation_complete=not problems, problems=sorted(problems), notice=notice)
