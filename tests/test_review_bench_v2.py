from __future__ import annotations

import asyncio
import os
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence import retrieval_worker
from case_intelligence.review_bench import create_app
from case_intelligence.review_bench_v2 import (
    Candidate,
    DeterministicEmbeddingAdapter,
    DeterministicReranker,
    HybridRetriever,
    InMemoryHybridBackend,
    LazySentenceTransformerEmbedding,
    PostgresHybridBackend,
    PostgresMigrator,
    RemoteEmbeddingAdapter,
    RemoteRerankerAdapter,
    extract_pdf_pages,
    postgres_migration_sql,
)

FIXTURE = Path(__file__).parents[1] / "src/case_intelligence/demo_data/synthetic_case_report.pdf"


def _c(matter: str, chunk: str, page: int, text: str, score: float = 1.0) -> Candidate:
    return Candidate(matter, "synthetic-report", chunk, "Synthetic incident report.pdf", page, text, score)


def test_postgres_migration_is_idempotent_and_reopens_with_matter_keys(tmp_path):
    migration = Path(__file__).parents[1] / "migrations/postgresql/0001_review_bench_v2.sql"
    sql = migration.read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "tsvector" in sql and "vector(768)" in sql
    assert sql.count("matter_id") >= 20
    recorder: list[str] = []
    migrator = PostgresMigrator(migration, executor=recorder.append)
    assert migrator.migrate() is True
    assert migrator.migrate() is False
    reopened = PostgresMigrator(migration, applied_versions=migrator.applied_versions, executor=recorder.append)
    assert reopened.migrate() is False
    assert len(recorder) == 1


def test_native_pdf_extraction_preserves_page_numbers_and_text():
    pages = extract_pdf_pages(FIXTURE)
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert "PUBLIC-STYLE SYNTHETIC INCIDENT REPORT" in pages[0].text
    assert "north entrance" in pages[1].text.casefold()
    assert "blue canvas bag" in pages[2].text.casefold()


def test_packaged_migration_matches_operator_copy():
    root = Path(__file__).parents[1] / "migrations" / "postgresql"
    operator_copy = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.glob("*.sql"))
    )
    assert postgres_migration_sql() == operator_copy
    assert "VALUES (1)" in operator_copy and "VALUES (2)" in operator_copy


def test_packaged_demo_sources_match_synthetic_fixture_inputs():
    root = Path(__file__).parents[1]
    fixture_root = root / "tests/fixtures/synthetic/slice1-intake/v1"
    package_root = root / "src/case_intelligence/demo_data/sources"
    for slug in ("alpha", "bravo"):
        fixture = fixture_root / f"matter-{slug}" / "input"
        packaged = package_root / slug
        expected = sorted(path.relative_to(fixture) for path in fixture.rglob("*") if path.is_file())
        actual = sorted(path.relative_to(packaged) for path in packaged.rglob("*") if path.is_file())
        assert actual == expected
        for relative in expected:
            assert (packaged / relative).read_bytes() == (fixture / relative).read_bytes()


def test_packaged_sqlite_migration_matches_operator_copy():
    root = Path(__file__).parents[1]
    assert (
        root / "src/case_intelligence/migrations/sqlite/0001_slice1a.sql"
    ).read_bytes() == (root / "migrations/sqlite/0001_slice1a.sql").read_bytes()


def test_hybrid_fusion_and_reranker_ordering_with_exact_page_citations():
    alpha = [
        _c("matter-alpha", "a1", 1, "A red bicycle appears in the property list", 0.9),
        _c("matter-alpha", "a2", 2, "The shared red bicycle was logged at the north entrance", 0.8),
        _c("matter-alpha", "a3", 3, "A blue canvas bag was inventoried separately", 0.7),
    ]
    backend = InMemoryHybridBackend(alpha)
    retriever = HybridRetriever(backend, DeterministicEmbeddingAdapter(), DeterministicReranker())
    results = retriever.search("matter-alpha", "where was the shared bicycle", limit=3)
    assert [item.page_number for item in results][:2] == [2, 1]
    assert results[0].citation == "Page 2"
    assert backend.calls == [("lexical", "matter-alpha"), ("dense", "matter-alpha")]


def test_dense_only_evidence_requires_both_calibrated_scores():
    candidate = _c("matter-alpha", "a1", 2, "The bicycle was logged at the entrance")

    class DenseOnlyBackend:
        def lexical(self, matter_id, query, limit):
            return ()

        def dense(self, matter_id, query_vector, query, limit):
            return (Candidate(**{**candidate.__dict__, "score": 0.73}),)

    class FixedReranker:
        available = True

        def __init__(self, score):
            self.score = score

        def rerank(self, query, candidates):
            return tuple(
                Candidate(**{**item.__dict__, "score": self.score}) for item in candidates
            )

    passing = HybridRetriever(
        DenseOnlyBackend(),
        DeterministicEmbeddingAdapter(),
        FixedReranker(0.81),
        minimum_dense_score=0.72,
        minimum_rerank_score=0.80,
    )
    failing = HybridRetriever(
        DenseOnlyBackend(),
        DeterministicEmbeddingAdapter(),
        FixedReranker(0.79),
        minimum_dense_score=0.72,
        minimum_rerank_score=0.80,
    )
    assert passing.search("matter-alpha", "two wheel transport") == (
        Candidate(**{**candidate.__dict__, "score": 0.81}),
    )
    assert failing.search("matter-alpha", "blood alcohol level") == ()


def test_worker_rejects_oversized_and_unknown_payloads_before_model_use(monkeypatch):
    class FakeEmbedding:
        calls = 0

        def encode(self, texts, normalize_embeddings=True):
            self.calls += 1
            return [[1.0] for _ in texts]

    fake = FakeEmbedding()
    monkeypatch.setattr(retrieval_worker, "_embedding", fake)
    monkeypatch.setattr(retrieval_worker, "_reranker", object())
    with TestClient(retrieval_worker.app) as client:
        oversized = client.post(
            "/embed",
            json={"texts": ["valid"], "ignored_padding": "x" * (1_048_576 + 1)},
        )
        unknown = client.post("/embed", json={"texts": ["valid"], "unknown": "field"})
    assert oversized.status_code == 413
    assert unknown.status_code == 422
    assert fake.calls == 0


def test_worker_counts_streamed_body_chunks_before_parsing():
    reached_app = False

    async def downstream(scope, receive, send):
        nonlocal reached_app
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        reached_app = True

    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/embed", "headers": []}
    asyncio.run(
        retrieval_worker.RequestBodyLimitMiddleware(downstream, max_body_bytes=4)(
            scope, receive, send
        )
    )
    assert reached_app is False
    assert sent[0]["status"] == 413


def test_hybrid_no_answer_and_adversarial_mixed_matter_fail_closed():
    backend = InMemoryHybridBackend([_c("matter-alpha", "a", 1, "north entrance")])
    retriever = HybridRetriever(backend, DeterministicEmbeddingAdapter(), DeterministicReranker())
    assert retriever.search("matter-alpha", "unfindable zephyr term") == ()

    mixed = InMemoryHybridBackend(
        [_c("matter-alpha", "a", 1, "shared bicycle"), _c("matter-bravo", "b", 9, "shared bicycle")],
        adversarial_mixed=True,
    )
    with pytest.raises(RuntimeError, match="matter boundary"):
        HybridRetriever(mixed, DeterministicEmbeddingAdapter(), DeterministicReranker()).search(
            "matter-alpha", "shared bicycle"
        )


def test_alpha_bravo_isolation_in_both_hybrid_lanes():
    rows = [
        _c("matter-alpha", "a", 2, "shared bicycle north entrance"),
        _c("matter-bravo", "b", 2, "shared bicycle south entrance"),
    ]
    backend = InMemoryHybridBackend(rows)
    retriever = HybridRetriever(backend, DeterministicEmbeddingAdapter(), DeterministicReranker())
    alpha = retriever.search("matter-alpha", "shared bicycle")
    bravo = retriever.search("matter-bravo", "shared bicycle")
    assert alpha and all(item.matter_id == "matter-alpha" for item in alpha)
    assert bravo and all(item.matter_id == "matter-bravo" for item in bravo)
    assert "south" not in repr(alpha).casefold()
    assert "north" not in repr(bravo).casefold()


def test_postgres_source_scope_is_applied_inside_both_candidate_queries():
    class RecordingCursor:
        def __init__(self, calls):
            self.calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, parameters):
            self.calls.append((sql, parameters))

        @staticmethod
        def fetchall():
            return [
                (
                    "matter-alpha",
                    "allowed-document",
                    "chunk-1",
                    "Allowed.txt",
                    1,
                    "bounded evidence",
                    0.9,
                    1,
                    1,
                    "version-1",
                    "digest-1",
                )
            ]

    class RecordingConnection:
        def __init__(self):
            self.calls = []

        def cursor(self):
            return RecordingCursor(self.calls)

    connection = RecordingConnection()
    backend = PostgresHybridBackend(
        connection, document_ids=frozenset({"allowed-document"})
    )
    assert backend.lexical("matter-alpha", "bounded evidence", 12)
    assert backend.dense("matter-alpha", (1.0, 0.0), "bounded evidence", 12)
    assert len(connection.calls) == 2
    for sql, parameters in connection.calls:
        assert "c.matter_id=%s" in sql
        assert "c.document_id = ANY(%s)" in sql
        assert ["allowed-document"] in parameters

    empty_connection = RecordingConnection()
    empty = PostgresHybridBackend(empty_connection, document_ids=frozenset())
    assert empty.lexical("matter-alpha", "bounded evidence", 12) == ()
    assert empty.dense("matter-alpha", (1.0, 0.0), "bounded evidence", 12) == ()
    assert empty_connection.calls == []


def test_model_adapter_is_lazy_and_disabled_without_explicit_enable(monkeypatch):
    monkeypatch.delenv("CASE_REVIEW_ENABLE_MODELS", raising=False)
    adapter = LazySentenceTransformerEmbedding.from_environment()
    assert adapter.available is False
    with pytest.raises(RuntimeError, match="not enabled"):
        adapter.embed(["text"])


class _WorkerResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_remote_model_worker_adapters_validate_shapes_and_rerank(monkeypatch):
    payloads = iter(
        (
            {"vectors": [[1.0, 0.0], [0.0, 1.0]]},
            {"scores": [0.2, 0.9]},
        )
    )

    def fake_open(_request, timeout, **_kwargs):
        assert timeout == 120.0
        return _WorkerResponse(json.dumps(next(payloads)).encode("utf-8"))

    monkeypatch.setattr("case_intelligence.review_bench_v2.open_service_request", fake_open)
    vectors = RemoteEmbeddingAdapter("http://127.0.0.1:8767").embed(["a", "b"])
    assert vectors == ((1.0, 0.0), (0.0, 1.0))

    candidates = (_c("matter-alpha", "a", 1, "first"), _c("matter-alpha", "b", 2, "second"))
    ranked = RemoteRerankerAdapter("http://127.0.0.1:8767").rerank("query", candidates)
    assert [item.chunk_id for item in ranked] == ["b", "a"]


def test_remote_embedding_adapter_batches_large_documents_at_worker_limit(monkeypatch):
    requested_batches: list[list[str]] = []

    def fake_open(request, timeout, **_kwargs):
        assert timeout == 120.0
        texts = json.loads(request.data)["texts"]
        requested_batches.append(texts)
        vectors = [[float(int(text.split("-")[-1])), 0.0] for text in texts]
        return _WorkerResponse(json.dumps({"vectors": vectors}).encode("utf-8"))

    monkeypatch.setattr("case_intelligence.review_bench_v2.open_service_request", fake_open)
    texts = [f"page-{index}" for index in range(97)]
    vectors = RemoteEmbeddingAdapter("http://127.0.0.1:8767").embed(texts)

    assert [len(batch) for batch in requested_batches] == [48, 48, 1]
    assert [int(vector[0]) for vector in vectors] == list(range(97))


def test_remote_embedding_rejects_wrong_batch_size(monkeypatch):
    def open_wrong_batch(*args, **kwargs):
        return _WorkerResponse(b'{"vectors": [[1.0]]}')

    monkeypatch.setattr(
        "case_intelligence.review_bench_v2.open_service_request",
        open_wrong_batch,
    )
    with pytest.raises(RuntimeError, match="invalid embedding batch"):
        RemoteEmbeddingAdapter("http://127.0.0.1:8767").embed(["a", "b"])


def test_v2_browser_routes_status_pdf_search_question_and_staff_safety(tmp_path):
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["capabilities"] == {
            "word_search": "ready",
            "meaning_search": "deterministic demo",
            "source_pages": "ready",
            "model_assistance": "not enabled",
        }
        workspace = client.get("/matters/alpha")
        assert workspace.status_code == 200
        assert "Synthetic incident report.pdf" in workspace.text
        assert "3 pages ready" in workspace.text
        assert "Word + meaning search" in workspace.text

        search = client.get("/matters/alpha", params={"q": "canvas bag inventory"})
        assert "Synthetic incident report.pdf · Page 3" in search.text
        assert "/matters/alpha/sources/synthetic-report?page=3" in search.text

        answer = client.get("/matters/alpha", params={"question": "Where was the bicycle logged?"})
        assert "north entrance" in answer.text.casefold()
        assert "Page 2" in answer.text

        source = client.get("/matters/alpha/sources/synthetic-report", params={"page": 2})
        assert source.status_code == 200
        assert "Page 2 of 3" in source.text
        assert "north entrance" in source.text.casefold()

        combined = health.__repr__() + workspace.text + search.text + answer.text + source.text
        for forbidden in (
            "/home/", "matter-alpha", "chunk-alpha", "ibm-granite", "Alibaba-NLP",
            "pgvector", "sha256", "model_id", "postgresql://",
        ):
            assert forbidden not in combined


def test_browser_falls_back_when_postgres_and_models_are_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_REVIEW_POSTGRES_DSN", "postgresql://invalid@127.0.0.1:1/nope")
    monkeypatch.setenv("CASE_REVIEW_ENABLE_MODELS", "1")
    monkeypatch.setenv("CASE_REVIEW_MODEL_LOCAL_ONLY", "1")
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        response = client.get("/matters/alpha", params={"q": "north entrance"})
        assert response.status_code == 200
        assert "Synthetic incident report.pdf" in response.text
        status = client.get("/health").json()["capabilities"]
        assert status["word_search"] == "ready"
        assert status["source_pages"] == "ready"
def test_extended_source_postgres_migration_matches_operator_copy():
    root = Path(__file__).parents[1]
    assert (
        root
        / "src/case_intelligence/migrations/postgresql/0005_extended_source_media_types.sql"
    ).read_bytes() == (
        root / "migrations/postgresql/0005_extended_source_media_types.sql"
    ).read_bytes()


def test_retrieval_startup_loads_both_offline_models_before_readiness(monkeypatch):
    monkeypatch.setattr(retrieval_worker, "_embedding", None)
    monkeypatch.setattr(retrieval_worker, "_reranker", None)
    calls = []
    def embedding():
        calls.append("embedding")
        retrieval_worker._embedding = object()
    def reranker():
        calls.append("reranker")
        retrieval_worker._reranker = object()
    monkeypatch.setattr(retrieval_worker, "_embedding_model", embedding)
    monkeypatch.setattr(retrieval_worker, "_reranker_model", reranker)
    with TestClient(retrieval_worker.app) as client:
        state = client.get("/health").json()
        assert state["embedding_loaded"] and state["reranker_loaded"]
    assert calls == ["embedding", "reranker"]


def test_retrieval_startup_failure_does_not_serve_false_readiness(monkeypatch):
    def fail():
        raise RuntimeError("synthetic missing offline artifact")
    monkeypatch.setattr(retrieval_worker, "_embedding_model", fail)
    with pytest.raises(RuntimeError, match="synthetic missing offline artifact"):
        with TestClient(retrieval_worker.app):
            pytest.fail("Unavailable models must stop service startup")
