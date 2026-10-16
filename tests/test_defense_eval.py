"""GPU-free checks for the defense-workflow harness, including CI stub mode."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from case_intelligence.defense_eval import (
    DEFAULT_SUITE, Citation, DefenseEvalError, Fact, ModelAnswer, Passage, Question,
    ReferenceStub, Support, load_suite, main, rank_passages, render_markdown, run_eval,
    score_question, suite_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def suite():
    return load_suite()


def test_versioned_suite_pins_bytes_and_every_question_type(suite):
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    assert payload["synthetic_only"] is True
    assert payload["fingerprint"] == suite.fingerprint == suite_fingerprint(payload)
    assert {question.type for question in suite.questions} == {
        "fact_lookup", "chronology", "contradiction", "impeachment",
        "media_transcript", "not_in_record",
    }
    transcript = suite.passage("hs-transcript-041")
    assert transcript.evidence_kind == "transcript"
    assert transcript.matter_id == "harbor-street"
    assert suite.passage("ly-note-001").matter_id == "lumen-yard"
    assert all(question.matter_id == "harbor-street" for question in suite.questions)


def test_stub_mode_scores_every_reference_question(suite, tmp_path):
    scorecard = run_eval(suite, ReferenceStub(), model={"mode": "stub", "backend": "reference-stub", "model": "reference-stub"})
    totals = scorecard["aggregates"]
    assert totals["questions"] == 11
    assert totals["answer_correct"] == totals["citation_support"] == totals["abstention_correct"] == 11
    assert totals["retrieval_hit"] == totals["retrieval_applicable"] == 9
    assert scorecard["measurement_only"] is True
    assert scorecard["release_threshold"] is None
    assert scorecard["llm_judge"] == {
        "enabled": False,
        "label": "optional_llm_judge",
        "included_in_deterministic_totals": False,
    }
    assert all(row["latency_ms"] >= 0 for row in scorecard["questions"])
    rendered = render_markdown(scorecard)
    assert "not a model quality result" in rendered
    assert "Not run." in rendered
    assert "ly-note-001" not in rendered
    for row in scorecard["questions"]:
        assert "ly-note-001" not in row["retrieved_passage_ids"]
    text = json.dumps(scorecard)
    assert "example.test" not in text
    assert "http://" not in text
    assert str(ROOT) not in text
    code = main(["--mode", "stub", "--output-dir", str(tmp_path)])
    assert code == 0
    written = json.loads((tmp_path / "scorecard.json").read_text(encoding="utf-8"))
    assert written["aggregates"]["answer_correct"] == 11
    assert "Defense workflow scorecard" in (tmp_path / "scorecard.md").read_text(encoding="utf-8")


def test_cli_refuses_to_overwrite_a_scorecard(tmp_path):
    assert main(["--mode", "stub", "--output-dir", str(tmp_path)]) == 0
    assert main(["--mode", "stub", "--output-dir", str(tmp_path)]) == 2
    assert main(["--mode", "stub", "--output-dir", str(tmp_path), "--force"]) == 0


def test_module_cli_stub_mode(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "case_intelligence.defense_eval", "--mode", "stub",
         "--output-dir", str(tmp_path)],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "answer 11/11" in result.stdout
    assert (tmp_path / "scorecard.json").is_file()
    assert (tmp_path / "scorecard.md").is_file()


def test_retrieval_miss_changes_the_score(suite):
    scorecard = run_eval(suite, ReferenceStub(), retriever=lambda passages, query, top_k: ())
    assert scorecard["aggregates"]["retrieval_hit"] == 0
    assert scorecard["aggregates"]["answer_correct"] == 2
    factual = [row for row in scorecard["questions"] if row["type"] != "not_in_record"]
    assert factual and all(row["answer_correct"] is False for row in factual)
    assert all(row["abstention_correct"] is True for row in scorecard["questions"] if row["type"] == "not_in_record")


def test_ranker_stays_inside_the_question_matter(suite):
    harbor = tuple(passage for passage in suite.passages if passage.matter_id == "harbor-street")
    mixed = harbor + (suite.passage("ly-note-001"),)
    ranked = rank_passages(mixed, "amber ribbon parcel at Lumen Yard", 8)
    assert ranked[0].passage_id == "ly-note-001"
    harbor_only = rank_passages(harbor, "amber ribbon parcel at Lumen Yard", 8)
    assert all(passage.matter_id == "harbor-street" for passage in harbor_only)


def test_citation_support_requires_the_cited_span(suite):
    question = next(item for item in suite.questions if item.question_id == "dw-opening-count")
    passage = suite.passage("hs-report-01")
    other = suite.passage("hs-report-02")
    quoted = ModelAnswer(False, "twelve", (Citation(passage.passage_id, "counted twelve teal lantern cases"),))
    paraphrased = ModelAnswer(False, "twelve", (Citation(passage.passage_id, "a number that is not written here"),))
    wrong_record = ModelAnswer(False, "twelve", (Citation(other.passage_id, "planned to carry the teal lantern parcel"),))
    passages = {passage.passage_id: passage, other.passage_id: other}
    assert score_question(question, quoted, (passage,), passages, 1)["citation_support"] is True
    assert score_question(question, paraphrased, (passage,), passages, 1)["citation_support"] is False
    assert score_question(question, wrong_record, (passage, other), passages, 1)["citation_support"] is False
    assert score_question(question, wrong_record, (passage, other), passages, 1)["answer_correct"] is True


def test_abstention_and_forbidden_facts_score_separately():
    passage = Passage(
        "p", "harbor-street", "p.txt", "0" * 64, "document", "DEMO-HS-001",
        "counted twelve teal lantern cases",
    )
    question = Question(
        "q", "harbor-street", "fact_lookup", "How many cases were counted?", False,
        (Fact("twelve", "twelve", (Support("p", "counted twelve teal lantern cases"),)),),
        ("eleven",),
    )
    invented = ModelAnswer(False, "twelve and then eleven", (Citation("p", "counted twelve teal lantern cases"),))
    row = score_question(question, invented, (passage,), {passage.passage_id: passage}, 4)
    assert row["citation_support"] is True
    assert row["abstention_correct"] is True
    assert row["answer_correct"] is False
    assert row["latency_ms"] == 4
    missing = Question(
        "missing", "harbor-street", "not_in_record", "What color was the grip?", True, (), (),
    )
    answered = ModelAnswer(False, "blue", (Citation("p", "counted twelve teal lantern cases"),))
    withheld = ModelAnswer(True, "Not in the record.", ())
    passages = {passage.passage_id: passage}
    assert score_question(missing, answered, (passage,), passages, 0)["abstention_correct"] is False
    assert score_question(missing, withheld, (passage,), passages, 0)["citation_support"] is True


def test_optional_judge_does_not_change_deterministic_totals(suite):
    def judge(question, answer):
        return {"correct": False, "rationale": "synthetic dissent"}

    scorecard = run_eval(suite, ReferenceStub(), judge=judge)
    assert scorecard["aggregates"]["answer_correct"] == 11
    assert scorecard["llm_judge"]["enabled"] is True
    assert scorecard["llm_judge"]["included_in_deterministic_totals"] is False
    assert all(row["llm_judge"]["included_in_deterministic_totals"] is False for row in scorecard["questions"])
    assert all(row["llm_judge"]["correct"] is False for row in scorecard["questions"])
    rendered = render_markdown(scorecard)
    assert "excluded from the deterministic totals" in rendered
    assert "optional_llm_judge" in rendered


def test_suite_rejects_fingerprint_hash_and_support_edits(tmp_path):
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    payload["description"] += " edited"
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="fingerprint"):
        load_suite(path)
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    payload["passages"][0]["sha256"] = "0" * 64
    payload["fingerprint"] = suite_fingerprint(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="bytes do not match"):
        load_suite(path)
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    payload["questions"][0]["required_facts"][0]["supports"][0]["support_text"] = "not written in that record"
    payload["fingerprint"] = suite_fingerprint(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="not in the named passage"):
        load_suite(path)


def test_configured_mode_stops_without_a_generator(monkeypatch, tmp_path):
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", raising=False)

    class Unavailable:
        available = False

    monkeypatch.setattr("case_intelligence.defense_eval.generator_from_environment", lambda: Unavailable())
    code = main(["--mode", "configured", "--output-dir", str(tmp_path), "--profile", "portable"])
    assert code == 2
    assert not (tmp_path / "scorecard.json").exists()


def test_judge_and_profile_flags_require_configured_mode(tmp_path):
    with pytest.raises(SystemExit) as judge_exit:
        main(["--mode", "stub", "--judge", "--output-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as profile_exit:
        main(["--mode", "stub", "--profile", "portable", "--output-dir", str(tmp_path)])
    assert judge_exit.value.code == 2
    assert profile_exit.value.code == 2


def test_declared_profile_records_the_pinned_catalog_entry(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "Qwen/Qwen3.5-4B")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "http://generator.example.test")
    from case_intelligence.defense_eval import _model_record
    record = _model_record("configured", "portable")
    assert record["backend"] == "openai"
    assert record["model"] == "Qwen/Qwen3.5-4B"
    assert record["profile"]["revision"] == "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a"
    assert record["profile"]["license"] == "Apache-2.0"
    assert "example.test" not in json.dumps(record)
    assert "http" not in json.dumps(record)
