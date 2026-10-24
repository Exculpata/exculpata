"""Synthetic endpoint HTTP contracts; TLS substitution is confined to this fixture."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from case_intelligence import retrieval_endpoints as endpoints
from case_intelligence.embedding_index import record_embedding_index
from case_intelligence.review_bench_v2 import RemoteEmbeddingAdapter


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    state = {"status": 200, "model": "synthetic", "dimension": 768, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_CONNECT(self):
            state["requests"].append(self.path)
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            self.reply({"data": [{"id": state["model"]}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["body"] = body
            rows = ([{"index": i, "embedding": [0.1] * state["dimension"]}
                     for i in range(len(body["input"]))] if "input" in body else
                    [{"index": i, "relevance_score": 0.75} for i in range(len(body["documents"]))])
            self.reply({"data" if "input" in body else "results": rows})

        def reply(self, value):
            assert self.headers["Authorization"] == "Bearer synthetic-secret"
            data = json.dumps(value if state["status"] == 200 else {"detail": "synthetic-secret"}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)  # fixture-write: synthetic HTTP response only

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("ssl.SSLContext.wrap_socket", lambda self, sock, **kwargs: sock)
    monkeypatch.setenv("NO_PROXY", "*")
    for role in ("EMBEDDING", "RERANK"):
        for key, value in {"URL": "https://models.example.test", "MODEL": "synthetic",
                           "API_KEY": "synthetic-secret", "REMOTE_CONFIRMED": "1",
                           "ALLOWED_HOSTS": "models.example.test", "TIMEOUT": "1",
                           "PROXY": f"http://127.0.0.1:{server.server_port}"}.items():
            monkeypatch.setenv(f"CASE_INTELLIGENCE_{role}_{key}", value)
    path = tmp_path / "identity.json"
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", str(path))
    config = endpoints.configured_endpoint("EMBEDDING")
    record_embedding_index(path, model=config.model, revision=config.identity, populated=False)
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("role", ["EMBEDDING", "RERANK"])
@pytest.mark.parametrize("condition,expected", [("ok", "ok"), ("missing", "model"),
    ("dimension", "rebuild"), (401, "authentication"), (403, "authentication"), ("unreachable", "unreachable")])
def test_doctor(endpoint, monkeypatch, caplog, role, condition, expected):
    if condition == "missing":
        endpoint["model"] = "other"
    elif condition == "dimension":
        endpoint["dimension"] = 3
        if role == "RERANK":
            expected = "ok"  # Rerank returns scores, not vectors.
    elif condition == "unreachable":
        monkeypatch.setenv(f"CASE_INTELLIGENCE_{role}_PROXY", "http://127.0.0.1:1")
    elif isinstance(condition, int):
        endpoint["status"] = condition
    result = endpoints.doctor(role)
    assert expected in result.lower()
    assert "synthetic-secret" not in result + caplog.text
    if condition == "ok":
        assert endpoint["requests"] == ["models.example.test:443"] * 2


def test_runtime_routes_and_refuses_changed_identity(endpoint, monkeypatch):
    adapter = RemoteEmbeddingAdapter("http://retrieval:8767")
    assert len(adapter.embed(["Synthetic evidence"])[0]) == 768
    assert endpoint["body"] == {"model": "synthetic", "input": ["Synthetic evidence"]}
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "changed")
    with pytest.raises(RuntimeError, match="rebuilt"):
        adapter.embed(["Synthetic evidence"])
    assert "rebuild" in endpoints.doctor("EMBEDDING").lower()


def test_default_and_secret_file(endpoint, monkeypatch, tmp_path):
    path = tmp_path / "key"
    path.write_text("unused-file-key\n")  # fixture-write: synthetic credential
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_API_KEY_FILE", str(path))
    assert endpoints.doctor("EMBEDDING") == "ok"
    path.write_text("synthetic-secret\n")  # fixture-write: synthetic credential
    monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_API_KEY")
    assert endpoints.doctor("EMBEDDING") == "ok"
    monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_URL")
    assert endpoints.configured_endpoint("EMBEDDING") is None


@pytest.mark.parametrize("setting,value", [("URL", "http://models.example.test"),
    ("URL", "https://localhost"), ("URL", "https://models.example.test/$MODEL"),
    ("TIMEOUT", "nan"), ("TIMEOUT", "0"), ("MODEL", ""), ("MODEL", "x\ny")])
def test_invalid_configuration(endpoint, monkeypatch, setting, value):
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_" + setting, value)
    with pytest.raises(ValueError):
        endpoints.configured_endpoint("EMBEDDING")


def test_runtime_dimension_failure(endpoint):
    endpoint["dimension"] = 2
    with pytest.raises(RuntimeError, match="rebuilt"):
        RemoteEmbeddingAdapter("http://retrieval:8767").embed(["Synthetic evidence"])


@pytest.mark.parametrize("role", ["EMBEDDING", "RERANK"])
def test_proxy_destinations(role):
    from tests.test_egress_proxy import proxy
    from tests.test_byom_egress_rework import WireRequest
    allowed = proxy.configured_destinations({role + "_EGRESS_URL": "https://models.example.test"})
    assert proxy.read_connect(WireRequest(b"CONNECT models.example.test:443 HTTP/1.1\r\n\r\n"), allowed)
    with pytest.raises(proxy.Denied):
        proxy.read_connect(WireRequest(b"CONNECT models.example.test:444 HTTP/1.1\r\n\r\n"), allowed)


@pytest.mark.parametrize("fail", [False, True])
def test_rebuild_preserves_compound_identity(endpoint, monkeypatch, fail):
    from contextlib import contextmanager, nullcontext
    from case_intelligence.retrieval_rebuild import rebuild
    from case_intelligence.embedding_index import embedding_index_path, load_embedding_index
    rows = [("matter-a", "document-a", "shared-chunk", "Synthetic A"),
            ("matter-b", "document-b", "shared-chunk", "Synthetic B")]
    calls = []

    class Connection:
        autocommit = True
        @contextmanager
        def transaction(self):
            yield
            assert load_embedding_index(embedding_index_path()) is None

        def cursor(self, **kwargs):
            return nullcontext(self)

        def execute(self, sql):
            calls.append(sql)

        def fetchmany(self, size):
            result = rows[:]
            rows.clear()
            return result

        def executemany(self, sql, values):
            assert "matter_id=%s AND document_id=%s AND chunk_id=%s" in sql
            assert [row[1:] for row in values] == [("matter-a", "document-a", "shared-chunk"),
                                                   ("matter-b", "document-b", "shared-chunk")]

    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "rebuilt-model")
    if fail:
        endpoint["status"] = 401
        with pytest.raises(RuntimeError, match="authentication"):
            rebuild(Connection())
        assert load_embedding_index(embedding_index_path())["model"] == "synthetic"
        return
    assert rebuild(Connection()) == 2
    assert "ACCESS EXCLUSIVE" in calls[0]
    record = load_embedding_index(embedding_index_path())
    assert record["model"] == "rebuilt-model" and record["populated"] is True
    assert "synthetic-secret" not in json.dumps(record)


def test_rerank_runtime_and_local_switch(endpoint, monkeypatch):
    from case_intelligence.review_bench_v2 import Candidate, RemoteRerankerAdapter
    candidate = Candidate("matter", "document", "chunk", "Synthetic", 1, "Synthetic passage")
    ranked = RemoteRerankerAdapter("http://retrieval:8767").rerank("Synthetic query", [candidate])
    assert ranked[0].chunk_id == candidate.chunk_id and ranked[0].score == 0.75
    assert endpoint["body"]["documents"] == [candidate.text]
    assert endpoints.retrieval_ready("", lambda url: False)
    monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_URL")
    with pytest.raises(RuntimeError, match="rebuilt"):
        endpoints.require_index_identity()
    from case_intelligence.embedding_index import embedding_index_path
    embedding_index_path().unlink()  # fixture-write: simulate lost index provenance
    with pytest.raises(RuntimeError, match="rebuilt"):
        endpoints.require_index_identity()
