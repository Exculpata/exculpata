from types import SimpleNamespace
from threading import RLock
from unittest.mock import MagicMock, Mock

import pytest

from case_intelligence import retrieval_endpoints
from case_intelligence.review_bench_v2 import (
    Candidate, InMemoryHybridBackend, RemoteEmbeddingAdapter, RemoteRerankerAdapter,
)
from tests.test_retrieval_endpoints import endpoint as endpoint


@pytest.mark.parametrize("role", ["embedding", "reranking"])
def test_retrieval_checks_the_current_grant_for_every_external_batch(endpoint, monkeypatch, role):
    allowed = True
    checked = []
    sent = []
    request = retrieval_endpoints.request_json

    def allow(matter_id, destination):
        checked.append((matter_id, destination.capability, destination.endpoint, destination.model))
        return allowed

    def revoke_after_dispatch(config, path, body):
        nonlocal allowed
        result = request(config, path, body)
        sent.append(body)
        allowed = False
        return result

    monkeypatch.setattr(retrieval_endpoints, "request_json", revoke_after_dispatch)
    if role == "embedding":
        adapter = RemoteEmbeddingAdapter("").for_matter("matter-alpha", allow)
        adapter.batch_limit = 1
        with pytest.raises(retrieval_endpoints.RetrievalGrantRequired):
            adapter.embed(["Synthetic first note", "Synthetic next note"])
        assert sent[0]["input"] == ["Synthetic first note"]
    else:
        candidates = [Candidate("matter-alpha", "notes", str(i), "Notes.txt", i, "Synthetic note")
                      for i in range(1, 42)]
        adapter = RemoteRerankerAdapter("").for_matter("matter-alpha", allow)
        with pytest.raises(retrieval_endpoints.RetrievalGrantRequired):
            adapter.rerank("Synthetic query", candidates)
        assert len(sent[0]["documents"]) == 40
    assert len(endpoint["requests"]) == len(sent) == 1
    assert checked == [("matter-alpha", role, "https://models.example.test", "synthetic")] * 2


@pytest.mark.parametrize("change", ["unbound", "model", "endpoint"])
def test_rerank_grant_cannot_be_used_for_a_different_destination(endpoint, monkeypatch, change):
    from case_intelligence.external_model_grants import configured_model_destination

    granted = configured_model_destination("reranking")
    adapter = RemoteRerankerAdapter("")
    if change != "unbound":
        adapter = adapter.for_matter("matter-alpha", lambda matter, dest: matter == "matter-alpha" and dest == granted)
    if change == "model":
        monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_MODEL", "replacement")
    elif change == "endpoint":
        monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_URL", "https://other.example.test")
    item = Candidate("matter-alpha", "document", "chunk", "Notes.txt", 1, "Synthetic note")
    with pytest.raises(retrieval_endpoints.RetrievalGrantRequired):
        adapter.rerank("Synthetic query", [item])
    assert endpoint["requests"] == []


def test_scoped_reranker_refuses_candidates_from_another_matter(endpoint):
    adapter = RemoteRerankerAdapter("").for_matter("matter-alpha", lambda _matter, _destination: True)
    foreign = Candidate("matter-bravo", "document", "chunk", "Notes.txt", 1, "Synthetic restricted note")
    with pytest.raises(RuntimeError, match="matter boundary"):
        adapter.rerank("Synthetic query", [foreign])
    assert endpoint["requests"] == []


def test_scoped_embedding_retains_explicit_transport_and_still_requires_its_grant(endpoint):
    transport = Mock(return_value={"vectors": [[0.5] * 768]})
    adapter = RemoteEmbeddingAdapter("http://retrieval:8767", request=transport)
    granted = adapter.for_matter("allowed", lambda matter, _dest: matter == "allowed")
    assert granted.embed(["Synthetic record"])[0] == (0.5,) * 768
    transport.assert_called_once()
    assert transport.call_args.args[0] == "https://models.example.test"
    with pytest.raises(retrieval_endpoints.RetrievalGrantRequired):
        adapter.for_matter("denied", lambda _matter, _dest: False).embed(["Synthetic restricted record"])
    transport.assert_called_once()
    assert endpoint["requests"] == []


def test_denied_matter_keeps_lexical_results_and_other_matters_providers(endpoint, monkeypatch):
    from case_intelligence.workbench import CaseIntelligenceWorkbench

    bench = object.__new__(CaseIntelligenceWorkbench)
    bench.learned_retrieval = bench.postgres_ready = True
    bench.postgres_connection = Mock()
    bench._postgres_lock = None
    bench.embedding = RemoteEmbeddingAdapter("")
    bench.reranker = RemoteRerankerAdapter("")
    bench.embedding_index_warning = ""
    bench.workspace = Mock()
    bench.workspace.model_grant_allows.side_effect = lambda matter_id, _destination: matter_id == "matter-alpha"
    for matter_id in ("matter-bravo", "matter-alpha"):
        item = Candidate(matter_id, "document", "chunk", "Notes.txt", 1, f"Synthetic note from {matter_id}")
        monkeypatch.setattr("case_intelligence.workbench.PostgresHybridBackend",
                            lambda *args, **kwargs: InMemoryHybridBackend([item]))
        retriever = bench._retriever(SimpleNamespace(matter_id=matter_id))
        assert retriever.search(matter_id, "synthetic note")[0].document_id == "document"
        assert len(endpoint["requests"]) == (0 if matter_id == "matter-bravo" else 2)
    assert endpoint["body"]["documents"] == ["Synthetic note from matter-alpha"]


def test_index_dispatch_uses_current_matter_grant_and_preserves_lexical_rows(endpoint, tmp_path, monkeypatch):
    from case_intelligence.external_model_grants import configured_model_destination
    from case_intelligence.pilot_uploads import PilotDocument
    from case_intelligence.workbench import CaseIntelligenceWorkbench

    connection = MagicMock(autocommit=True)
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (False, False)
    bench = CaseIntelligenceWorkbench(tmp_path / "runtime", postgres_connection=connection)
    try:
        owner = "principal-owner"
        bench.workspace.upsert_principal("test", owner, owner, owner, preferred_principal_id=owner)
        allowed = bench.workspace.create_matter("Synthetic approved", "", owner)
        denied = bench.workspace.create_matter("Synthetic restricted", "", owner)
        destination = configured_model_destination("embedding")
        bench.workspace.set_model_grant(allowed.matter_id, destination, owner, approved=True)
        endpoint["requests"].clear()
        received = []

        def index(matter, text):
            document = PilotDocument("synthetic-document", "Notes.txt", "notes.txt", "text/plain",
                                     len(text), "ready", "", [{"number": 1, "text": text}], version_id="synthetic-v1")
            bench._index_document(matter, document)
            writes = [call.args[1] for call in cursor.execute.call_args_list
                      if call.args[0].startswith("INSERT INTO review_chunk(")]
            written = writes[-1]
            assert written[0] == matter.matter_id and written[5] == text
            if written[6] is not None:
                received.append(endpoint["body"]["input"])

        index(denied, "Synthetic restricted record.")
        assert endpoint["requests"] == []
        index(allowed, "Synthetic approved record.")
        bench.workspace.set_model_grant(allowed.matter_id, destination, owner, approved=False)
        index(allowed, "Synthetic record queued before revocation.")
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_URL", "invalid")
        index(allowed, "Synthetic record indexed after endpoint misconfiguration.")
        assert received == [["Synthetic approved record."]]
        assert len(endpoint["requests"]) == 1
        assert bench.embedding_status == "endpoint verified"
    finally:
        bench.close()


def test_invalid_rerank_configuration_after_startup_keeps_lexical_results(endpoint, monkeypatch):
    from case_intelligence.review_bench_v2 import HybridRetriever

    item = Candidate("matter-alpha", "document", "chunk", "Notes.txt", 1, "Synthetic note")
    adapter = RemoteRerankerAdapter("").for_matter("matter-alpha", lambda _matter, _dest: True)
    failed = Mock()
    retriever = HybridRetriever(InMemoryHybridBackend([item]), None, adapter, endpoint_failed=failed)
    monkeypatch.setenv("CASE_INTELLIGENCE_RERANK_MODEL", "invalid\u200bmodel")
    assert retriever.search("matter-alpha", "Synthetic")[0].chunk_id == "chunk"
    failed.assert_called_once_with("RERANK", retrieval_endpoints.INVALID_CONFIGURATION)
    assert endpoint["requests"] == []


def test_failed_partial_index_lookup_keeps_embedding_provider_available(endpoint):
    from case_intelligence.embedding_index import MISSING_EMBEDDINGS
    from case_intelligence.workbench import CaseIntelligenceWorkbench

    bench = object.__new__(CaseIntelligenceWorkbench)
    bench.learned_retrieval = True
    bench.embedding = RemoteEmbeddingAdapter("")
    bench.embedding_index_warning = MISSING_EMBEDDINGS
    bench._postgres_lock = RLock()
    bench.postgres_connection = MagicMock()
    cursor = bench.postgres_connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = RuntimeError("Synthetic projection unavailable")
    assert bench._matter_embedding("matter-alpha") is None
    assert isinstance(bench.embedding, RemoteEmbeddingAdapter)
    bench.postgres_connection.transaction.return_value.__exit__.assert_called_once()
    assert endpoint["requests"] == []
