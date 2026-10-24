"""Offline regressions for PR 286 against network contract 320."""
import io
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from case_intelligence import external_generator as endpoints, generation
from case_intelligence.generator_transport import open_generator_request
from tests.test_egress_proxy import proxy
from tests.test_oss_installer import installer

ROOT = Path(__file__).resolve().parents[1]
URL = "https://answers.example.test:8443"
PRIVATE = str(ipaddress.IPv4Address((10 << 24) | 7))
PINS = json.dumps({"issuer": URL, "addresses": [PRIVATE]})


class WireRequest:
    def __init__(self, request):
        self.data = io.BytesIO(request)
        self.sent = []

    def recv(self, size):
        return self.data.read(size)

    def settimeout(self, timeout):
        pass

    def sendall(self, value):
        self.sent.append(value)


def destinations(generator=URL, pins=""):
    return proxy.configured_destinations({
        "OIDC_ISSUER": "https://issuer.example.test",
        "GENERATOR_EGRESS_URL": generator,
        "GENERATOR_EGRESS_PINS": pins,
    })


def test_relay_generator_connect_and_oidc_have_separate_pins(monkeypatch):
    allowed = destinations(pins=PINS)
    assert allowed[("answers.example.test", 8443)] == {ipaddress.ip_address(PRIVATE)}
    assert allowed[("issuer.example.test", 443)] == frozenset()
    calls = []
    def dial(host, port, pins):
        calls.append((host, port, pins))
        return io.BytesIO()
    monkeypatch.setattr(proxy, "dial", dial)
    monkeypatch.setattr(proxy, "relay", lambda *args: None)
    request = WireRequest(b"CONNECT answers.example.test:8443 HTTP/1.1\r\n\r\n")
    proxy.Handler(request, None, SimpleNamespace(allowed=allowed, pins=frozenset()))
    assert b"200 Connection Established" in request.sent[0]
    assert calls == [("answers.example.test", 8443, {ipaddress.ip_address(PRIVATE)})]


@pytest.mark.parametrize("wire", [
    b"CONNECT other.example.test:8443 HTTP/1.1\r\n\r\n",
    b"CONNECT answers.example.test:443 HTTP/1.1\r\n\r\n",
    b"GET http://answers.example.test:8443/ HTTP/1.1\r\n\r\n",
])
def test_relay_denies_other_authorities_and_plain_http(wire):
    with pytest.raises(proxy.Denied):
        proxy.read_connect(WireRequest(wire), destinations())


def test_empty_generator_slot_denies():
    with pytest.raises(proxy.Denied):
        proxy.read_connect(WireRequest(b"CONNECT answers.example.test:8443 HTTP/1.1\r\n\r\n"), destinations(""))


def test_generator_dns_needs_exact_reviewed_private_pin(monkeypatch):
    monkeypatch.setattr(proxy, "resolve", lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PRIVATE, 8443))])
    with pytest.raises(proxy.Denied):
        proxy.public_addresses("answers.example.test", 8443, destinations()[("answers.example.test", 8443)])
    assert proxy.public_addresses("answers.example.test", 8443, destinations(pins=PINS)[("answers.example.test", 8443)])


def test_external_transport_connects_only_to_relay_with_tls_identity(monkeypatch):
    import http.client
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED", "1")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: pytest.fail("app must not resolve external DNS"))
    connections = []
    tunnels = []
    wraps = []
    class Socket:
        def setsockopt(self, *args):
            pass
        def sendall(self, data):
            tunnels.append(data)
        def makefile(self, mode, *args):
            return io.BytesIO(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        def close(self):
            pass
    def connect(address, *args, **kwargs):
        connections.append(address)
        assert address == ("egress-proxy", 3128)
        return Socket()
    def wrap(context, sock, *, server_hostname):
        wraps.append((context.check_hostname, context.verify_mode, server_hostname))
        return sock
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr("ssl.SSLContext.wrap_socket", wrap)
    def response(connection):
        return SimpleNamespace(status=200, reason="OK", msg="OK", code=200,
                               headers={}, info=lambda: {}, read=lambda *a: b'{}')
    monkeypatch.setattr(http.client.HTTPSConnection, "getresponse", response)
    result = open_generator_request(urllib.request.Request(URL + "/v1/models"), timeout=1)
    assert result.read() == b'{}'
    assert connections == [("egress-proxy", 3128)]
    assert any(data.startswith(b"CONNECT answers.example.test:8443 HTTP/") for data in tunnels)
    assert wraps == [(True, 2, "answers.example.test")]


def test_bundled_generation_keeps_companion_transport(monkeypatch):
    calls = []
    def companion(request, **kwargs):
        calls.append((request.full_url, kwargs))
        return io.BytesIO(b'{}')
    monkeypatch.setattr(generation, "open_service_request", companion, raising=False)
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: pytest.fail("bundled DNS belongs to companion transport"))
    assert generation._bounded_json_get("http://generator:8000/v1/models", timeout=1) == {}
    assert calls == [("http://generator:8000/v1/models", {"environment_name": "CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "timeout": 1})]


@pytest.mark.parametrize("url", ["http://answers.example.test:11434", "http://127.0.0.1:11434"])
def test_runtime_configuration_rejects_external_http(monkeypatch, url):
    with pytest.raises(ValueError, match="HTTPS|https"):
        endpoints.parse_generator_endpoint("ollama", url, "synthetic")


@pytest.mark.parametrize("url", ["http://answers.example.test:11434", "http://127.0.0.1:11434"])
def test_app_environment_rejects_external_http(monkeypatch, url):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "ollama")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", url)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test")
    monkeypatch.setattr(generation, "_probe_generator", lambda client: None)
    with pytest.raises(ValueError, match="HTTPS|https"):
        generation.generator_from_environment()


@pytest.mark.parametrize("url", [URL, "http://generator:8000"])
def test_https_and_bundled_configuration_accepted(monkeypatch, url):
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: (ipaddress.ip_address(PRIVATE),))
    assert endpoints.parse_generator_endpoint("ollama", url, "synthetic").url == url
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", url)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test,generator")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED", "1")
    monkeypatch.setattr(generation, "_probe_generator", lambda client: None)
    assert generation.generator_from_environment().endpoint == url


def test_installer_noninteractive_rejects_http():
    args = installer._parser().parse_args([
        "install", "--models", "none", "--non-interactive", "--external-generator", "ollama",
        "--external-generator-url", "http://127.0.0.1:11434", "--external-generator-model", "synthetic",
    ])
    with pytest.raises(RuntimeError, match="HTTPS|https"):
        installer._external_from_args(args)


def test_installer_interactive_reprompts_http(monkeypatch, capsys):
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: (ipaddress.ip_address("8.8.8.8"),))
    answers = iter(["yes", "ollama", "http://answers.example.test", "synthetic", "ollama", URL, "synthetic", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    args = installer._parser().parse_args(["install", "--models", "none"])
    endpoint = installer._collect_external_generator(installer.Console(color=False), args)
    assert endpoint.url == URL
    assert "HTTPS" in capsys.readouterr().out


@pytest.mark.parametrize("key", ["CASE_INTELLIGENCE_GENERATOR_MODEL", "EXCULPATA_GENERATOR_ALLOWED_HOSTS"])
def test_generator_dollar_rejected_on_write_and_retained_read(tmp_path, key):
    with pytest.raises(RuntimeError, match=r"\$"):
        installer._env_text({key: "synthetic$PATHvalue"}, "Synthetic")
    path = tmp_path / "synthetic.env"
    # fixture-write: Synthetic retained Compose interpolation must be rejected.
    path.write_text(f'{key}="synthetic$PATHvalue"\n')
    with pytest.raises(RuntimeError, match=r"\$"):
        installer._dotenv(path)


def test_generator_model_dollar_rejected_before_save(monkeypatch):
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: (ipaddress.ip_address(PRIVATE),))
    with pytest.raises(ValueError, match=r"\$"):
        endpoints.parse_generator_endpoint("ollama", URL, "synthetic$PATHmodel")


def test_app_networks_and_extra_hosts_equal_main():
    # Copied from main 347f108 compose.yaml services.app: networks [app-egress, services, models], no extra_hosts.
    main = {"networks": ["app-egress", "services", "models"], "extra_hosts": None}
    app = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["app"]
    assert {key: app.get(key) for key in ("networks", "extra_hosts")} == main


def test_installer_derives_generator_relay_from_selected_file(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    # fixture-write: Synthetic Compose coordinates select one app env file.
    (tmp_path / "compose.env").write_text(installer._env_text({"EXCULPATA_CONFIG_ROOT": str(config)}, "Synthetic"))
    # fixture-write: Synthetic saved generator and reviewed pins for derivation.
    (config / "recordbench.env").write_text(installer._env_text({
        "CASE_INTELLIGENCE_GENERATOR_URL": URL,
        "CASE_INTELLIGENCE_GENERATOR_EGRESS_PINS": PINS,
    }, "Synthetic"))
    monkeypatch.setenv("GENERATOR_EGRESS_URL", "https://unrelated.example.test")
    result = installer._compose_environment(["docker", "compose", "--env-file", str(tmp_path / "compose.env")])
    assert result["GENERATOR_EGRESS_URL"] == URL
    assert result["GENERATOR_EGRESS_PINS"] == PINS


@pytest.mark.parametrize("url", ["", "http://generator:8000"])
def test_empty_or_bundled_slot_discards_shell_generator_authority(tmp_path, monkeypatch, url):
    config = tmp_path / "config"
    config.mkdir()
    # fixture-write: Synthetic app file disables external generator egress.
    (config / "recordbench.env").write_text(installer._env_text({"CASE_INTELLIGENCE_GENERATOR_URL": url}, "Synthetic"))
    # fixture-write: Synthetic Compose configuration selects that app file.
    (tmp_path / "compose.env").write_text(installer._env_text({"EXCULPATA_CONFIG_ROOT": str(config)}, "Synthetic"))
    monkeypatch.setenv("GENERATOR_EGRESS_URL", URL)
    monkeypatch.setenv("GENERATOR_EGRESS_PINS", PINS)
    result = installer._compose_environment(["docker", "compose", "--env-file", str(tmp_path / "compose.env")])
    assert result["GENERATOR_EGRESS_URL"] == result["GENERATOR_EGRESS_PINS"] == ""


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", str(endpoints._METADATA),
    str(ipaddress.IPv4Address((169 << 24) | (254 << 16) | 1))])
def test_generator_pins_never_authorize_denied_addresses(address):
    with pytest.raises(ValueError):
        destinations(pins=json.dumps({"issuer": URL, "addresses": [address]}))


def test_generator_and_oidc_shared_authority_cannot_merge_pins():
    with pytest.raises(proxy.Denied, match="conflicting"):
        proxy.configured_destinations({"OIDC_ISSUER": URL, "GENERATOR_EGRESS_URL": URL, "GENERATOR_EGRESS_PINS": PINS})


@pytest.mark.parametrize("value", ["https://ans$HOSTNAME:8443", "https://answers.example.test/$PATH"])
def test_relay_never_accepts_generator_interpolation(value):
    with pytest.raises(proxy.Denied, match="interpolation"):
        destinations(value)


def test_missing_proxy_and_injected_opener_cannot_enable_direct_transport(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED", "1")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test")
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_PROXY", raising=False)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("no direct connection"))
    request = urllib.request.Request(URL)
    with pytest.raises(endpoints.DestinationRejected, match="PROXY"):
        open_generator_request(request, timeout=1)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_PROXY", "http://egress-proxy:3128")
    opener = SimpleNamespace(open=lambda *a, **k: pytest.fail("no injected bypass"))
    with pytest.raises(endpoints.DestinationRejected, match="CONNECT relay"):
        open_generator_request(request, timeout=1, opener=opener)


def test_installer_private_hostname_requires_reviewed_pins(monkeypatch):
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: (ipaddress.ip_address(PRIVATE),))
    args = installer._parser().parse_args([
        "install", "--models", "none", "--non-interactive", "--external-generator", "ollama",
        "--external-generator-url", URL, "--external-generator-model", "synthetic",
    ])
    with pytest.raises(RuntimeError, match="reviewed"):
        installer._external_from_args(args)
    args.generator_egress_pins = PINS
    assert installer._external_from_args(args).url == URL


def test_app_private_pins_preserve_consent_and_deny_policy(monkeypatch):
    from case_intelligence import generator_transport
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", URL)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_REMOTE_CONFIRMED", "0")
    monkeypatch.setattr(endpoints, "addresses_for", lambda host: pytest.fail("no app DNS"))
    monkeypatch.setattr(generator_transport.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=lambda *a, **k: "tunnel"))
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_EGRESS_PINS", PINS)
    assert open_generator_request(urllib.request.Request(URL), timeout=1) == "tunnel"
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_EGRESS_PINS", json.dumps({"issuer": URL, "addresses": ["8.8.8.8"]}))
    with pytest.raises(endpoints.DestinationRejected, match="outside"):
        open_generator_request(urllib.request.Request(URL), timeout=1)


def test_compose_fixes_generator_relay_and_separate_slot():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    assert services["app"]["environment"]["CASE_INTELLIGENCE_GENERATOR_PROXY"] == "http://egress-proxy:3128"
    assert services["egress-proxy"]["environment"]["GENERATOR_EGRESS_URL"] == "${GENERATOR_EGRESS_URL:-}"
    assert services["egress-proxy"]["environment"]["GENERATOR_EGRESS_PINS"] == "${GENERATOR_EGRESS_PINS:-}"


def test_dollar_rejected_in_app_settings_and_env_update(monkeypatch, tmp_path):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", URL)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic$PATHmodel")
    with pytest.raises(ValueError, match=r"\$"):
        generation.generator_from_environment()
    path = tmp_path / "compose.env"
    # fixture-write: Synthetic literal configuration for update rejection.
    path.write_text('EXCULPATA_GENERATOR_ALLOWED_HOSTS="answers"\n')
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match=r"\$"):
        installer._replace_env(path, "EXCULPATA_GENERATOR_ALLOWED_HOSTS", "ans$HOSTNAME")
    assert path.read_bytes() == before
