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
            assert len(row["retrieved_passage_ids"]) <= 8
            assert all(by_matter[passage_id] == row["matter_id"] for passage_id in row["retrieved_passage_ids"])
        totals = scorecard["aggregates"]
        assert totals["questions"] == len(suite.questions)
        assert totals["errors"] == 0
        assert {row["question_id"] for row in scorecard["questions"]} == {
            question.question_id for question in suite.questions
        }
        for metric in ("answer_correct", "citation_support", "abstention_correct", "retrieval_hit"):
            assert totals[metric] == sum(row[metric] is True for row in scorecard["questions"])
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
    assert written["aggregates"]["errors"] == 0
    assert written["aggregates"]["questions"] == len(suite.questions)
    assert "postgresql://" not in (tmp_path / "scorecard.json").read_text(encoding="utf-8")
