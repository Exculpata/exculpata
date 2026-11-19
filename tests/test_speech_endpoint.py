"""Installed-app diagnostic for a synthetic recording through the configured ASR relay."""
import hashlib
import json
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from case_intelligence import speech_endpoint


def transcript():
    return {"text": "The blue marker is beside the empty box.", "language": "english",
            "duration": 5.5, "segments": [{"start": 0.0, "end": 2.1, "text": "The blue marker is beside the empty box."}]}


@pytest.fixture
def provider(monkeypatch, tmp_path):
    state = {"connections": [], "requests": [], "response": json.dumps(transcript()).encode(), "status": 200}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_CONNECT(self):
            state["connections"].append(self.path)
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            wire = ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode()
            message = BytesParser(policy=policy.default).parsebytes(wire + self.rfile.read(int(self.headers["Content-Length"])))
            parts = {part.get_param("name", header="Content-Disposition"): part for part in message.iter_parts()}
            state["requests"].append((self.path, self.headers["Authorization"], parts))
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(state["response"])))
            self.end_headers()
            self.wfile.write(state["response"])  # fixture-write: synthetic endpoint reply

    credential = tmp_path / "credential"
    credential.write_text("synthetic-test-token\n")  # fixture-write: synthetic test credential
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("ssl.SSLContext.wrap_socket", lambda self, sock, **kwargs: sock)
    monkeypatch.setenv("NO_PROXY", "*")
    for key, value in {"URL": "https://speech.example.test", "MODEL": "synthetic-asr", "REMOTE_CONFIRMED": "1",
                       "ALLOWED_HOSTS": "speech.example.test", "PROXY": f"http://127.0.0.1:{server.server_port}",
                       "TIMEOUT": "3", "API_KEY_FILE": str(credential)}.items():
        monkeypatch.setenv("CASE_INTELLIGENCE_ASR_" + key, value)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_doctor_posts_only_packaged_audio(provider):
    assert speech_endpoint.doctor() == "ok"
    assert provider["connections"] == ["speech.example.test:443"]
    [(path, authorization, parts)] = provider["requests"]
    assert (path, authorization) == ("/v1/audio/transcriptions", "Bearer synthetic-test-token")
    assert parts.keys() == {"model", "response_format", "timestamp_granularities[]", "file"}
    assert parts["model"].get_content() == "synthetic-asr"
    assert parts["response_format"].get_content() == "verbose_json"
    assert parts["timestamp_granularities[]"].get_content() == "segment"
    recording = parts["file"]
    assert recording.get_filename() == "diagnostic.wav"
    assert recording.get_content_type() == "audio/wav"
    assert hashlib.sha256(recording.get_payload(decode=True)).hexdigest() == (
        "325e73ecfff901a056d8e08ea55d7222e77ace5b2058459f0d25ae2b344ac90b"
    )


@pytest.mark.parametrize("change", [
    {"segments": []}, {"text": ""}, {"language": ""}, {"segments": [{"text": "synthetic", "start": True, "end": 1}]},
    {"segments": [{"text": "synthetic", "start": 1.0001, "end": 1.0002}]},
    {"segments": [{"text": "synthetic", "start": 1, "end": 0.8}]},
    {"segments": [{"text": "synthetic", "start": -1, "end": 0.8}]},
    {"segments": [{"text": "synthetic", "start": 3, "end": 30}]},
    {"segments": [{"text": "synthetic", "start": 0, "end": float("nan")}]},
    {"segments": [{"text": " ", "start": 0, "end": 1}]},
])
def test_invalid_timed_output_is_rejected(change):
    data = transcript() | change
    assert not speech_endpoint._valid_transcript(data)


@pytest.mark.parametrize("status,response,diagnostic", [
    (200, b'{"error":"synthetic-provider-detail"}', "response"),
    (200, b"synthetic-provider-detail", "response"), (200, b" " * 70000, "response"),
    (401, b"synthetic-provider-detail", "credential"), (503, b"synthetic-provider-detail", "service"),
])
def test_doctor_failures_are_content_free(provider, status, response, diagnostic):
    provider.update(status=status, response=response)
    result = speech_endpoint.doctor()
    assert diagnostic in result.lower()
    assert "synthetic" not in result.lower()
    assert "speech.example.test" not in result


def test_optional_configuration_and_transport_consent(provider, monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_ASR_REMOTE_CONFIRMED")
    assert speech_endpoint.doctor() != "ok"
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_URL", "http://speech.example.test")
    assert "configuration" in speech_endpoint.doctor().lower()
    monkeypatch.delenv("CASE_INTELLIGENCE_ASR_URL")
    assert speech_endpoint.configured_speech_endpoint() is None
    assert "no external" in speech_endpoint.doctor().lower()
    assert provider["connections"] == []


def test_packaged_recording_must_match_its_digest(provider, monkeypatch, tmp_path):
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"synthetic replacement")  # fixture-write: synthetic altered fixture
    monkeypatch.setattr(speech_endpoint, "_RECORDING", replacement)
    assert speech_endpoint.doctor() == speech_endpoint.FIXTURE_UNAVAILABLE
    assert provider["requests"] == []


def test_unreadable_secret_and_unreachable_service(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_API_KEY_FILE", str(tmp_path / "synthetic-missing-key"))
    assert speech_endpoint.doctor() == speech_endpoint.INVALID_CONFIGURATION
    monkeypatch.delenv("CASE_INTELLIGENCE_ASR_API_KEY_FILE")
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_PROXY", "http://127.0.0.1:1")
    assert speech_endpoint.doctor() == speech_endpoint.UNAVAILABLE
    assert provider["requests"] == []
