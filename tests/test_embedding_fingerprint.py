"""Fixed synthetic vectors for a bundled-to-endpoint index migration."""
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from case_intelligence import embedding_index
from case_intelligence import embedding_fingerprint as fingerprints
from tests.test_retrieval_endpoints import endpoint as endpoint


def test_float32_fingerprint_requires_distinct_nonzero_finite_vectors():
    vectors = [[float(i == n) for i in range(768)] for n in range(3)]
    fingerprint = fingerprints.vector_fingerprint(vectors)
    assert len(fingerprint) == 64
    assert fingerprints.vector_fingerprint(list(reversed(vectors))) != fingerprint
    for bad in (vectors[:-1], [vectors[0]] * 3, [[0.] * 768, *vectors[1:]],
                [[float("nan")] * 768, *vectors[1:]], [[1e100] * 768, *vectors[1:]]):
        with pytest.raises(ValueError):
            fingerprints.vector_fingerprint(bad)


def test_recording_projection_keeps_verified_migration_receipt(tmp_path):
    path = tmp_path / "index.json"
    record = {"projection_format": embedding_index.TRANSFER_FORMAT, "model": "model", "revision": "endpoint:1",
              "populated": True, "synthetic_fingerprint": "b" * 64}
    path.write_text(json.dumps(record))  # fixture-write: synthetic verified migration receipt
    assert not embedding_index.record_embedding_index(path, model="model", revision="endpoint:1", populated=True)
    assert embedding_index.load_embedding_index(path) == record
    del record["synthetic_fingerprint"]
    assert embedding_index.embedding_change_warning(record, model="model", revision="endpoint:1")


def test_migration_requires_recorded_worker_and_complete_projection(endpoint, monkeypatch):
    from unittest.mock import MagicMock
    from case_intelligence import retrieval_endpoints, retrieval_rebuild

    path = embedding_index.embedding_index_path()
    revision = "b" * 40
    for populated in (False, True):
        embedding_index.record_embedding_index(path, model="synthetic-worker", revision=revision, populated=populated)
    saved = path.read_bytes()
    db = MagicMock(autocommit=True)
    cursor = db.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (True, True)
    health = {"status": "ok", "embedding_loaded": True, "embedding_model": "synthetic-worker", "embedding_revision": "c" * 40}
    monkeypatch.setenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL", "http://localhost")
    worker_calls = []
    from case_intelligence.review_bench_v2 import DeterministicEmbeddingAdapter
    synthetic_vectors = DeterministicEmbeddingAdapter().embed

    def worker(url, route, body=None):
        worker_calls.append(route)
        if route == "/health":
            return health
        assert json.loads(body) == {"texts": list(fingerprints.PROBES)}
        return {"vectors": synthetic_vectors(fingerprints.PROBES)}

    monkeypatch.setattr(retrieval_endpoints, "_worker_json", worker)
    with pytest.raises(ValueError, match="complete populated index"):
        retrieval_rebuild.migrate_index(db)
    assert not worker_calls
    cursor.fetchone.return_value = (True, False)
    legacy = json.loads(saved)
    del legacy["projection_format"]
    path.write_text(json.dumps(legacy))  # fixture-write: older synthetic record of unknown projection format
    with pytest.raises(ValueError, match="known provenance"):
        retrieval_rebuild.migrate_index(db)
    assert embedding_index.load_embedding_index(path) == legacy and not worker_calls
    path.write_bytes(saved)  # fixture-write: restore known producing identity for the next independent check
    with pytest.raises(ValueError, match="revision"):
        retrieval_rebuild.migrate_index(db)
    assert worker_calls == ["/health"]
    assert not endpoint["requests"] and path.read_bytes() == saved
    health["embedding_revision"] = revision
    endpoint["embedding_function"] = lambda texts: synthetic_vectors(list(reversed(texts)))
    with pytest.raises(ValueError, match="fingerprint changed"):
        retrieval_rebuild.migrate_index(db)
    assert path.read_bytes() == saved
    endpoint.update(status=503)
    with pytest.raises(retrieval_endpoints.EndpointFailure):
        retrieval_rebuild.migrate_index(db)
    assert path.read_bytes() == saved
    endpoint.update(status=200, embedding_function=synthetic_vectors, model="another-model")
    with pytest.raises(retrieval_endpoints.EndpointFailure, match="not listed"):
        retrieval_rebuild.migrate_index(db)
    assert path.read_bytes() == saved
    endpoint["model"] = "synthetic"
    retrieval_rebuild.migrate_index(db)
    assert embedding_index.load_embedding_index(path)["projection_format"] == embedding_index.TRANSFER_FORMAT
    migrated = path.read_bytes()
    monkeypatch.delenv("CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL")
    retrieval_rebuild.migrate_index(db)
    assert path.read_bytes() == migrated
    assert retrieval_endpoints.doctor("EMBEDDING") == "ok"
    from case_intelligence.review_bench_v2 import RemoteEmbeddingAdapter
    adapter = RemoteEmbeddingAdapter("").for_matter("matter-a", lambda *args: True)
    assert len(adapter.embed(["Synthetic matter text"])[0]) == 768
    assert endpoint["bodies"][-1]["input"] == ["Synthetic matter text"]
    endpoint["bodies"].clear()

    allowed = iter([True, False])
    revoked = RemoteEmbeddingAdapter("").for_matter("matter-a", lambda *args: next(allowed))
    with pytest.raises(retrieval_endpoints.RetrievalGrantRequired):
        revoked.embed(["Synthetic matter text"])
    assert [body["input"] for body in endpoint["bodies"]] == [list(fingerprints.PROBES)]
    endpoint["embedding_function"] = lambda texts: synthetic_vectors(list(reversed(texts)))
    assert "fingerprint changed" in retrieval_endpoints.doctor("EMBEDDING")
    with pytest.raises(retrieval_endpoints.EndpointFailure, match="fingerprint changed"):
        adapter.embed(["Synthetic matter text"])
    assert all("Synthetic matter text" not in body["input"] for body in endpoint["bodies"])


@contextmanager
def synthetic_worker(model, revision, embed, received):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)  # fixture-write: synthetic embedding service reply

        def do_GET(self):
            assert self.path == "/health"
            self.reply({"status": "ok", "embedding_loaded": True, "embedding_model": model, "embedding_revision": revision})

        def do_POST(self):
            assert self.path == "/embed"
            texts = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["texts"]
            received.append(texts)
            self.reply({"vectors": embed(texts)})

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("options", [("--plan",), ("--confirm-stopped", "--approved-plan", "plan.json")])
def test_migration_command_requires_stopped_writers_without_rebuild_approval(monkeypatch, options):
    import sys
    from case_intelligence import retrieval_rebuild

    monkeypatch.setattr(sys, "argv", ["retrieval_rebuild", "--verify-migration", *options])
    with pytest.raises(SystemExit) as caught:
        retrieval_rebuild.main()
    assert caught.value.code == 2
