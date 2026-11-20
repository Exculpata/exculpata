"""Synthetic endpoint HTTP contracts; TLS substitution is confined to this fixture."""
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from case_intelligence import retrieval_endpoints as endpoints
from case_intelligence.embedding_index import record_embedding_index
from case_intelligence.external_model_grants import model_destination
from case_intelligence.review_bench_v2 import RemoteEmbeddingAdapter


def _approved_adapter(adapter, capability, *models):
    approved = {model_destination(capability, "https://models.example.test", model) for model in models}
    return adapter.for_matter("matter", lambda matter_id, destination: matter_id == "matter" and destination in approved)


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    state = {"status": 200, "model": "synthetic", "dimension": 768, "requests": [], "bodies": []}

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
            state["bodies"].append(body)
            rows = ([{"index": i, "embedding": [state.get("component", 0.1)] * state["dimension"]}
                     for i in range(len(body["input"]))] if "input" in body else
                    [{"index": 0 if state.get("repeat_index") else i, "relevance_score": state.get("component", 0.75)}
                     for i in range(len(body["documents"]))])
            if "embedding_function" in state and "input" in body:
                for row, vector in zip(rows, state["embedding_function"](body["input"]), strict=True):
                    row["embedding"] = vector
            self.reply({"data" if "input" in body else "results": rows})

        def reply(self, value):
            assert self.headers["Authorization"] == "Bearer " + state.get("key", "synthetic-secret")
            if state.get("malformed_status"):
                # fixture-write: synthetic malformed HTTP status echoing synthetic request values
                self.wfile.write(b"HTTP/1.1 synthetic-chunk-TEXT synthetic-key\r\n")
                self.close_connection = True
                return
            status = state["status"]
            if "fail_on_post" in state and (self.command != "POST" or len(state["bodies"]) != state["fail_on_post"]):
                status = 200
            data = json.dumps(value if status == 200 else {"detail": "synthetic-secret"}).encode()
            self.send_response(status)
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
                           "ALLOWED_HOSTS": "models.example.test", "TIMEOUT": "5",
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
def test_malformed_status_line_stays_content_free(endpoint, monkeypatch, role):
    endpoint.update(malformed_status=True, key="synthetic-key")
    monkeypatch.setenv(f"CASE_INTELLIGENCE_{role}_API_KEY", "synthetic-key")
    config = endpoints.configured_endpoint(role)
    with pytest.raises(RuntimeError) as failure:
        endpoints.endpoint_batch(config, "embed" if role == "EMBEDDING" else "rerank",
                                 {"texts": ["synthetic-chunk-TEXT"], "query": "Synthetic query"})
    error = failure.value
    expected = "Endpoint unreachable or invalid response; check configuration and relay."
    assert str(error) == expected
    assert error.args == (expected,)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    diagnostic = endpoints.doctor(role)
    assert isinstance(diagnostic, str)
    for value in ("synthetic-chunk-TEXT", "synthetic-key"):
        assert value not in str(error) + repr(error) + diagnostic
    assert endpoint["body"]["input" if role == "EMBEDDING" else "documents"] == ["synthetic-chunk-TEXT"]
    assert endpoint["requests"] == ["models.example.test:443"] * 2

    def escaped_http_error(*args, **kwargs):
        raise http.client.BadStatusLine("HTTP/1.1 synthetic-chunk-TEXT synthetic-key")

    monkeypatch.setattr(endpoints, "request_json", escaped_http_error)
    assert endpoints.doctor(role) == expected


@pytest.mark.parametrize("role", ["EMBEDDING", "RERANK"])
@pytest.mark.parametrize("component", [True, "0.5"], ids=["boolean", "numeric-string"])
def test_boolean_embedding_is_rejected(endpoint, role, component):
    endpoint["component"] = component
    with pytest.raises(RuntimeError) as failure:
        endpoints.endpoint_batch(endpoints.configured_endpoint(role),
                                 "embed" if role == "EMBEDDING" else "rerank",
                                 {"texts": ["Synthetic chunk"], "query": "Synthetic query"})
    assert str(failure.value) == "Invalid retrieval response; check API compatibility."
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True


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
    adapter = _approved_adapter(RemoteEmbeddingAdapter("http://retrieval:8767"), "embedding", "synthetic", "changed")
    assert len(adapter.embed(["Synthetic evidence"])[0]) == 768
    assert endpoint["body"] == {"model": "synthetic", "input": ["Synthetic evidence"]}
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "changed")
    with pytest.raises(RuntimeError, match="rebuilt"):
        adapter.embed(["Synthetic evidence"])
    assert "rebuild" in endpoints.doctor("EMBEDDING").lower()


def test_endpoint_probe_is_independent_of_index(endpoint, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "replacement")
    endpoint["model"] = "replacement"
    config = endpoints.configured_endpoint("EMBEDDING")
    assert endpoints.probe_endpoint(config) == "ok"
    status = endpoints.doctor("EMBEDDING").lower()
    assert "endpoint passed" in status and "rebuild" in status
    assert endpoint["body"] == {"model": "replacement", "input": ["Synthetic diagnostic text"]}
    assert len(endpoint["requests"]) == 4


@pytest.mark.parametrize("populated", [False, True])
def test_legacy_index_identity_cannot_authorize_existing_vectors(endpoint, populated):
    from case_intelligence.embedding_index import embedding_index_path

    config = endpoints.configured_endpoint("EMBEDDING")
    path = embedding_index_path()
    legacy = json.dumps({"model": config.model, "revision": config.identity, "populated": populated})
    path.write_text(legacy)  # fixture-write: synthetic index produced by the earlier projection
    assert record_embedding_index(path, model=config.model, revision=config.identity, populated=True)
    assert path.read_text() == legacy
    with pytest.raises(RuntimeError, match="rebuilt"):
        _approved_adapter(RemoteEmbeddingAdapter(""), "embedding", "synthetic").embed(["Synthetic evidence"])
    assert endpoint["requests"] == []
    assert "rebuild" in endpoints.doctor("EMBEDDING").lower()
    assert record_embedding_index(path, model=config.model, revision=config.identity, populated=False) == ""
    endpoints.require_index_identity(require_record=True)


@pytest.mark.parametrize("role", ["EMBEDDING", "RERANK"])
def test_unconfigured_and_invalid_endpoints_skip_network(endpoint, monkeypatch, role):
    monkeypatch.delenv(f"CASE_INTELLIGENCE_{role}_URL")
    status = endpoints.doctor(role).lower()
    assert "no external endpoint" in status and "not checked" in status
    monkeypatch.setenv(f"CASE_INTELLIGENCE_{role}_URL", "http://models.example.test")
    status = endpoints.doctor(role)
    assert f"CASE_INTELLIGENCE_{role}_" in status and "HTTPS" in status
    assert endpoint["requests"] == []


def test_dimension_mismatch_is_an_endpoint_failure(endpoint):
    endpoint["dimension"] = 3
    diagnosis = endpoints.probe_endpoint(endpoints.configured_endpoint("EMBEDDING"))
    assert "returned" in diagnosis.lower() and "768" in diagnosis
    assert "endpoint passed" not in endpoints.doctor("EMBEDDING").lower()


@pytest.mark.parametrize("role", ["EMBEDDING", "RERANK"])
def test_listed_model_with_invalid_api_response_fails_probe(endpoint, role):
    if role == "RERANK":
        endpoint["repeat_index"] = True
    else:
        endpoint["component"] = "synthetic-invalid-output"
    assert endpoints.probe_endpoint(endpoints.configured_endpoint(role)) == (
        "Invalid retrieval response; check API compatibility."
    )
    assert len(endpoint["requests"]) == 2


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
    adapter = _approved_adapter(RemoteEmbeddingAdapter("http://retrieval:8767"), "embedding", "synthetic")
    with pytest.raises(RuntimeError, match="rebuilt"):
        adapter.embed(["Synthetic evidence"])


@pytest.mark.parametrize("role", ["EMBEDDING", "RERANK"])
def test_proxy_destinations(role):
    from tests.test_egress_proxy import proxy
    from tests.test_byom_egress_rework import WireRequest
    allowed = proxy.configured_destinations({role + "_EGRESS_URL": "https://models.example.test"})
    assert proxy.read_connect(WireRequest(b"CONNECT models.example.test:443 HTTP/1.1\r\n\r\n"), allowed)
    with pytest.raises(proxy.Denied):
        proxy.read_connect(WireRequest(b"CONNECT models.example.test:444 HTTP/1.1\r\n\r\n"), allowed)


def test_rerank_runtime_and_local_switch(endpoint, monkeypatch):
    from case_intelligence.review_bench_v2 import Candidate, RemoteRerankerAdapter
    candidate = Candidate("matter", "document", "chunk", "Synthetic", 1, "Synthetic passage")
    adapter = _approved_adapter(RemoteRerankerAdapter("http://retrieval:8767"), "reranking", "synthetic")
    ranked = adapter.rerank("Synthetic query", [candidate])
    assert ranked[0].chunk_id == candidate.chunk_id and ranked[0].score == 0.75
    assert endpoint["body"]["documents"] == [candidate.text]
    assert endpoints.retrieval_ready("", lambda url: False)
    monkeypatch.delenv("CASE_INTELLIGENCE_EMBEDDING_URL")
    with pytest.raises(RuntimeError, match="rebuilt"):
        endpoints.require_index_identity()
    from case_intelligence.embedding_index import embedding_index_path
    embedding_index_path().unlink()  # fixture-write: simulate lost index provenance
    with pytest.raises(RuntimeError, match="rebuilt"):
        endpoints.require_index_identity(require_record=True)


@pytest.mark.parametrize("identity", ["matching", "changed", "missing"])
def test_disabled_learned_retrieval_never_calls_endpoint(endpoint, monkeypatch, identity):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from case_intelligence.embedding_index import embedding_index_path
    from case_intelligence.pilot_uploads import PilotDocument
    from case_intelligence.review_bench_v2 import RemoteRerankerAdapter
    from case_intelligence.workbench import CaseIntelligenceWorkbench

    if identity == "changed":
        monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_MODEL", "changed")
    elif identity == "missing":
        embedding_index_path().unlink()  # fixture-write: synthetic missing index identity
    bench = object.__new__(CaseIntelligenceWorkbench)
    bench.learned_retrieval = False
    bench.postgres_ready = True
    bench.postgres_connection = MagicMock(autocommit=False)
    bench._postgres_lock = threading.RLock()
    bench.embedding_index_warning = ""
    bench.embedding = RemoteEmbeddingAdapter("http://retrieval:8767")
    bench.reranker = RemoteRerankerAdapter("http://retrieval:8767")
    matter = SimpleNamespace(matter_id="synthetic-matter", display_name="Synthetic matter")
    text = "Synthetic blue bicycle evidence."
    document = PilotDocument("synthetic-document", "Synthetic.txt", "synthetic.txt", "text/plain",
        len(text), "ready", "", [{"number": 1, "text": text}], version_id="synthetic-v1")
    candidate = bench._candidate(matter, document, document.parsed_units()[0], 1)
    monkeypatch.setattr(bench, "_candidates", lambda *_args: (candidate,))

    try:
        stages = []
        results = bench._retriever(matter).search(matter.matter_id, "blue bicycle", stage_callback=stages.append)
        assert stages == ["retrieving", "reranking"]
        assert results and results[0].document_id == document.document_id
        bench._index_document(matter, document)
        cursor = bench.postgres_connection.cursor.return_value.__enter__.return_value
        chunks = [call.args[1] for call in cursor.execute.call_args_list
                  if call.args[0].startswith("INSERT INTO review_chunk(")]
        assert len(chunks) == 1 and chunks[0][5] == text
        assert chunks[0][6] is None
        bench.postgres_connection.commit.assert_called_once()
        if identity != "matching":
            bench.learned_retrieval = True
            bench._index_document(matter, document)
            chunks = [call.args[1] for call in cursor.execute.call_args_list
                      if call.args[0].startswith("INSERT INTO review_chunk(")]
            assert len(chunks) == 2 and chunks[-1][6] is None
    finally:
        assert endpoint["requests"] == [], "Disabled learned retrieval contacted the endpoint"
