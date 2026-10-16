"""GPU-free checks for the defense-workflow harness, including CI stub mode."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from case_intelligence.defense_eval import (
    DEFAULT_SUITE, Citation, DefenseEvalError, Fact, ModelAnswer, Passage, PostgresHybridRetriever,
    Question, ReferenceStub, Support, _healthy_retrieval_worker, hybrid_adapters, load_suite, main,
    rank_passages, render_markdown, run_eval, score_question, suite_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def suite():
    return load_suite()


def test_versioned_suite_pins_bytes_and_every_question_type(suite):
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    assert payload["suite_version"] == "2"
    assert payload["synthetic_only"] is True
    assert payload["fingerprint"] == suite.fingerprint == suite_fingerprint(payload)
    assert len(suite.questions) >= 40
    counts = {}
    for question in suite.questions:
        counts[question.type] = counts.get(question.type, 0) + 1
        assert question.near_misses
    assert counts == {
        "fact_lookup": 10, "chronology": 6, "contradiction": 6,
        "impeachment": 7, "media_transcript": 5, "not_in_record": 8,
    }
    assert {question.matter_id for question in suite.questions} == {"harbor-street", "lumen-yard"}
    assert sum(question.matter_id == "lumen-yard" for question in suite.questions) >= 10
    transcript = suite.passage("hs-transcript-041")
    assert transcript.evidence_kind == "transcript"
    assert transcript.matter_id == "harbor-street"
    assert suite.passage("ly-transcript").matter_id == "lumen-yard"
    assert suite.passage("ly-transcript").evidence_kind == "transcript"


def test_version_one_suite_still_loads():
    suite = load_suite(ROOT / "benchmarks" / "defense-workflow" / "v1" / "suite.json")
    assert suite.suite_version == "1"
    assert len(suite.questions) == 11
    assert suite.fingerprint == "56b0f4e27b2bb3222239f63fe3267947b2ecb8b94cf2fed505ca69e3873b8768"
    assert all(question.matter_id == "harbor-street" for question in suite.questions)
    assert all(question.near_misses == () for question in suite.questions)


def test_stub_mode_scores_every_reference_question(suite, tmp_path):
    scorecard = run_eval(suite, ReferenceStub(), model={"mode": "stub", "backend": "reference-stub", "model": "reference-stub"})
    totals = scorecard["aggregates"]
    assert totals["questions"] == 42
    assert totals["answer_correct"] == totals["citation_support"] == totals["abstention_correct"] == 42
    assert totals["retrieval_hit"] == totals["retrieval_applicable"] == 34
    assert scorecard["retriever"] == {
        "name": "lexical",
        "index": "in-process",
        "embedder": None,
        "reranker": None,
        "learned_thresholds": False,
    }
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
    assert "Retriever: `lexical`" in rendered
    assert "Not run." in rendered
    for row in scorecard["questions"]:
        prefix = "ly-" if row["matter_id"] == "harbor-street" else "hs-"
        assert all(not passage_id.startswith(prefix) for passage_id in row["retrieved_passage_ids"])
    text = json.dumps(scorecard)
    assert "example.test" not in text
    assert "http://" not in text
    assert str(ROOT) not in text
    code = main(["--mode", "stub", "--output-dir", str(tmp_path)])
    assert code == 0
    written = json.loads((tmp_path / "scorecard.json").read_text(encoding="utf-8"))
    assert written["aggregates"]["answer_correct"] == 42
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
    assert "answer 42/42" in result.stdout
    assert (tmp_path / "scorecard.json").is_file()
    assert (tmp_path / "scorecard.md").is_file()


def test_retrieval_miss_changes_the_score(suite):
    scorecard = run_eval(suite, ReferenceStub(), retriever=lambda passages, query, top_k: ())
    assert scorecard["aggregates"]["retrieval_hit"] == 0
    assert scorecard["aggregates"]["answer_correct"] == 8
    factual = [row for row in scorecard["questions"] if row["type"] != "not_in_record"]
    assert factual and all(row["answer_correct"] is False for row in factual)
    assert all(row["abstention_correct"] is True for row in scorecard["questions"] if row["type"] == "not_in_record")


def test_ranker_stays_inside_the_question_matter(suite):
    harbor = tuple(passage for passage in suite.passages if passage.matter_id == "harbor-street")
    ribbon = suite.passage("ly-near-ribbon")
    ranked = rank_passages(harbor + (ribbon,), "green ribbon sample on the Copper Annex bench", 8)
    assert ranked[0].passage_id == "ly-near-ribbon"
    harbor_only = rank_passages(harbor, "green ribbon sample on the Copper Annex bench", 8)
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
    assert scorecard["aggregates"]["answer_correct"] == 42
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


def test_citing_a_near_miss_fails_citation_support(suite):
    question = next(item for item in suite.questions if item.question_id == "dw-opening-count")
    near = suite.passage("hs-report-08")
    gold = suite.passage("hs-report-01")
    assert near.passage_id in question.near_misses
    quoted = ModelAnswer(False, "twelve", (Citation(near.passage_id, "counted eleven teal cases"),))
    row = score_question(
        question, quoted, (gold, near),
        {gold.passage_id: gold, near.passage_id: near}, 1,
    )
    assert row["answer_correct"] is True
    assert row["citation_support"] is False
    assert near.passage_id in row["unsupported_citations"]


def test_near_miss_cannot_be_the_reference_passage(tmp_path):
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    question = next(item for item in payload["questions"] if item["question_id"] == "dw-opening-count")
    question["near_misses"] = [question["required_facts"][0]["supports"][0]["passage_id"]]
    payload["fingerprint"] = suite_fingerprint(payload)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="competing passage"):
        load_suite(path)
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    question = next(item for item in payload["questions"] if item["question_id"] == "dw-opening-count")
    question["required_facts"][0]["answer_text"] = "teal"
    question["required_facts"][0]["supports"][0]["support_text"] = "teal"
    payload["fingerprint"] = suite_fingerprint(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="contains a reference span"):
        load_suite(path)


def test_compare_prints_per_category_deltas(tmp_path, capsys):
    left = tmp_path / "a.json"
    right = tmp_path / "b.json"
    left.write_text(json.dumps({
        "fingerprint": "same",
        "suite_version": "2",
        "questions": [
            {"question_id": "q1", "type": "fact_lookup", "answer_correct": True,
             "citation_support": True, "abstention_correct": True, "retrieval_hit": True},
            {"question_id": "q2", "type": "contradiction", "answer_correct": True,
             "citation_support": False, "abstention_correct": True, "retrieval_hit": True},
        ],
    }), encoding="utf-8")
    right.write_text(json.dumps({
        "fingerprint": "other",
        "suite_version": "2",
        "questions": [
            {"question_id": "q1", "type": "fact_lookup", "answer_correct": False,
             "citation_support": True, "abstention_correct": True, "retrieval_hit": True},
            {"question_id": "q2", "type": "contradiction", "answer_correct": True,
             "citation_support": True, "abstention_correct": True, "retrieval_hit": False},
            {"question_id": "q3", "type": "fact_lookup", "answer_correct": True,
             "citation_support": True, "abstention_correct": True, "retrieval_hit": True},
        ],
    }), encoding="utf-8")
    assert main(["--compare", str(left), str(right)]) == 0
    printed = capsys.readouterr().out
    assert "Fingerprints differ" in printed
    assert "| fact_lookup | 1/1 -> 0/1 (-1) |" in printed
    assert "| contradiction | 1/1 -> 1/1 (+0) | 0/1 -> 1/1 (+1) |" in printed
    assert "`q1`: Answer True -> False" in printed
    assert "`q3`" in printed
    assert main(["--compare", str(tmp_path / "missing.json"), str(right)]) == 2
    with pytest.raises(SystemExit) as compare_mode:
        main(["--compare", str(left), str(right), "--mode", "stub"])
    assert compare_mode.value.code == 2


def test_postgres_flag_requires_the_database_setting(monkeypatch, tmp_path):
    monkeypatch.delenv("CASE_REVIEW_POSTGRES_TEST_DSN", raising=False)
    assert main(["--mode", "stub", "--retriever", "postgres", "--output-dir", str(tmp_path)]) == 2
    assert not (tmp_path / "scorecard.json").exists()
    with pytest.raises(DefenseEvalError, match="CASE_REVIEW_POSTGRES_TEST_DSN"):
        PostgresHybridRetriever(load_suite())


def test_learned_retrieval_floors_follow_worker_health(monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", raising=False)
    assert _healthy_retrieval_worker() is None
    embedder, _reranker, floors = hybrid_adapters(None)
    assert type(embedder).__name__ == "DeterministicEmbeddingAdapter"
    assert floors == {}
    embedder, _reranker, floors = hybrid_adapters("http://127.0.0.1:9")
    assert type(embedder).__name__ == "RemoteEmbeddingAdapter"
    assert floors == {"minimum_dense_score": 0.72, "minimum_rerank_score": 0.80}

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            return self.payload

    monkeypatch.setenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        "case_intelligence.defense_eval.urllib.request.urlopen",
        lambda *args, **kwargs: Response(
            b'{"status":"ok","embedding_loaded":true,"reranker_loaded":true}'
        ),
    )
    assert _healthy_retrieval_worker() == "http://127.0.0.1:9"
    monkeypatch.setattr(
        "case_intelligence.defense_eval.urllib.request.urlopen",
        lambda *args, **kwargs: Response(
            b'{"status":"ok","embedding_loaded":false,"reranker_loaded":true}'
        ),
    )
    assert _healthy_retrieval_worker() is None
