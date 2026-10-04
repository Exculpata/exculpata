"""Score drafted questions on the fixed synthetic set (assistant actions brief, increment C).

Pass bar, recorded by the maintainer: at least 90% of the questions shown to a
reviewer cite only relevant passages, and none quotes text that is not in its cited
passages. So that dropping questions cannot pass on its own, every case must
also show at least MIN_QUESTIONS_PER_CASE questions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .generation import EvidenceItem, GenerationRejected, GenerationUnavailable
from .question_drafting import PURPOSES, draft_questions, unsupported_quotes

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "benchmarks" / "question-drafts-v1.json"
# The set is fixed: changing it needs a new file and a new recorded fingerprint.
QUESTION_SET_FINGERPRINT = "7674d1da108db0dffe5392a7ca841c483e09e9ec220838a1afac5b5f99a5ea95"
RELEVANT_RATE_BAR = 0.90
MIN_QUESTIONS_PER_CASE = 3


def load_cases(path: Path = DEFAULT_CASES) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if data.get("format") != "recordbench-question-draft-set-v1" or data.get("synthetic_only") is not True:
        raise ValueError("Not a synthetic question-draft set.")
    for case in data["cases"]:
        if case["purpose"] not in PURPOSES or not set(case["relevant"]) <= {p["id"] for p in case["passages"]}:
            raise ValueError(f"Invalid case {case.get('id')!r}.")
    return data, hashlib.sha256(raw).hexdigest()


class _Recording:
    """Wraps the configured client to keep each raw reply for the receipt."""

    def __init__(self, client):
        self.client = client
        self.raw = None

    @property
    def available(self):
        return self.client.available

    def draft_questions(self, **kwargs):
        self.raw = self.client.draft_questions(**kwargs)
        return self.raw


def evaluate(service, data: dict) -> dict:
    """Run every case through the production drafting path and score what a reviewer would see."""
    recording = _Recording(service.client)
    original = service.client
    service.client = recording
    cases, shown_total, relevant_total, shown_bad_quotes, raw_bad_quotes, thin = [], 0, 0, 0, 0, []
    try:
        for case in data["cases"]:
            evidence = tuple(EvidenceItem(p["id"], p["source_name"], p["location"], p["excerpt"],
                                          "transcript" if p["source_name"].endswith(".wav") else "document")
                             for p in case["passages"])
            by_id = {item.evidence_id: item for item in evidence}
            recording.raw = None
            result = dict(id=case["id"], purpose=case["purpose"])
            try:
                draft = draft_questions(service, case["purpose"], case["topic"], evidence)
                shown = draft.questions
                result.update(omitted=draft.omitted, elapsed_ms=draft.elapsed_ms)
            except GenerationRejected as exc:
                shown = ()
                result.update(rejected=str(exc))
            raw_questions = recording.raw.get("questions") if isinstance(recording.raw, dict) else None
            for value in raw_questions if isinstance(raw_questions, list) else ():
                if isinstance(value, dict) and isinstance(value.get("text"), str):
                    ids = value.get("evidence_ids")
                    # A malformed reply (not a list) is scored, never allowed to stop the run.
                    ids = ids if isinstance(ids, (list, tuple)) else ()
                    cited = [by_id[i] for i in ids if isinstance(i, str) and i in by_id]
                    raw_bad_quotes += bool(unsupported_quotes(value["text"], cited or evidence[:0]))
            questions = []
            for question in shown:
                # Relevant only if every cited passage is one the case allows: citing a
                # distractor alongside a relevant passage does not count.
                relevant = set(question.evidence_ids) <= set(case["relevant"])
                bad = unsupported_quotes(question.text, [by_id[i] for i in question.evidence_ids])
                relevant_total += relevant
                shown_bad_quotes += bool(bad)
                questions.append(dict(text=question.text, evidence_ids=list(question.evidence_ids),
                                      relevant=relevant, unsupported_quotes=list(bad)))
            shown_total += len(questions)
            if len(questions) < MIN_QUESTIONS_PER_CASE:
                thin.append(case["id"])
            result.update(questions=questions, raw=recording.raw)
            cases.append(result)
    finally:
        service.client = original
    rate = relevant_total / shown_total if shown_total else 0.0
    return dict(
        shown_questions=shown_total, relevant_questions=relevant_total, relevant_rate=round(rate, 4),
        shown_unsupported_quotes=shown_bad_quotes, raw_unsupported_quotes=raw_bad_quotes,
        cases_below_minimum=thin,
        passed=rate >= RELEVANT_RATE_BAR and shown_bad_quotes == 0 and not thin,
        cases=cases,
    )


def receipt(service, path: Path = DEFAULT_CASES) -> dict:
    data, fingerprint = load_cases(path)
    client = service.client
    result = dict(format="recordbench-question-draft-evaluation-v1", synthetic_only=True,
                  set_fingerprint=fingerprint, pinned_set=fingerprint == QUESTION_SET_FINGERPRINT,
                  case_count=len(data["cases"]),
                  pass_bar=dict(relevant_rate=RELEVANT_RATE_BAR, unsupported_quotes=0,
                                minimum_questions_per_case=MIN_QUESTIONS_PER_CASE),
                  adapter=type(client).__name__, model=getattr(client, "model", None), model_gate="outstanding")
    try:
        available = service.available
    except Exception:
        available = False
    if not available:
        result["limitation"] = ("Configured runtime unavailable. No model evaluation was executed; "
                                "deterministic tests are separate.")
        return result
    try:
        result.update(evaluate(service, data))
    except GenerationUnavailable as exc:
        result["limitation"] = f"The configured runtime failed during evaluation: {exc}"
        return result
    # Only the pinned set can pass the gate recorded in a pull request.
    result["model_gate"] = "passed" if result["passed"] and result["pinned_set"] else "failed"
    return result
