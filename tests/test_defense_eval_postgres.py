"""PostgreSQL hybrid retrieval for the defense-workflow harness.

Skipped unless CASE_REVIEW_POSTGRES_TEST_DSN is set. The application suite
does not require a database; the postgres-integration job runs this file.
"""
from __future__ import annotations

import json
import os

import pytest

from case_intelligence.defense_eval import (
    PostgresHybridRetriever, ReferenceStub, load_suite, main, run_eval,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("CASE_REVIEW_POSTGRES_TEST_DSN"),
    reason="set CASE_REVIEW_POSTGRES_TEST_DSN for the isolated development database",
)


def test_postgres_hybrid_path_scores_every_question_and_removes_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", raising=False)
    suite = load_suite()
    retriever = PostgresHybridRetriever(suite)
    try:
        scorecard = run_eval(
            suite, ReferenceStub(), retriever=retriever, retriever_record=retriever.record,
            model={"mode": "stub", "backend": "reference-stub", "model": "reference-stub"},
        )
        by_matter = {passage.passage_id: passage.matter_id for passage in suite.passages}
        for row in scorecard["questions"]:
            if row["type"] != "not_in_record":
                assert row["retrieved_passage_ids"]
            assert all(by_matter[passage_id] == row["matter_id"] for passage_id in row["retrieved_passage_ids"])
        totals = scorecard["aggregates"]
        assert totals["questions"] == 42
        assert totals["retrieval_applicable"] == 34
        # English websearch AND matches no single passage for the long questions
        # below, so the deterministic dense lane and reranker fill the top set.
        assert {
            row["question_id"] for row in scorecard["questions"] if row["retrieval_hit"] is False
        } == {
            "dw-dispatch-arrival",
            "dw-inspection-plan",
            "dw-passage-observation",
            "dw-platform-contents",
            "dw-screen-then-transfer",
        }
        assert scorecard["retriever"] == {
            "name": "postgres-hybrid",
            "index": "postgresql-fts-pgvector",
            "embedder": "DeterministicEmbeddingAdapter",
            "reranker": "DeterministicReranker",
            "learned_thresholds": False,
        }
        rendered = json.dumps(scorecard)
        assert "postgresql://" not in rendered
        assert "synthetic-ci-only" not in rendered
        assert os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"] not in rendered
        assert totals["retrieval_hit"] == 29
        assert totals["answer_correct"] == totals["citation_support"] == totals["abstention_correct"] == 37
        lanes = scorecard["lane_diagnostics"]
        assert lanes["questions_diagnosed"] == 34
        assert lanes["fused_matches_rerank_input"] is True
        assert lanes["deep_prefix_matches"] is True
        # Every miss is absent from the full-text lane and reaches the reranker
        # through the dense lane, so on this path each one is a ranking miss.
        assert {(entry["question_id"], entry["passage_id"]) for entry in lanes["misses"]} == {
            ("dw-dispatch-arrival", "hs-report-04"),
            ("dw-dispatch-arrival", "hs-report-05"),
            ("dw-inspection-plan", "hs-report-02"),
            ("dw-inspection-plan", "hs-report-03"),
            ("dw-passage-observation", "hs-note-21"),
            ("dw-platform-contents", "hs-report-06"),
            ("dw-screen-then-transfer", "hs-report-04"),
        }
        assert lanes["by_lane_status"]["lexical"] == {"hit": 0, "ranking": 0, "recall": 7}
        assert lanes["by_class"] == {"ranking": 7}
        assert lanes["by_deciding_lane"] == {"post_rerank": 3, "rerank": 4}
        hit_rows = [row for row in scorecard["questions"] if row["retrieval_hit"] is True]
        assert all(
            not entry["missed"]
            for row in hit_rows for entry in row["lane_diagnostics"]["passages"]
        )
    finally:
        retriever.close()

    import psycopg

    database_ids = list(retriever._database_ids.values())
    with psycopg.connect(os.environ["CASE_REVIEW_POSTGRES_TEST_DSN"]) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT matter_id FROM review_matter WHERE matter_id = ANY(%s)",
                (database_ids,),
            )
            assert cursor.fetchall() == []

    code = main(["--mode", "stub", "--retriever", "postgres", "--output-dir", str(tmp_path)])
    assert code == 0
    written = json.loads((tmp_path / "scorecard.json").read_text(encoding="utf-8"))
    assert written["retriever"]["name"] == "postgres-hybrid"
    assert written["aggregates"]["retrieval_hit"] == 29
    assert "postgresql://" not in (tmp_path / "scorecard.json").read_text(encoding="utf-8")
