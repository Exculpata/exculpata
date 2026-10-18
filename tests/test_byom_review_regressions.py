"""Synthetic regressions for model transport, schema, qualification and availability."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import threading
import urllib.error
import urllib.request
import zipfile
import io

import pytest

from case_intelligence import external_generator as endpoints
from case_intelligence import generator_completion as completion
from case_intelligence.generation import (
    ANSWER_SCHEMA, GenerationUnavailable, OpenAICompatibleGenerator, _bounded_json_get,
)
from case_intelligence.generator_schema import probe_schema_enforcement
from case_intelligence.generator_transport import open_generator_request
from case_intelligence.model_qualification import apply_harness_result, qualification_label
from case_intelligence.work_product_exports import export_answer, export_full_review
from tests.test_work_product_exports import _records, _review_records


@contextmanager
def server(handler):
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield http.server_port
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.mark.parametrize("address", [
    str(ipaddress.IPv4Address((198 << 24) | (18 << 16) | 1)),
    str(ipaddress.IPv4Address((192 << 24) | 1)),
    str(ipaddress.IPv4Address((240 << 24) | 1)),
    str(ipaddress.IPv6Address((0x2001 << 112) | 1)),
])
def test_special_purpose_ranges_need_consent(address):
    assert not endpoints.host_is_local(address)
    host = f"[{address}]" if ":" in address else address
    parsed = endpoints.parse_generator_endpoint("ollama", f"http://{host}", "synthetic")
    with pytest.raises(endpoints.EndpointRejected, match="outside"):
        endpoints.require_local_or_confirmed(parsed)


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("scope", ["", "%synthetic0", "%25synthetic0"])
def test_ipv6_metadata_cannot_be_confirmed(confirmed, scope, monkeypatch):
    address = ipaddress.ip_address(str(endpoints._METADATA) + scope)
    url = f"http://[{address}]"
    endpoint = endpoints.parse_generator_endpoint("ollama", url, "synthetic", remote_confirmed=confirmed)
    with pytest.raises(endpoints.EndpointRejected, match="metadata"):
        endpoints.require_local_or_confirmed(endpoint)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED", str(int(confirmed)))
    with pytest.raises(endpoints.DestinationRejected, match="metadata"):
        endpoints.require_resolved_destination(url)


def test_dns_deadline_does_not_mutate_socket_defaults(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = socket.getdefaulttimeout()
    def lookup(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return []
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(endpoints, "_DNS_TIMEOUT", 0.02)
    try:
        assert endpoints.addresses_for("synthetic.example.test") == ()
        assert entered.is_set()
        with socket.socket() as unrelated:
            assert unrelated.gettimeout() == original
        assert socket.getdefaulttimeout() == original
    finally:
        release.set()


def test_installer_host_gateway_alias_does_not_exempt_runtime_dns(monkeypatch):
    monkeypatch.setattr(endpoints, "_resolve_addresses", lambda host: ())
    url = "http://" + endpoints.HOST_GATEWAY_NAME + ":11434"
    endpoint = endpoints.parse_generator_endpoint("ollama", url, "synthetic")
    assert endpoints.require_local_or_confirmed(endpoint).local_network
    with pytest.raises(endpoints.DestinationRejected, match="did not resolve"):
        endpoints.require_resolved_destination(url)


def test_failed_dns_and_rejected_destinations_degrade_availability(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "synthetic.example.test")
    client = OpenAICompatibleGenerator("http://synthetic.example.test", "synthetic")
    for addresses in ((), (endpoints._METADATA,)):
        monkeypatch.setattr(endpoints, "_resolve_addresses", lambda host: addresses)
        assert client.available is False
        assert _bounded_json_get(client.endpoint + "/v1/models", timeout=0.1) == {}


def test_transport_uses_approved_numeric_address_and_ignores_proxy(monkeypatch):
    received = []
    class Handler(QuietHandler):
        def do_GET(self):
            received.append(self.headers["Host"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
    with server(Handler) as port:
        lookups = []
        def resolve(host):
            lookups.append(host)
            return (ipaddress.ip_address("127.0.0.1"),) if len(lookups) == 1 else (endpoints._METADATA,)
        monkeypatch.setattr(endpoints, "_resolve_addresses", resolve)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("second DNS lookup"))
        monkeypatch.setenv("http_proxy", "http://proxy.example.test:9")
        request = urllib.request.Request(f"http://synthetic.example.test:{port}/v1/models")
        with open_generator_request(request, timeout=1) as response:
            assert response.read() == b'{}'
    assert lookups == ["synthetic.example.test"]
    assert received == [f"synthetic.example.test:{port}"]


def test_redirect_cannot_forward_authorization(monkeypatch):
    received = []
    class Target(QuietHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
    with server(Target) as target:
        class Redirect(QuietHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target}/stolen")
                self.end_headers()
        with server(Redirect) as origin:
            req = urllib.request.Request(f"http://127.0.0.1:{origin}/", headers={"Authorization": "Bearer synthetic-token"})
            with pytest.raises(urllib.error.HTTPError) as error:
                open_generator_request(req, timeout=1)
            assert error.value.code == 302
    assert received == []


@pytest.mark.parametrize("status,body", [(400, b'{}'), (404, b'{}'), (200, b'not json')])
def test_schema_rejection_is_not_reported_as_unreachable(status, body):
    class Handler(QuietHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)
    with server(Handler) as port:
        result = probe_schema_enforcement(f"http://127.0.0.1:{port}", "llamacpp", "synthetic", timeout=1)
    assert result.status == "refused"
    assert result.attempts == 3


def test_schema_refresh_returns_cached_status_while_one_probe_runs(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def probe(client):
        calls.append(client)
        entered.set()
        assert release.wait(3)
        completion.remember_schema(client, completion.SchemaProbe("verified", 1, "synthetic", ""))
        finished.set()
    client = OpenAICompatibleGenerator("http://127.0.0.1", "synthetic", schema_mode="verified")
    monkeypatch.setattr(completion, "probe_generator", probe)
    try:
        completion.refresh_generator_schema(client)
        assert entered.wait(1)
        for _ in range(20):
            completion.refresh_generator_schema(client)
        assert calls == [client]
        assert client.schema_mode == "verified"
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.parametrize("mode", ["verified", "validate_and_retry", "unreachable"])
def test_recorded_context_schema_repairs_use_dispatch_receipts(mode):
    client = OpenAICompatibleGenerator("http://127.0.0.1", "synthetic", schema_mode=mode)
    valid = dict(answerable=False, claims=[], limitation=None, missing_information="Synthetic missing information")
    class Dispatch:
        def __init__(self):
            self.calls = []
        def send(self, client, request, repair):
            self.calls.append((request, repair))
            payload = {**valid, "extra": True} if len(self.calls) == 1 else valid
            return {"choices": [{"message": {"content": json.dumps(payload)}}]}
    dispatch = Dispatch()
    assert client.generate(question="Synthetic question", evidence=(), recorded_dispatch=dispatch) == valid
    assert len(dispatch.calls) == 2
    assert dispatch.calls[0][1] is False
    assert dispatch.calls[1][1] is True
    assert "previous reply" in dispatch.calls[1][0]["messages"][-1]["content"]


def test_verified_schema_still_refuses_repeated_invalid_replies():
    client = OpenAICompatibleGenerator("http://127.0.0.1", "synthetic", schema_mode="verified")
    calls = []
    def send(attempt):
        calls.append(attempt)
        return {"unexpected": "synthetic"}
    with pytest.raises(GenerationUnavailable, match="JSON schema"):
        completion.complete_structured(client, send, lambda payload: payload, ANSWER_SCHEMA)
    assert calls == [0, 1, 2]


def test_mutable_model_alias_never_inherits_an_unattested_pass():
    revision = "sha256:" + "a" * 64
    registry = apply_harness_result({}, model_id="synthetic:latest", harness="synthetic", passed=True, revision=revision)
    assert qualification_label("synthetic:latest", registry=registry) == "unvalidated model"
    assert qualification_label("synthetic:latest", registry=registry, revision=revision) == ""
    assert qualification_label("synthetic:latest", registry=registry, revision="sha256:" + "b" * 64) == "unvalidated model"
    with pytest.raises(ValueError, match="immutable"):
        apply_harness_result({}, model_id="synthetic:latest", harness="synthetic", passed=True)


@pytest.mark.parametrize("format_name", ["csv", "json", "markdown", "docx"])
def test_every_source_exports_keep_unattested_model_label(format_name):
    matter, *_ = _records()
    criterion, version, run, decision, metrics = _review_records(matter)
    artifact = export_full_review(matter, criterion, version, run, (decision,), metrics, format_name)
    body = artifact.body
    if format_name == "docx":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            body = archive.read("word/document.xml")
    assert b"unvalidated model" in body


@pytest.mark.parametrize("format_name", ["markdown", "docx"])
def test_rejected_model_answers_keep_qualification_label(format_name):
    matter, conversation, question, answer = _records()
    answer = replace(answer, payload={"kind": "not-supported", "model_notice": "unvalidated model", "missing_information": "Synthetic missing support"})
    artifact = export_answer(matter, conversation, answer, question, format_name)
    body = artifact.body
    if format_name == "docx":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            body = archive.read("word/document.xml")
    assert b"unvalidated model" in body


@pytest.mark.parametrize("saved,shell", [("1", None), ("0", "1")])
def test_doctor_schema_probe_uses_saved_consent_and_restores_shell(monkeypatch, tmp_path, saved, shell):
    import os
    from types import SimpleNamespace
    from tests.test_oss_installer import installer
    from case_intelligence.generator_schema import SchemaProbe
    key = "CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED"
    if shell is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, shell)
    app = {
        "CASE_INTELLIGENCE_GENERATOR_BACKEND": "openai",
        "CASE_INTELLIGENCE_GENERATOR_SERVER": "llamacpp",
        "CASE_INTELLIGENCE_GENERATOR_URL": "http://api.example.test",
        "CASE_INTELLIGENCE_GENERATOR_MODEL": "synthetic",
        "CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS": "api.example.test",
        key: saved,
    }
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_print_hardware_suggestion", lambda console: None)
    observed = []
    def probe(*args, **kwargs):
        observed.append(os.environ.get(key))
        return SchemaProbe("verified", 1, "Synthetic probe", "")
    monkeypatch.setattr(installer, "probe_schema_enforcement", probe)
    console = SimpleNamespace(note=lambda text: None, warn=lambda text: None)
    installer._doctor_generator(console, tmp_path, dry_run=False)
    assert observed == [saved]
    assert os.environ.get(key) == shell


def test_installer_imports_and_preflight_need_only_the_standard_library(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-S", str(root / "scripts/exculpata_install.py"),
         "preflight", "--root", str(tmp_path / "uncreated"), "--models", "none", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode in (0, 1), result.stderr
    assert result.stdout, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["checks"]
    assert not (tmp_path / "uncreated").exists()


def test_selecting_local_generator_clears_previous_remote_consent(tmp_path):
    from tests.test_oss_installer import installer
    app = {"CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED": "1"}
    args = installer._parser().parse_args([
        "install", "--models", "none", "--non-interactive", "--external-generator", "ollama",
        "--external-generator-url", "http://127.0.0.1:11434", "--external-generator-model", "synthetic",
    ])
    installer._apply_external_generator(args, app, {}, {"secrets": tmp_path})
    assert app["CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED"] == "0"
