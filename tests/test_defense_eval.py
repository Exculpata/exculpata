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
    write_scorecard,
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


def test_stub_mode_scores_every_reference_question(suite, tmp_path, monkeypatch):
    # A fixed retrieval fixture checks the scorer, not a product-quality threshold.
    def fixed_retrieval(passages, query, top_k):
        return passages

    monkeypatch.setattr("case_intelligence.defense_eval.rank_passages", fixed_retrieval)
    monkeypatch.setattr("case_intelligence.defense_eval.time.perf_counter", lambda: 0)
    scorecard = run_eval(suite, ReferenceStub(), retriever=fixed_retrieval,
                        model={"mode": "stub", "backend": "reference-stub", "model": "reference-stub"})
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
    assert "answer " in result.stdout
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert card["aggregates"]["questions"] == len(load_suite().questions)
    assert card["aggregates"]["errors"] == 0
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


def test_optional_judge_does_not_change_deterministic_totals(suite, monkeypatch):
    monkeypatch.setattr("case_intelligence.defense_eval.time.perf_counter", lambda: 0)
    def judge(question, answer):
        return {"correct": False, "rationale": "synthetic dissent"}

    scorecard = run_eval(suite, ReferenceStub(), judge=judge)
    assert scorecard["aggregates"] == run_eval(suite, ReferenceStub())["aggregates"]
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
    # fixture-write: Prepare synthetic input or evidence for this regression.
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="fingerprint"):
        load_suite(path)
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    payload["passages"][0]["sha256"] = "0" * 64
    payload["fingerprint"] = suite_fingerprint(payload)
    # fixture-write: Prepare synthetic input or evidence for this regression.
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="bytes do not match"):
        load_suite(path)
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    payload["questions"][0]["required_facts"][0]["supports"][0]["support_text"] = "not written in that record"
    payload["fingerprint"] = suite_fingerprint(payload)
    # fixture-write: Prepare synthetic input or evidence for this regression.
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="not in the named passage"):
        load_suite(path)


def test_configured_mode_stops_without_a_generator(monkeypatch, tmp_path):
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", raising=False)

    class Unavailable:
        available = False

    def unavailable_generator():
        return Unavailable()

    monkeypatch.setattr("case_intelligence.defense_eval.generator_from_environment", unavailable_generator)
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
    # fixture-write: Prepare synthetic input or evidence for this regression.
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="competing passage"):
        load_suite(path)
    payload = json.loads(DEFAULT_SUITE.read_text(encoding="utf-8"))
    question = next(item for item in payload["questions"] if item["question_id"] == "dw-opening-count")
    question["required_facts"][0]["answer_text"] = "teal"
    question["required_facts"][0]["supports"][0]["support_text"] = "teal"
    payload["fingerprint"] = suite_fingerprint(payload)
    # fixture-write: Prepare synthetic input or evidence for this regression.
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DefenseEvalError, match="contains a reference span"):
        load_suite(path)


def test_compare_prints_per_category_deltas(tmp_path, capsys):
    left = tmp_path / "a.json"
    right = tmp_path / "b.json"
    # fixture-write: Prepare synthetic input or evidence for this regression.
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
    # fixture-write: Prepare synthetic input or evidence for this regression.
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

    def healthy_response(request, *, timeout, environment_name):
        return Response(b'{"status":"ok","embedding_loaded":true,"reranker_loaded":true}')

    def unloaded_response(request, *, timeout, environment_name):
        return Response(b'{"status":"ok","embedding_loaded":false,"reranker_loaded":true}')

    monkeypatch.setenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(
        "case_intelligence.defense_eval.open_service_request",
        healthy_response,
    )
    assert _healthy_retrieval_worker() == "http://127.0.0.1:9"
    monkeypatch.setattr(
        "case_intelligence.defense_eval.open_service_request",
        unloaded_response,
    )
    assert _healthy_retrieval_worker() is None


def test_suite_rejects_cross_matter_support(tmp_path, suite):
    payload = json.loads(DEFAULT_SUITE.read_text())
    question = payload["questions"][0]
    other = next(passage for passage in suite.passages if passage.matter_id != question["matter_id"])
    question["required_facts"][0]["supports"] = [
        {"passage_id": other.passage_id, "support_text": other.text}
    ]
    payload["fingerprint"] = suite_fingerprint(payload)
    path = tmp_path / "suite.json"
    # fixture-write: Pin a synthetic suite whose valid support belongs to another matter.
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=question["question_id"] + ".*matter"):
        load_suite(path)


def test_comparison_reports_changed_types_in_each_cards_category(tmp_path):
    from case_intelligence.defense_eval import compare_scorecards

    row = {"question_id": "q1", "type": "fact_lookup", "answer_correct": True,
           "citation_support": True, "abstention_correct": True, "retrieval_hit": True}
    left, right = tmp_path / "a.json", tmp_path / "b.json"
    # fixture-write: Supply a synthetic scorecard before a question category change.
    left.write_text(json.dumps({"fingerprint": "before", "suite_version": "2", "questions": [row]}))
    row = dict(row, type="chronology", answer_correct=False)
    # fixture-write: Supply the same question id with a new category and score.
    right.write_text(json.dumps({"fingerprint": "after", "suite_version": "3", "questions": [row]}))
    report = compare_scorecards(left, right)
    assert "Changed question types" in report
    assert "`q1`: fact_lookup -> chronology" in report
    assert "| fact_lookup | 1/1 -> 0/0 (-1) |" in report
    assert "| chronology | 0/0 -> 0/1 (+0) |" in report
    assert "| overall | 1/1 -> 0/1 (-1) |" in report


def test_malformed_model_content_has_specific_unavailable_type():
    from case_intelligence.generation import (
        GenerationInvalidContent, GenerationUnavailable, _parse_model_content,
    )

    assert issubclass(GenerationInvalidContent, GenerationUnavailable)
    with pytest.raises(GenerationInvalidContent):
        _parse_model_content("not json")


@pytest.mark.parametrize("case,reason,lane", [
    ("shared_evaluator", "ranked_out", "rerank"),
    ("hit", None, None),
    ("recall", "not_retrieved", "lexical+vector"),
    ("ranking", "ranked_out", "rerank"),
    ("fusion", "ranked_out", "fused"),
    ("floor", "score_floor", "rerank"),
    ("crowding", "crowded_by_near_duplicates", "post_rerank"),
    ("beyond_depth", "beyond_depth", "post_rerank"),
])
def test_lane_miss_reasons_and_pass_through(case, reason, lane, monkeypatch):
    from dataclasses import asdict, replace
    from case_intelligence.defense_eval_lanes import RecordingBackend, RecordingReranker
    from case_intelligence import review_bench_v2 as product

    candidates = tuple(product.Candidate(
        "matter", name, name, name + ".txt", i + 1, text, score,
    ) for i, (name, text, score) in enumerate([
        ("other", "synthetic unrelated notice", 0.9),
        ("reference", "synthetic decisive statement", 0.2),
    ]))
    if case == "beyond_depth":
        candidates += tuple(replace(candidates[0], document_id=f"extra-{i}",
                                    chunk_id=f"extra-{i}", score=0.1) for i in range(21))
        def reorder(query, rows, **kwargs):
            return tuple(r for r in rows if r.document_id != "reference") + tuple(
                r for r in rows if r.document_id == "reference")
        monkeypatch.setattr(product, "rank_temporal_candidates", reorder)
    lexical = () if case == "floor" else candidates
    dense = candidates
    if case == "recall":
        lexical = dense = candidates[:1]

    class Backend:
        def lexical(self, matter_id, query, limit):
            return lexical[:limit]

        def dense(self, matter_id, vector, query, limit):
            return dense[:limit]

    class Reranker:
        available = True

        def rerank(self, query, rows):
            scores = {r.document_id: r.score for r in candidates}
            return tuple(sorted((replace(r, score=scores[r.document_id]) for r in rows),
                                key=lambda r: -r.score))

    options = {"rerank_limit": 1} if case == "fusion" else {}
    if case == "floor":
        options.update(minimum_dense_score=0.1, minimum_rerank_score=0.5)
    top_k = 2 if case == "hit" else 1
    trace = {}
    embedder = product.DeterministicEmbeddingAdapter()
    plain = product.HybridRetriever(Backend(), embedder, Reranker(), **options)
    recorded = product.HybridRetriever(RecordingBackend(Backend(), trace), embedder,
                                      RecordingReranker(Reranker(), trace), **options)
    expected = plain.search("matter", "synthetic", top_k)
    actual = recorded.search("matter", "synthetic", top_k)
    encode = lambda rows: json.dumps([asdict(r) for r in rows], sort_keys=True).encode()
    assert encode(actual) == encode(expected)
    passages = tuple(Passage(r.document_id, "matter", r.source_name, "0" * 64,
                             "document", "1", r.text) for r in candidates)
    question = Question("q", "matter", "fact_lookup", "synthetic", False,
                        (Fact("fact", "statement", (Support("reference", "statement"),)),),
                        (), ("other",) if case in {"crowding", "beyond_depth"} else ())
    adapter = object.__new__(PostgresHybridRetriever)
    adapter._retriever = recorded
    adapter._trace = trace
    adapter._database_ids = {"matter": "matter"}
    adapter._last_search = ("matter", "synthetic", top_k, dict(trace), actual)
    from case_intelligence.defense_eval import Suite
    adapter.suite = Suite("synthetic", "1", "0" * 64, "synthetic", passages, (question,))
    retrieved = tuple(p for p in passages if p.passage_id in {r.document_id for r in actual})
    if case == "shared_evaluator":
        from case_intelligence.defense_eval import _lane_module
        _lane_module.cache_clear()
        monkeypatch.setattr("case_intelligence.defense_eval.__name__", "__main__")
        monkeypatch.setitem(sys.modules, "case_intelligence.defense_eval_lanes", None)
    diagnostics = adapter.lane_diagnostics(question, retrieved, top_k)
    entry = diagnostics["passages"][0]
    assert (entry["miss_reason"], entry["miss_lane"]) == (reason, lane)
    assert entry["missed"] is (case != "hit")
    assert entry["final_rank"] == (2 if case in {"hit", "crowding", "ranking", "shared_evaluator"} else None)
    if case == "beyond_depth":
        assert entry["rerank_rank"] == 2
        assert entry["near_duplicates_ahead"] == []
    for name in ("lexical", "vector", "fused", "rerank", "final"):
        assert entry[name + "_status"] in {"hit", "ranking", "recall"}
        assert (entry[name + "_rank"] is None) == (entry[name + "_score"] is None)
    assert encode(recorded.search("matter", "synthetic", top_k)) == encode(expected)
    monkeypatch.setattr("case_intelligence.defense_eval.time.perf_counter", lambda: 0)
    measured = run_eval(adapter.suite, ReferenceStub(), retriever=adapter, top_k=top_k)
    def unrecorded(passages, query, top_k):
        ids = [r.document_id for r in plain.search("matter", query, top_k)]
        return tuple(next(p for p in passages if p.passage_id == id_) for id_ in ids)
    baseline = run_eval(adapter.suite, ReferenceStub(), retriever=unrecorded, top_k=top_k)
    assert measured["aggregates"] == baseline["aggregates"]
    measured.pop("lane_diagnostics")
    for row in measured["questions"]:
        row.pop("lane_diagnostics")
    assert measured == baseline
    if case == "ranking":
        original_search = recorded.search
        def drifted(matter_id, query, limit):
            rows = original_search(matter_id, query, limit)
            return (replace(rows[0], score=rows[0].score + 1),) + rows[1:]
        monkeypatch.setattr(recorded, "search", drifted)
        with pytest.raises(DefenseEvalError, match="changed the scored prefix"):
            adapter.lane_diagnostics(question, retrieved, top_k)


def test_lane_summary_and_optional_schema():
    from case_intelligence.defense_eval_lanes import summarize_lane_diagnostics, render_lane_diagnostics
    from case_intelligence.tier_evals import Scorecard, new_scorecard, validate_scorecard
    assert Scorecard.model_fields["lane_diagnostics"].is_required() is False
    card = new_scorecard("a" * 40, "synthetic", {"cpu": "synthetic", "gpus": []})
    validate_scorecard(card)
    card["lane_diagnostics"] = {"misses": []}
    validate_scorecard(json.loads(json.dumps(card)))
    assert Scorecard.model_validate(card).lane_diagnostics == {"misses": []}
    entry = dict(passage_id="p", missed=True, miss_reason="score_floor", miss_class="ranking",
                 miss_lane="rerank", **{f"{lane}_status": "recall" for lane in
                                       ("lexical", "vector", "fused", "rerank", "final")})
    summary = summarize_lane_diagnostics([dict(question_id="q", lane_diagnostics={"passages": [entry]})])
    assert summary["by_reason"] == {"score_floor": 1}
    assert summary["by_lane_status"]["final"]["recall"] == 1
    assert "score_floor" in "\n".join(render_lane_diagnostics(summary))


@pytest.mark.parametrize("reasons", [(), ("score_floor", "ranked_out")])
def test_lane_scorecard_markdown_matches_saved_json(suite, tmp_path, reasons):
    from case_intelligence.defense_eval_lanes import LANES, summarize_lane_diagnostics

    entries = [
        dict(passage_id=f"p-{index}", missed=True, miss_reason=reason, miss_lane="rerank",
             **{f"{lane}_status": "recall" for lane in LANES})
        for index, reason in enumerate(reasons)
    ]
    card = run_eval(suite, ReferenceStub())
    card["lane_diagnostics"] = summarize_lane_diagnostics([
        dict(question_id="synthetic-question", lane_diagnostics={"passages": entries}),
    ])
    # fixture-write: Save synthetic scorecards through the production writer for JSON round-trip coverage.
    json_path, markdown_path = write_scorecard(tmp_path, card)
    saved_card = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown == render_markdown(saved_card)
    assert markdown == render_markdown(card)
    lane_rows = [line.split(" | ")[0] for line in markdown.splitlines()
                 if any(line.startswith(f"| {lane} |") for lane in LANES)]
    assert lane_rows == [f"| {lane}" for lane in LANES]
    if reasons:
        assert "Reasons: ranked_out 1, score_floor 1" in markdown
