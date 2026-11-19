"""Synthetic workbench checks for independently configured retrieval endpoints."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from case_intelligence import embedding_index, retrieval_endpoints, review_bench_v2
from case_intelligence.external_model_grants import configured_model_destination
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import CaseIntelligenceWorkbench, create_workbench_app
from tests.test_browser_local_accounts import ORIGIN, login
from tests.test_evaluation_storage_banner import _app as local_admin_app
from tests.test_milestone_a_workbench import WEB_ACTOR, _create_matter
from tests.test_retrieval_endpoints import endpoint as endpoint


def _app(tmp_path, **kwargs):
    return create_workbench_app(
        tmp_path / "runtime", auth_mode="test", generator=UnavailableGenerator(), **kwargs,
    )


def test_admin_shows_independent_endpoint_and_index_checks(endpoint, monkeypatch, tmp_path):
    monkeypatch.delenv("CASE_INTELLIGENCE_RERANK_URL")
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", raising=False)
    with TestClient(local_admin_app(tmp_path), base_url=ORIGIN) as client:
        assert login(client, "alice.reviewer", "synthetic-local-password").status_code == 303
        page = client.get("/admin")
        assert page.status_code == 200
        assert "<dt>Embedding provider</dt><dd>endpoint verified</dd>" in page.text
        assert "<dt>Reranking provider</dt><dd>not configured</dd>" in page.text
        assert "<dt>Embedding index</dt><dd>unavailable</dd>" in page.text


@pytest.mark.parametrize("configured,enabled", [(False, True), (True, False)])
def test_exact_search_without_endpoint_traffic(endpoint, monkeypatch, tmp_path, configured, enabled):
    if not configured:
        for role in ("EMBEDDING", "RERANK"):
            monkeypatch.delenv(f"CASE_INTELLIGENCE_{role}_URL")
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", raising=False)
    with TestClient(_app(tmp_path, learned_retrieval=enabled)) as client:
        capabilities = client.get("/health").json()["capabilities"]
        assert capabilities["search"] == "word search only"
        if not configured:
            assert capabilities["embedding"] == capabilities["reranking"] == "not configured"
        slug = _create_matter(client)
        response = client.post(
            f"/matters/{slug}/uploads",
            files={"files": ("Bicycle notes.txt", b"Synthetic bicycle receipt.", "text/plain")},
        )
        assert response.status_code == 200
        result = client.get(f"/matters/{slug}/exact-search", params={"words": "bicycle"})
        assert result.status_code == 200 and "Bicycle notes.txt" in result.text
    assert endpoint["requests"] == []


@pytest.mark.parametrize("changed,rerank_enabled", [(False, False), (False, True), (True, False)])
def test_verified_embedding_and_index_are_reported_independently(endpoint, monkeypatch, tmp_path, changed, rerank_enabled):
    if not rerank_enabled:
        monkeypatch.delenv("CASE_INTELLIGENCE_RERANK_URL")
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", raising=False)
    config = retrieval_endpoints.configured_endpoint("EMBEDDING")
    path = embedding_index.embedding_index_path()
    embedding_index.record_embedding_index(path, model=config.model, revision=config.identity, populated=True)
    if changed:
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "replacement")
        endpoint["model"] = "replacement"
    before = path.read_bytes()
    connection = MagicMock(autocommit=True)
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (True, False)
    with TestClient(_app(tmp_path, postgres_connection=connection)) as client:
        status = client.get("/health").json()["capabilities"]
        assert status["embedding"] == "endpoint verified"
        assert status["reranking"] == ("endpoint verified" if rerank_enabled else "not configured")
        if changed:
            assert "rebuilt" in status["embedding_index"].lower()
            assert status["search"] == "word search only"
            slug = _create_matter(client)
            client.post(f"/matters/{slug}/uploads", files={
                "files": ("Bicycle notes.txt", b"Synthetic bicycle receipt.", "text/plain"),
            }).raise_for_status()
            result = client.get(f"/matters/{slug}/exact-search", params={"words": "bicycle"})
            assert result.status_code == 200 and "Bicycle notes.txt" in result.text
            assert path.read_bytes() == before
        else:
            assert status["embedding_index"] == "ready"
            assert status["search"] == ("word + meaning" if rerank_enabled else "word search with embeddings")
        assert len(endpoint["requests"]) == (4 if rerank_enabled else 2)


def test_failed_configured_endpoint_stays_unavailable_with_a_healthy_worker(endpoint, monkeypatch, tmp_path):
    monkeypatch.setenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "http://retrieval:8787")
    monkeypatch.setattr(CaseIntelligenceWorkbench, "_retrieval_worker_ready", staticmethod(lambda _: True))
    monkeypatch.setattr(review_bench_v2, "_local_model_request",
                        lambda *args: pytest.fail("Failed endpoint fell back to bundled model"))
    endpoint["status"] = 401
    connection = MagicMock(autocommit=True)
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (False, False)
    with TestClient(_app(tmp_path, postgres_connection=connection)) as client:
        status = client.get("/health").json()["capabilities"]
        assert "authentication" in status["embedding"].lower()
        assert "authentication" in status["reranking"].lower()
        assert status["embedding_index"] == "ready"
        assert status["search"] == "word search only"
        slug = _create_matter(client)
        client.post(f"/matters/{slug}/uploads", files={
            "files": ("Training note.txt", b"Synthetic receipt.", "text/plain"),
        }).raise_for_status()
        assert len(endpoint["requests"]) == 2


def test_embedding_outage_during_intake_keeps_source_searchable(endpoint, monkeypatch, tmp_path):
    monkeypatch.delenv("CASE_INTELLIGENCE_RERANK_URL")
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", raising=False)
    connection = MagicMock(autocommit=True)
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (False, False)
    with TestClient(_app(tmp_path, postgres_connection=connection)) as client:
        status = client.get("/health").json()["capabilities"]
        assert status["embedding"] == "endpoint verified"
        assert status["embedding_index"] == "ready"
        slug = _create_matter(client)
        bench = client.app.state.workbench
        bench.workspace.set_model_grant(bench.matter(slug, WEB_ACTOR).matter_id,
                                        configured_model_destination("embedding"), WEB_ACTOR, approved=True)
        endpoint["status"] = 401
        client.post(f"/matters/{slug}/uploads", files={
            "files": ("Inventory.txt", b"Synthetic bicycle receipt.", "text/plain"),
        }).raise_for_status()
        chunks = [call.args[1] for call in cursor.execute.call_args_list
                  if call.args[0].startswith("INSERT INTO review_chunk(")]
        assert len(chunks) == 1 and chunks[0][6] is None
        result = client.get(f"/matters/{slug}/exact-search", params={"words": "bicycle"})
        assert result.status_code == 200 and "Inventory.txt" in result.text
        status = client.get("/health").json()["capabilities"]
        assert "authentication" in status["embedding"].lower()
        assert status["embedding_index"] == embedding_index.MISSING_EMBEDDINGS
        assert status["search"] == "word search only"
