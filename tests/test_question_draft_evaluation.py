"""Synthetic regression: the fixed question-draft set and its scoring."""
import json
import subprocess
import sys
from pathlib import Path

from case_intelligence.generation import GroundedGenerationService, UnavailableGenerator
from case_intelligence.question_draft_evaluation import (
    DEFAULT_CASES, MIN_QUESTIONS_PER_CASE, QUESTION_SET_FINGERPRINT, evaluate, load_cases, receipt,
)

ROOT = Path(__file__).resolve().parents[1]


class Scripted:
    """Drafts from the passages it is given, citing them as instructed."""
    def __init__(self, choose, count=3, quote=None):
        self.choose, self.count, self.quote = choose, count, quote

    @property
    def available(self):
        return True

    def draft_questions(self, *, purpose, topic, evidence):
        questions = []
        for index in range(self.count):
            item = self.choose(evidence, index)
            words = " ".join(item.excerpt.split()[:6]).strip(".,:;\"")
            text = f"{index + 1}. {('Who', 'What', 'Where')[index]} can explain the record that says {words}?"
            if self.quote:
                text = f'{("Why", "How", "When")[index]} does it say "{self.quote}" about {words}?'
            questions.append({"text": text, "evidence_ids": [item.evidence_id]})
        return {"questions": questions}


def run(client):
    data, _ = load_cases()
    return evaluate(GroundedGenerationService(client), data)


def test_the_set_is_synthetic_fixed_and_mixes_relevant_and_unrelated_passages():
    data, fingerprint = load_cases()
    assert len(data["cases"]) == 10 and fingerprint == QUESTION_SET_FINGERPRINT
    assert {case["purpose"] for case in data["cases"]} == {"witness", "discovery"}
    for case in data["cases"]:
        ids = {passage["id"] for passage in case["passages"]}
        assert set(case["relevant"]) < ids, case["id"]  # every case has a distractor


def test_relevant_grounded_drafts_pass_the_bar():
    result = run(Scripted(lambda evidence, index: evidence[index % 2]))
    assert result["passed"] is True
    assert result["shown_questions"] == 10 * MIN_QUESTIONS_PER_CASE and result["relevant_rate"] == 1.0
    assert result["shown_unsupported_quotes"] == result["raw_unsupported_quotes"] == 0


def test_drafts_citing_unrelated_passages_quoting_absent_text_or_too_few_fail():
    unrelated = run(Scripted(lambda evidence, index: evidence[2]))
    assert unrelated["passed"] is False and unrelated["relevant_rate"] == 0.0
    # Invented quotations never reach the reviewer, so every case falls below the minimum.
    quoting = run(Scripted(lambda evidence, index: evidence[0], quote="it was already open"))
    assert quoting["passed"] is False and quoting["shown_unsupported_quotes"] == 0
    assert quoting["raw_unsupported_quotes"] == 30 and len(quoting["cases_below_minimum"]) == 10
    thin = run(Scripted(lambda evidence, index: evidence[0], count=2))
    assert thin["passed"] is False and thin["relevant_rate"] == 1.0 and len(thin["cases_below_minimum"]) == 10


def test_without_a_runtime_the_model_gate_stays_outstanding():
    result = receipt(GroundedGenerationService(UnavailableGenerator()))
    assert result["model_gate"] == "outstanding" and "No model evaluation was executed" in result["limitation"]
    completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "evaluate-question-drafts.py")],
                               capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, check=True)
    assert json.loads(completed.stdout)["model_gate"] == "outstanding"
    assert DEFAULT_CASES.is_file()
