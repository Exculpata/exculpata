"""Hosted-review regressions; no live issuer or DNS connections."""
import hashlib
import importlib.util
from pathlib import Path
import socket

import httpx
import pytest


ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = load("compatibility_relay", "deploy/egress-proxy/proxy.py")
contract = load("compatibility_contract", "scripts/network-contract.py")


def test_inventory_fingerprints_keep_empty_fields_across_python_versions(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "example.py").write_text("import httpx\nhttpx.get('https://issuer.example.test')\n")
    call = "Call(func=Attribute(value=Name(id='httpx', ctx=Load()), attr='get', ctx=Load()), args=[Constant(value='https://issuer.example.test')], keywords=[])"
    expected = hashlib.sha256((":" + call).encode()).hexdigest()
    rows = contract.source_inventory(tmp_path)
    assert any(kind == "client" and digest == expected and detail == "httpx.get"
               for _, _, kind, digest, detail in rows)
    (source / "example.py").write_text("import httpx\nhttpx.get('https://issuer.example.test', follow_redirects=True)\n")
    assert not any(kind == "client" and digest == expected
                   for _, _, kind, digest, _ in contract.source_inventory(tmp_path))


@pytest.mark.parametrize("host", ["bücher.example.test", "faß.example.test", "παράδειγμα.example.test", "BÜCHER.example.test"])
def test_unicode_issuer_matches_httpx_ascii_connect_authority(host):
    issuer = "https://" + host + ":8443/realm"
    expected = (httpx.URL(issuer).raw_host.decode("ascii"), 8443)
    assert relay.issuer_authority(issuer) == expected
    assert relay.authority(expected[0] + ":8443") == expected
    assert relay.issuer_authority("https://" + expected[0] + ":8443/realm") == expected


@pytest.mark.parametrize("host", ["☃.example.test", "iss\u200buer.example.test", "issuer\u202e.example.test", "ｅｘａｍｐｌｅ.test"])
def test_invalid_idna_issuer_stays_denied(host):
    with pytest.raises(ValueError):
        relay.issuer_authority("https://" + host)


@pytest.mark.parametrize("host", ["login_eu.example.test", "_identity.example.test", "login_eu_2.example.test"])
def test_existing_ascii_underscore_issuer_matches_transport(host):
    from case_intelligence.identity import OidcSettings
    issuer = "https://" + host + ":8443/realm"
    settings = OidcSettings(issuer=issuer, external_origin="https://app.example.test",
                            client_id="synthetic", client_secret="synthetic-secret")
    expected = (httpx.URL(settings.issuer).raw_host.decode("ascii"), 8443)
    assert relay.issuer_authority(settings.issuer) == expected
    class Request:
        def __init__(self, target):
            self.data = iter(f"CONNECT {target}:8443 HTTP/1.1\r\n\r\n".encode())
        def recv(self, _): return bytes([next(self.data)])
    assert relay.read_connect(Request(host), expected) == expected
    for other in (host + ".evil.test", host.replace("_", "-"), "other.example.test"):
        with pytest.raises(relay.Denied):
            relay.read_connect(Request(other), expected)


def test_stalled_first_address_leaves_budget_for_healthy_fallback(monkeypatch):
    clock, connected, closed = [0.0], [], []
    monkeypatch.setattr(relay.time, "monotonic", lambda: clock[0])
    addresses = ["192.0.2.1", "192.0.2.2"]  # Dial double only; never resolved or contacted.
    answers = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in addresses]
    def validated(*args):
        clock[0] += 1  # DNS shares the same total budget.
        return answers
    monkeypatch.setattr(relay, "public_addresses", validated)
    class Connection:
        def settimeout(self, timeout): self.timeout = timeout
        def connect(self, address):
            connected.append(address)
            if address[0] == addresses[0]:
                clock[0] += self.timeout
                raise TimeoutError()
        def close(self): closed.append(self)
    monkeypatch.setattr(relay.socket, "socket", lambda *args: Connection())
    result = relay.dial("issuer.example.test", 443)
    assert connected == [(ip, 443) for ip in addresses]
    assert len(closed) == 1 and result not in closed
    assert clock[0] < relay.SETUP_SECONDS
