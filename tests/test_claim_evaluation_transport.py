"""Synthetic HTTP transport controls; no runtime model or network is exercised."""
import importlib.util
import io
import json
from email.message import Message
from pathlib import Path
import socket
import urllib.request
import urllib.response

import pytest

from case_intelligence import generation


ENDPOINT = "http://127.0.0.1:18439"
MODEL = "synthetic-test-only"
ARTIFACT = "a" * 64
RAW = {"answerable": True, "claims": [{"text": "Team Orchid sent the copper valve to Team Juniper.",
        "evidence_ids": ["S1"]}], "limitation": None, "missing_information": ""}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Synthetic transport tests must not contact a network or model.")
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(urllib.request, "urlopen", fail)


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate-claims.py"
    spec = importlib.util.spec_from_file_location("r1b_transport_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticHTTPHandler(urllib.request.HTTPHandler):
    handler_order = 100

    def __init__(self, backend, *, redirect_stage=None, redirect_code=302):
        super().__init__()
        self.backend = backend
        self.redirect_stage = redirect_stage
        self.redirect_code = redirect_code
        self.requests = []

    def http_open(self, request):
        self.requests.append((request.full_url, request.host, request.get_method()))
        # Any redirected target or inherited proxy route fails this control.
        assert request.full_url.startswith(ENDPOINT + "/")
        assert request.host == "127.0.0.1:18439"
        metadata = request.get_method() == "GET"
        headers = Message()
        code = 200
        if self.redirect_stage == ("metadata" if metadata else "generation"):
            code = self.redirect_code
            headers["Location"] = "http://example.com/redirected-model"
            payload = {}
        elif metadata:
            payload = ({"models": [{"name": MODEL, "digest": ARTIFACT}]}
                       if self.backend == "ollama" else {"data": [{"id": MODEL}]})
        else:
            content = json.dumps(RAW)
            payload = ({"message": {"content": content}} if self.backend == "ollama" else
                       {"choices": [{"message": {"content": content}}]})
        response = urllib.response.addinfourl(io.BytesIO(json.dumps(payload).encode()),
                                             headers, request.full_url, code)
        response.msg = "Synthetic response"
        return response


def install_synthetic_transport(cli, monkeypatch, handler):
    original = cli.evaluation_opener

    def opener():
        result = original()
        result.add_handler(handler)
        return result

    monkeypatch.setattr(cli, "evaluation_opener", opener)


def capture_arguments(tmp_path, backend):
    runtime_path = tmp_path / "synthetic-runtime.json"
    runtime_path.write_text(json.dumps({
        "runtime_name": "synthetic", "runtime_version": "test-only",
        "accelerator": "none", "accelerator_memory_gib": 0, "driver": "none",
        "model_artifact_sha256": ARTIFACT, "offline_readiness": "not_exercised",
        "offline_evidence_sha256": None, "upstream_model_id": "Qwen/Qwen3.5-4B",
        "upstream_revision": "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a", "license": "Apache-2.0",
    }))
    output = tmp_path / "capture.json"
    return ["capture", "--backend", backend, "--endpoint", ENDPOINT,
            "--profile", "portable", "--model", MODEL, "--runtime-profile", str(runtime_path),
            "--repetitions", "1", "--output", str(output)], output


@pytest.mark.parametrize("backend", ["ollama", "openai"])
@pytest.mark.parametrize("stage", ["metadata", "generation"])
@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308, 399])
def test_redirect_is_fatal_without_target_contact_or_receipt(cli, monkeypatch, tmp_path, backend, stage, status):
    handler = SyntheticHTTPHandler(backend, redirect_stage=stage, redirect_code=status)
    install_synthetic_transport(cli, monkeypatch, handler)
    args, output = capture_arguments(tmp_path, backend)
    with pytest.raises(cli.EvaluationBoundaryError, match="redirected"):
        cli.main(args)
    assert not output.exists()
    assert handler.requests
    for url, _, _ in handler.requests:
        assert url.startswith(ENDPOINT + "/")
    assert sum(method == "POST" for _, _, method in handler.requests) == (stage == "generation")


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_structured_local_capture_ignores_ambient_proxies(cli, monkeypatch, tmp_path, backend):
    for variable in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(variable, "http://example.com:8080")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    handler = SyntheticHTTPHandler(backend)
    install_synthetic_transport(cli, monkeypatch, handler)
    args, output = capture_arguments(tmp_path, backend)
    assert cli.main(args) == 0
    receipt = json.loads(output.read_text())
    assert receipt["generation"]["attempts"] == receipt["generation"]["generated"] == 11
    assert receipt["runtime_identity"]["observations"] == 23
    assert sum(method == "POST" for _, _, method in handler.requests) == 11
    assert all(host == "127.0.0.1:18439" for _, host, _ in handler.requests)


@pytest.mark.parametrize("client_type", [generation.OllamaGenerator, generation.OpenAICompatibleGenerator])
def test_default_adapters_preserve_existing_helper_signatures(monkeypatch, client_type):
    # Existing callers and synthetic fixtures need not accept an opener keyword.
    def get(url, *, timeout, headers=None):
        return {"models": [{"name": MODEL}], "data": [{"id": MODEL}]}

    review = {"decision": "include", "rationale": "Synthetic supported result", "evidence_ids": ["S1"]}
    def post(url, payload, *, timeout, headers=None):
        schema = payload.get("format") or payload["response_format"]["json_schema"]["schema"]
        content = json.dumps(review if "decision" in schema["properties"] else RAW)
        return {"message": {"content": content}, "choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(generation, "_bounded_json_get", get)
    monkeypatch.setattr(generation, "_bounded_json_request", post)
    client = client_type(ENDPOINT, MODEL)
    evidence = (generation.EvidenceItem("S1", "Synthetic.txt", "Line 1", RAW["claims"][0]["text"]),)
    assert client.available
    assert client.generate(question="What happened?", evidence=evidence) == RAW
    assert client.classify_source(criterion="Synthetic criterion", include_guidance="",
                                  exclude_guidance="", evidence=evidence) == review


def test_recorded_dispatch_cannot_bypass_injected_transport(cli):
    class Dispatch:
        def send(self, *args):
            pytest.fail("Recorded dispatch must not bypass the selected transport.")

    client = generation.OpenAICompatibleGenerator(ENDPOINT, MODEL, opener=cli.evaluation_opener())
    with pytest.raises(ValueError, match="Recorded dispatch"):
        client.generate(question="Synthetic question", evidence=(), recorded_dispatch=Dispatch())
