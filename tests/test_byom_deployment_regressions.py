"""Synthetic deployment and saved provenance regressions for BYOM."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from case_intelligence import external_generator as endpoints
from case_intelligence.generation import _bounded_json_get

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("port", ["abc", "99999", "-1", "0"])
def test_malformed_port_is_rejected_before_install_or_health(monkeypatch, port):
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: pytest.fail("DNS must not run"))
    url = f"http://127.0.0.1:{port}"
    with pytest.raises(endpoints.EndpointRejected, match="port"):
        endpoints.parse_generator_endpoint("ollama", url, "synthetic")
    assert _bounded_json_get(url + "/api/tags", timeout=1) == {}


def test_compose_app_and_retrieval_share_embedding_identity(monkeypatch, tmp_path):
    from case_intelligence.embedding_index import note_populated_index
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for key in ("CASE_REVIEW_EMBEDDING_MODEL", "CASE_REVIEW_EMBEDDING_REVISION"):
        assert services["app"]["environment"][key] == services["retrieval"]["environment"][key]
    monkeypatch.setenv("CASE_INTELLIGENCE_EMBEDDING_INDEX_RECORD", str(tmp_path / "index.json"))
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_MODEL", "synthetic/first")
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_REVISION", "synthetic-revision-1")
    assert note_populated_index(populated=False) == ""
    assert note_populated_index(populated=True) == ""
    monkeypatch.setenv("CASE_REVIEW_EMBEDDING_MODEL", "synthetic/second")
    assert "must be rebuilt" in note_populated_index(populated=True)


def test_compilation_persists_client_notice_after_model_changes(tmp_path):
    from case_intelligence.report_compilation import compile_report
    from tests.test_report_compilation import material, service
    generator = service()
    generator.client.model_notice = "unvalidated model"
    draft = compile_report("timeline", materials=(material(),), generator=generator)
    generator.client.model_notice = ""
    assert draft.coverage["model_notice"] == "unvalidated model"
    assert "unvalidated model" in draft.sections[-1]["body"]


@pytest.mark.parametrize(("url", "host"), [
    ("http://generator:8000", "generator"),
    ("https://answers.example.test:8000", "answers.example.test"),
])
def test_doctor_checks_server_in_existing_app_namespace(monkeypatch, tmp_path, url, host):
    from tests.test_oss_installer import installer
    app = {"CASE_INTELLIGENCE_GENERATOR_BACKEND": "vllm",
           "CASE_INTELLIGENCE_GENERATOR_URL": url,
           "CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS": host,
           "CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE": "/run/secrets/generator-api-key",
           "CASE_INTELLIGENCE_GENERATOR_MODEL": "synthetic"}
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    commands, output = [], []
    def probe(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "verified", "attempts": 1,
            "detail": "Synthetic schema check", "fix": ""}))
    monkeypatch.setattr(installer, "_probe", probe)
    console = SimpleNamespace(note=output.append, warn=output.append)
    installer._doctor_generator(console, tmp_path, dry_run=True)
    assert commands == []
    installer._doctor_generator(console, tmp_path, dry_run=False)
    assert commands == [["docker", "compose", "exec", "-T", "app", "python", "-m", "case_intelligence.generator_doctor"]]
    assert "running app schema enforcement :: verified" in output


@pytest.mark.parametrize(("saved", "dry_run", "reply", "expected"), [
    (False, False, {}, ""),
    (True, True, {}, "dry run"),
    (True, False, {"transcription": "ok"}, "transcription endpoint :: ok"),
    (True, False, {"transcription": "synthetic-private-runtime-output"}, "diagnostic unavailable"),
])
def test_speech_doctor_runs_only_in_app_and_displays_known_messages(monkeypatch, tmp_path, saved, dry_run, reply, expected):
    from tests.test_oss_installer import installer
    app = {"CASE_INTELLIGENCE_ASR_URL": "https://speech.example.test"} if saved else {}
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    commands, output = [], []

    def probe(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(reply))

    monkeypatch.setattr(installer, "_probe", probe)
    installer._doctor_generator(SimpleNamespace(note=output.append, warn=output.append), tmp_path, dry_run=dry_run)
    expected_commands = [["docker", "compose", "exec", "-T", "app", "python", "-m", "case_intelligence.speech_endpoint"]]
    assert commands == (expected_commands if saved and not dry_run else [])
    assert expected in " ".join(output)
    assert "synthetic-private-runtime-output" not in " ".join(output)


@pytest.mark.parametrize("dry_run", [False, True])
def test_doctor_checks_configured_media_contracts_independently(monkeypatch, tmp_path, dry_run):
    from tests.test_oss_installer import installer
    app = {f"CASE_INTELLIGENCE_{role}_URL": f"https://{role.lower()}.example.test"
           for role in ("ALIGNMENT", "TRANSLATION", "DIARIZATION")}
    app.update({f"CASE_INTELLIGENCE_{role}_MODEL": "synthetic-model" for role in ("ALIGNMENT", "TRANSLATION", "DIARIZATION")})
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    commands, output = [], []
    replies = {"alignment": {"alignment": "ok"}, "translation": {"translation": "unreachable"},
               "speaker_labeling": {"speaker_labeling": "synthetic-private-runtime-output"}}

    def probe(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies[command[-1]]))

    monkeypatch.setattr(installer, "_probe", probe)
    installer._doctor_generator(SimpleNamespace(note=output.append, warn=output.append), tmp_path, dry_run=dry_run)
    command = ["docker", "compose", "exec", "-T", "app", "python", "-m", "case_intelligence.media_endpoint_doctor"]
    assert commands == ([] if dry_run else [[*command, capability] for capability in replies])
    summary = " ".join(output)
    assert "synthetic-private-runtime-output" not in summary
    if dry_run:
        assert "dry run" in summary
    else:
        assert "word alignment endpoint :: ok" in summary
        assert "translation endpoint :: service unreachable" in summary
        assert "speaker labeling endpoint diagnostic unavailable" in summary.lower()


@pytest.mark.parametrize("dry_run", [True, False])
def test_doctor_redacts_invalid_saved_url_before_display(monkeypatch, tmp_path, dry_run):
    from tests.test_oss_installer import installer
    app = {"CASE_INTELLIGENCE_GENERATOR_BACKEND": "vllm",
           "CASE_INTELLIGENCE_GENERATOR_URL": "https://synthetic-key@answers.example.test"}
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_probe", lambda *a, **k: pytest.fail("invalid endpoint was probed"))
    output = []
    installer._doctor_generator(SimpleNamespace(note=output.append, warn=output.append), tmp_path, dry_run=dry_run)
    assert all("synthetic-key" not in line for line in output)
    assert any("not checked" in line for line in output)


def test_packaged_registry_matches_editable_registry():
    from importlib.resources import files
    from case_intelligence.model_qualification import load_qualifications
    for name in ("model_qualifications.json", "model_qualifications.schema.json"):
        assert files("case_intelligence").joinpath("config", name).read_bytes() == (ROOT / "config" / name).read_bytes()
    assert load_qualifications()["schema_version"] == 1


@pytest.mark.parametrize("returncode", [1, 2, -9])
def test_bundled_doctor_timeout_drift_fails_only_for_rc_one(monkeypatch, tmp_path, returncode):
    from dataclasses import asdict
    from case_intelligence import generator_doctor
    from tests.test_oss_installer import installer

    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_TIMEOUT", "311")
    drift = generator_doctor.probe_configured_generator()
    app = {"CASE_INTELLIGENCE_GENERATOR_BACKEND": "vllm",
           "CASE_INTELLIGENCE_GENERATOR_URL": "http://generator:8000"}
    monkeypatch.setattr(installer, "_dotenv", lambda path: {} if path.name == "compose.env" else app)
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    commands, output = [], []

    def probe(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(asdict(drift)))

    monkeypatch.setattr(installer, "_probe", probe)
    console = SimpleNamespace(note=output.append, warn=output.append)
    if returncode == 1:
        with pytest.raises(RuntimeError, match="CASE_INTELLIGENCE_GENERATOR_TIMEOUT"):
            installer._doctor_generator(console, tmp_path, dry_run=False)
        assert drift.detail in output
        assert drift.fix in output
    else:
        installer._doctor_generator(console, tmp_path, dry_run=False)
        assert "schema enforcement :: not checked; the application network probe is unavailable" in output
        assert drift.detail not in output
    assert commands == [["docker", "compose", "exec", "-T", "app", "python", "-m",
                         "case_intelligence.generator_doctor"]]


@pytest.mark.parametrize("stdout", [
    json.dumps({"status": "unreachable", "attempts": 0,
                "detail": "The configured schema check is unavailable.", "fix": "Check configuration."}),
    "not JSON", "{}", "x" * 4097,
])
def test_bundled_doctor_other_rc_one_results_remain_unavailable(monkeypatch, tmp_path, stdout):
    from tests.test_oss_installer import installer
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    monkeypatch.setattr(installer, "_probe", lambda *a, **k: SimpleNamespace(returncode=1, stdout=stdout))
    output = []
    installer._doctor_generator_in_app(SimpleNamespace(note=output.append, warn=output.append), tmp_path)
    assert output == ["schema enforcement :: not checked; the application network probe is unavailable"]


def test_in_container_probe_uses_effective_config_and_runtime_credentials(monkeypatch):
    from case_intelligence import generator_doctor
    from case_intelligence.generator_schema import SchemaProbe
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "http://generator:8000")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "generator")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "vllm")
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_SERVER", raising=False)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_API_KEY", "synthetic-inline-credential")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE", "/synthetic/unreadable")
    seen = []
    def probe(*args, **kwargs):
        seen.append((args, kwargs))
        return SchemaProbe("verified", 1, "Synthetic schema check", "")
    monkeypatch.setattr(generator_doctor, "probe_schema_enforcement", probe)
    assert generator_doctor.probe_configured_generator().status == "verified"
    assert seen[0][0] == ("http://generator:8000", "vllm", "synthetic")
    assert seen[0][1]["headers"] == {"Authorization": "Bearer synthetic-inline-credential"}
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "other.example.test")
    assert generator_doctor.probe_configured_generator().status == "unreachable"
    assert len(seen) == 1


@pytest.mark.parametrize("view", ["assistant", "workspace"])
def test_unsupported_saved_answer_keeps_notices_after_model_change(tmp_path, view):
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.workbench import create_workbench_app
    from tests.test_guided_usability import _matter
    with TestClient(create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test")) as client:
        slug, matter_id, _ = _matter(client)
        store = client.app.state.workbench.workspace
        conversation = store.get_conversation(matter_id)
        # fixture-write: Seed synthetic data for this isolated regression.
        store.append_message(matter_id, conversation.conversation_id, "assistant", "Synthetic missing support", {
            "kind": "not-supported", "missing_information": "Synthetic missing support",
            "model_notice": "unvalidated model", "schema_enforcement": "not enforced / validated locally"})
        path = f"/matters/{slug}/assistant" if view == "assistant" else f"/matters/{slug}"
        page = client.get(path, params={"conversation": conversation.conversation_id})
        assert page.status_code == 200
        assert "Synthetic missing support" in page.text
        assert "unvalidated model" in page.text
        assert "Schema enforcement: not enforced / validated locally." in page.text


def _synthetic_ca_and_server_certs(directory: Path, hostname: str) -> tuple[Path, Path, Path]:
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Internal Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "internal-ca.pem"
    cert_path = directory / "server.crt"
    key_path = directory / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_path.chmod(0o644)
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


def test_external_model_transport_supports_custom_ca_file_through_connect_relay(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import select
    import socket
    import ssl
    import threading
    import urllib.error
    import urllib.request
    from case_intelligence.external_generator import DestinationRejected
    from case_intelligence.generator_transport import open_generator_request

    hostname = "models.example.test"
    ca_path, cert_path, key_path = _synthetic_ca_and_server_certs(tmp_path, hostname)

    class ModelHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"data":[{"id":"synthetic-vllm"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    tls_server = HTTPServer(("127.0.0.1", 0), ModelHandler)
    tls_port = tls_server.server_address[1]
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    tls_server.socket = server_ctx.wrap_socket(tls_server.socket, server_side=True)
    threading.Thread(target=tls_server.serve_forever, daemon=True).start()

    class ConnectRelay(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            upstream = socket.create_connection(("127.0.0.1", tls_port), timeout=5)
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            client_sock = self.connection

            def pump(src, dst):
                try:
                    while True:
                        chunk = src.recv(4096)
                        if not chunk:
                            break
                        dst.sendall(chunk)
                except OSError:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            forward = threading.Thread(target=pump, args=(client_sock, upstream), daemon=True)
            forward.start()
            try:
                pump(upstream, client_sock)
                forward.join(timeout=5)
            finally:
                upstream.close()

        def log_message(self, format, *args):
            return

    relay_server = HTTPServer(("127.0.0.1", 0), ConnectRelay)
    relay_port = relay_server.server_address[1]
    threading.Thread(target=relay_server.serve_forever, daemon=True).start()

    origin = f"https://{hostname}:{tls_port}"
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", origin)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", hostname)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED", "1")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_PROXY", f"http://127.0.0.1:{relay_port}")
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_EGRESS_PINS", raising=False)
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_CA_FILE", raising=False)
    monkeypatch.delenv("CASE_INTELLIGENCE_MODEL_CA_FILE", raising=False)

    try:
        req = urllib.request.Request(f"{origin}/v1/models", method="GET")
        with pytest.raises(urllib.error.URLError):
            open_generator_request(req, timeout=5)

        monkeypatch.setenv("CASE_INTELLIGENCE_MODEL_CA_FILE", str(ca_path))
        with open_generator_request(req, timeout=5) as response:
            assert response.status == 200
            assert b"synthetic-vllm" in response.read()

        # Per-role CA file takes precedence over shared CA file.
        monkeypatch.setenv("CASE_INTELLIGENCE_MODEL_CA_FILE", str(tmp_path / "missing.pem"))
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_CA_FILE", str(ca_path))
        with open_generator_request(req, timeout=5) as response:
            assert response.status == 200

        # Unsafe or missing CA files fail closed.
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_CA_FILE", "relative/ca.pem")
        with pytest.raises(DestinationRejected, match="absolute"):
            open_generator_request(req, timeout=5)

        symlink_ca = tmp_path / "ca-link.pem"
        symlink_ca.symlink_to(ca_path)
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_CA_FILE", str(symlink_ca))
        with pytest.raises(DestinationRejected, match="symbolic links"):
            open_generator_request(req, timeout=5)

        writable_ca = tmp_path / "writable-ca.pem"
        writable_ca.write_bytes(ca_path.read_bytes())
        writable_ca.chmod(0o666)
        monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_CA_FILE", str(writable_ca))
        with pytest.raises(DestinationRejected, match="writable"):
            open_generator_request(req, timeout=5)
    finally:
        relay_server.shutdown()
        relay_server.server_close()
        tls_server.shutdown()
        tls_server.server_close()


def test_health_surfaces_postgres_failure_and_distinguishes_external_asr(tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from case_intelligence.generation import UnavailableGenerator
    from case_intelligence.workbench import create_workbench_app

    secret_dsn = tmp_path / "postgres-dsn"
    secret_dsn.write_text("postgresql://synthetic:synthetic_secret_pw@127.0.0.1:1/exculpata\n", encoding="utf-8")
    secret_dsn.chmod(0o600)
    monkeypatch.setenv("CASE_INTELLIGENCE_POSTGRES_DSN_FILE", str(secret_dsn))
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_URL", "https://asr.example.test:8443")
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_MODEL", "synthetic-whisper")
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_ALLOWED_HOSTS", "asr.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_REMOTE_CONFIRMED", "1")
    monkeypatch.setenv("CASE_INTELLIGENCE_ASR_PROXY", "http://127.0.0.1:3128")

    with caplog.at_level("WARNING"):
        with TestClient(create_workbench_app(tmp_path / "runtime", generator=UnavailableGenerator(), auth_mode="test")) as client:
            health = client.get("/health").json()
            assert health["capabilities"]["postgres"] == "unavailable"
            assert health["capabilities"]["transcription"] == "external speech API"
    assert "PostgreSQL connection failed" in caplog.text
    assert "synthetic_secret_pw" not in caplog.text

