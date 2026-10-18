"""Synthetic extraction/client/worker composition; never load learned models."""
from __future__ import annotations

import hashlib
import io
import json
import math
import time
import urllib.error
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from case_intelligence import retrieval_worker
from case_intelligence.pilot_uploads import PilotStore
from case_intelligence.review_bench_v2 import (
    Candidate, RemoteEmbeddingAdapter, RemoteRerankerAdapter, upsert_postgres_document,
)


@pytest.fixture
def worker_transport(monkeypatch):
    requests = []
    embedded = []

    class Embedding:
        def encode(self, texts, **_kwargs):
            embedded.extend(texts)
            return [[1.0, float("TAIL" in text)] + [0.0] * 766 for text in texts]

    class Reranker:
        def predict(self, pairs):
            return [float("TAIL" in text) for _query, text in pairs]

    monkeypatch.setattr(retrieval_worker, "_embedding", Embedding())
    monkeypatch.setattr(retrieval_worker, "_reranker", Reranker())
    with TestClient(retrieval_worker.app) as client:
        def open_request(request, timeout):
            requests.append(request)
            response = client.post(request.full_url, content=request.data,
                                   headers={"Content-Type": "application/json"})
            if response.status_code != 200:
                raise urllib.error.HTTPError(request.full_url, response.status_code,
                                             "synthetic rejection", {}, None)
            return io.BytesIO(response.content)

        monkeypatch.setattr("urllib.request.urlopen", open_request)
        yield requests, embedded


class RecordingDatabase:
    autocommit = True

    def __init__(self):
        self.calls = []

    @contextmanager
    def transaction(self):
        yield

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))


def test_accepted_long_text_indexes_and_reranks_without_changing_evidence(tmp_path, worker_transport):
    text = "Synthetic record " + "x" * 25_197 + " TAIL"
    assert len(text) == 25_219
    store = PilotStore(tmp_path / "sources")
    try:
        document, _ = store.store_stream("synthetic.txt", "text/plain", io.BytesIO(text.encode()))
        original = document.parsed_units()
        assert len(original) == 1 and original[0].text == text
        connection = RecordingDatabase()
        upsert_postgres_document(connection, matter_id="synthetic-matter", matter_name="Synthetic",
            document_id=document.document_id, display_name=document.display_name,
            media_type=document.media_type, units=original, ensure_schema=False,
            source_version_id=document.version_id, embedder=RemoteEmbeddingAdapter("http://testserver"))
        for sql, params in connection.calls:
            if "INSERT INTO review_chunk" in sql:
                chunk = params
                break
        else:
            raise StopIteration
        assert chunk[3] == "chunk-1"
        assert chunk[5] == text
        assert chunk[7:11] == (1, 1, document.version_id, hashlib.sha256(text.encode()).hexdigest())
        candidate = Candidate("synthetic-matter", document.document_id, "chunk-1", "synthetic.txt",
            1, text, line_start=1, line_end=1, source_version_id=document.version_id,
            excerpt_digest=original[0].excerpt_digest)
        ranked = RemoteRerankerAdapter("http://testserver").rerank("TAIL", [candidate])
        assert len(ranked) == 1 and ranked[0].score == 1.0
        assert ranked[0].text == text and ranked[0].chunk_id == candidate.chunk_id
        assert ranked[0].excerpt_digest == candidate.excerpt_digest and ranked[0].citation == "Line 1"
        assert document.parsed_units() == original
        assert "".join(worker_transport[1]) == text
        assert len(worker_transport[1]) > 1
    finally:
        store.close()


@pytest.mark.parametrize("character", ["é", "界", "😀", "\"", "\\"])
def test_batches_obey_actual_wire_byte_limit_and_preserve_order(character, worker_transport):
    texts = [f"Synthetic-{index:02d} " + character * 19_980 for index in range(48)]
    vectors = RemoteEmbeddingAdapter("http://testserver").embed(texts)
    requests, embedded = worker_transport
    assert len(vectors) == len(texts)
    assert embedded == texts
    assert all(len(request.data) <= retrieval_worker.MAX_BODY_BYTES for request in requests)
    assert all(len(json.loads(request.data)["texts"]) <= 48 for request in requests)


@pytest.mark.parametrize("length", [19_999, 20_000, 20_001, 40_003])
def test_fragment_edges_keep_all_text_and_aggregate_one_vector(length, worker_transport):
    text = "x" * length
    vectors = RemoteEmbeddingAdapter("http://testserver").embed([text, "other"])
    assert len(vectors) == 2 and all(len(row) == 768 for row in vectors)
    assert "".join(worker_transport[1][:-1]) == text
    assert vectors[0][:2] == (1.0, 0.0)
    assert vectors[1][:2] == (1.0, 0.0)


def test_fragment_aggregation_weights_characters_and_normalizes(worker_transport):
    vectors = RemoteEmbeddingAdapter("http://testserver").embed(["x" * 20_000 + "TAIL" * 2_500])
    norm = math.hypot(1.0, 1 / 3)
    assert vectors[0][:2] == pytest.approx((1 / norm, (1 / 3) / norm))


@pytest.mark.parametrize("overhead", [0, -1])
def test_exact_serialized_byte_boundary_includes_framing(overhead, monkeypatch, worker_transport):
    from case_intelligence import retrieval_limits
    body = b'{"texts":["first","second"]}'
    monkeypatch.setattr(retrieval_limits, "MAX_BODY_BYTES", len(body) + overhead)
    RemoteEmbeddingAdapter("http://testserver").embed(["first", "second"])
    assert len(worker_transport[0]) == (1 if overhead == 0 else 2)
    assert all(len(request.data) <= len(body) + overhead for request in worker_transport[0])


def test_unrepresentable_request_is_nonretryable_before_transport(monkeypatch):
    from case_intelligence import retrieval_limits
    monkeypatch.setattr(retrieval_limits, "MAX_BODY_BYTES", 10)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("unexpected transport"))
    with pytest.raises(retrieval_limits.RetrievalInputProblem):
        RemoteEmbeddingAdapter("http://testserver").embed(["Synthetic"])


def test_whitespace_only_fragments_remain_in_evidence_without_invalid_worker_calls(worker_transport):
    text = "x" * 20_000 + " " * 20_000 + "TAIL"
    vectors = RemoteEmbeddingAdapter("http://testserver").embed([text])
    assert len(vectors) == 1
    assert worker_transport[1] == ["x" * 20_000, "TAIL"]


def test_rerank_byte_batches_keep_original_candidates(worker_transport):
    candidates = [Candidate("synthetic", "document", f"chunk-{index}", "Synthetic.pdf",
        index, "😀" * 19_994 + ("TAIL" if index == 37 else "text")) for index in range(40)]
    ranked = RemoteRerankerAdapter("http://testserver").rerank("q" * 512, candidates)
    assert ranked[0].chunk_id == "chunk-37"
    assert len(ranked) == len(candidates)
    assert {item.chunk_id: item.text for item in ranked} == {item.chunk_id: item.text for item in candidates}
    assert len(worker_transport[0]) > 1
    assert all(len(request.data) <= retrieval_worker.MAX_BODY_BYTES for request in worker_transport[0])


@pytest.mark.parametrize("texts", [[""], [" "], ["\ud800"]])
def test_invalid_inputs_fail_locally_without_service_input_leak(texts, monkeypatch):
    from case_intelligence.retrieval_limits import RetrievalInputProblem
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("unexpected transport"))
    with pytest.raises(RetrievalInputProblem, match="administrator") as error:
        RemoteEmbeddingAdapter("http://testserver").embed(texts)
    assert error.value.retryable is False


@pytest.mark.parametrize("query", ["", "q" * 513, "\ud800"])
def test_rerank_invalid_query_fails_before_transport(query, monkeypatch):
    from case_intelligence.retrieval_limits import RetrievalInputProblem
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: pytest.fail("unexpected transport"))
    with pytest.raises(RetrievalInputProblem):
        RemoteRerankerAdapter("http://testserver").rerank(query, [])


@pytest.mark.parametrize("vectors", [[], [[1.0], [1.0, 2.0]], [[float("nan")], [1.0]], [[float("inf")], [1.0]]])
def test_malformed_fragment_vectors_fail_before_index_writes(vectors, monkeypatch):
    from case_intelligence.pilot_uploads import PilotUnit
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k:
                        io.BytesIO(json.dumps({"vectors": vectors}).encode()))
    connection = RecordingDatabase()
    with pytest.raises(RuntimeError, match="invalid embedding batch"):
        upsert_postgres_document(connection, matter_id="synthetic", matter_name="Synthetic",
            document_id="document", display_name="Synthetic.txt", media_type="text/plain",
            units=[PilotUnit(1, "x" * 20_001)], ensure_schema=False,
            embedder=RemoteEmbeddingAdapter("http://testserver"))
    assert connection.calls == []


@pytest.mark.parametrize("failure", ["unavailable", "dimension", "missing"])
def test_later_fragment_failure_never_publishes_a_partial_index(failure, monkeypatch):
    from case_intelligence.pilot_uploads import PilotUnit
    calls = []

    def open_request(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 2 and failure == "unavailable":
            raise urllib.error.HTTPError("http://testserver/embed", 503, "unavailable", {}, None)
        rows = [[1.0] + [0.0] * 767]
        if len(calls) == 2:
            rows = [[1.0]] if failure == "dimension" else []
        return io.BytesIO(json.dumps({"vectors": rows}).encode())

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    adapter = RemoteEmbeddingAdapter("http://testserver")
    adapter.batch_limit = 1
    connection = RecordingDatabase()
    with pytest.raises((RuntimeError, urllib.error.HTTPError)):
        upsert_postgres_document(connection, matter_id="synthetic", matter_name="Synthetic",
            document_id="document", display_name="Synthetic.txt", media_type="text/plain",
            units=[PilotUnit(1, "x" * 20_001)], ensure_schema=False, embedder=adapter)
    assert len(calls) == 2
    assert connection.calls == []


@pytest.mark.parametrize("background,status", [(False, 413), (True, 422)])
def test_permanent_index_rejection_is_actionable_and_not_retryable_after_reopen(
    tmp_path, monkeypatch, background, status,
):
    from case_intelligence.workbench import create_workbench_app
    root = tmp_path / "runtime"
    app = create_workbench_app(root, auth_mode="test", background_ingestion=background,
                               ingestion_workers=1)

    def rejected(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://testserver/embed", status,
            "synthetic server detail must stay private", {}, io.BytesIO(b"synthetic private input"))

    monkeypatch.setattr("urllib.request.urlopen", rejected)
    with TestClient(app) as client:
        bench = app.state.workbench
        created = client.post("/matters", data={"name": "Synthetic boundary", "descriptor": "Synthetic"},
                              follow_redirects=False)
        slug = created.headers["location"].split("/")[2]
        matter = bench.matter(slug, "development-taylor-morgan")
        bench.postgres_ready = True
        monkeypatch.setattr(bench, "_index_document", lambda *_args:
                            RemoteEmbeddingAdapter("http://testserver").embed(["synthetic"]))
        if background:
            monkeypatch.setattr(bench.ingestion, "index_document", bench._index_document)
        uploaded = client.post(f"/matters/{slug}/uploads", files={
            "files": ("synthetic.txt", b"Synthetic record", "text/plain")})
        assert uploaded.status_code == 200
        store = bench.source_store(matter)
        document = next(iter(store.documents.values()))
        deadline = time.monotonic() + 5
        while document.state in {"queued", "processing", "ready"} and time.monotonic() < deadline:
            time.sleep(0.01)
        assert document.state == "failed"
        assert "administrator" in document.message and "Try again" not in document.message
        assert "private" not in document.message
        assert bench.sources(matter)[0].retryable is False
        assert bench._source_catalog_projection(document)["retryable"] is False
        token = store.action_token(document)
        rejected_retry = client.post(f"/matters/{slug}/sources/{token}/retry", follow_redirects=False)
        assert rejected_retry.status_code == 409
        assert "administrator" in rejected_retry.text
        assert b"Synthetic record" == (store.files / document.stored_name).read_bytes()
    reopened = create_workbench_app(root, auth_mode="test", background_ingestion=False)
    with TestClient(reopened) as client:
        bench = reopened.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        assert bench.sources(matter)[0].retryable is False
        assert client.post(f"/matters/{slug}/sources/{token}/retry", follow_redirects=False).status_code == 409
