"""Synthetic proxy policy and socket-pair smoke; no live network policy changes."""
import importlib.util
import ipaddress
import json
from pathlib import Path
import socket
import threading
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[1] / "deploy/egress-proxy/proxy.py"
spec = importlib.util.spec_from_file_location("egress_proxy", PATH)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


@pytest.mark.parametrize("case", [
    "http", "url-interpolation", "pins-interpolation", "conflicting-pins",
    "pins-without-url", "invalid-pin-address",
])
def test_generator_configuration_error_names_settings_without_values(case):
    issuer = "https://idp.example.test"
    url = "https://generator.example.test"
    pins = ""
    issuer_pins = ""
    if case == "http":
        url = "http://generator.example.test"
    elif case == "url-interpolation":
        url += "/${X}"
    elif case == "pins-interpolation":
        pins = "${X}"
    elif case == "conflicting-pins":
        url = issuer
        issuer_pins = json.dumps({"issuer": issuer, "addresses": [str(ipaddress.IPv4Address(bytes((10, 1, 2, 3))))]})
        pins = json.dumps({"issuer": issuer, "addresses": [str(ipaddress.IPv4Address(bytes((10, 1, 2, 4))))]})
    elif case == "pins-without-url":
        url, pins = "", "synthetic-pins-without-url"
    elif case == "invalid-pin-address":
        pins = json.dumps({"issuer": url, "addresses": ["synthetic-invalid-pin"]})
    result = subprocess.run(
        [sys.executable, str(PATH)],
        env={"PYTHONPATH": str(PATH.parents[2] / "src"),
             "OIDC_ISSUER": issuer, "OIDC_ISSUER_PINS": issuer_pins,
             "GENERATOR_EGRESS_URL": url, "GENERATOR_EGRESS_PINS": pins},
        capture_output=True, text=True, timeout=3,
    )
    assert result.returncode == 1
    assert result.stderr.startswith(
        "Egress proxy configuration invalid: GENERATOR_EGRESS_URL/GENERATOR_EGRESS_PINS: "
    )
    assert len(result.stderr.splitlines()) == 1
    assert result.stdout == ""
    for value in (issuer, url, pins, issuer_pins, "synthetic-invalid-pin", "${X}"):
        if value:
            assert value not in result.stderr
    if case == "conflicting-pins":
        assert "conflicting authority pins" in result.stderr
        for encoded in (pins, issuer_pins):
            assert json.loads(encoded)["addresses"][0] not in result.stderr


@pytest.mark.parametrize("target", [
    "issuer.example.test.evil.test:443", "evil.test:443", "issuer.example.test:444",
    "issuer.example.test:0", "issuer.example.test:65536", "issuer.example.test.:443",
    "issuer.example.test:443/path", "user@issuer.example.test:443",
    "https://issuer.example.test:443", "127.0.0.1:443", "[::1]:443", "*:443",
])
def test_connect_denies_other_destinations(target):
    class Request:
        data = iter(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
        def recv(self, _):
            return bytes([next(self.data)])
    with pytest.raises(proxy.Denied):
        proxy.read_connect(Request(), ("issuer.example.test", 443))


@pytest.mark.parametrize("issuer", ["https://issuer.example.test:0/realm", "https://issuer.example.test:00000/realm", "http://issuer.example.test", "https://user@issuer.example.test", "https://issuer.example.test?x=1", "https://issuer.example.test/#fragment", "https://issuer.example.test\n", "https://issuer.example.test\\evil"])
def test_bad_issuer(issuer):
    with pytest.raises(proxy.Denied):
        proxy.issuer_authority(issuer)


def test_exact_issuer_and_nondefault_port():
    assert proxy.issuer_authority("https://issuer.example.test:8443/realm") == ("issuer.example.test", 8443)
    assert proxy.issuer_authority("") is None


def answer(ip):
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


@pytest.mark.parametrize("address", ["127.0.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.1", "0.0.0.0", "224.0.0.1", "::1", "2001:db8::1", "64:ff9b::0808:0808"])
def test_dns_denies_private_special_and_mixed_answers(monkeypatch, address):
    monkeypatch.setattr(proxy, "resolve", lambda *a, **k: [answer("8.8.8.8"), answer(address)])
    with pytest.raises(proxy.Denied):
        proxy.public_addresses("issuer.example.test", 443)


@pytest.mark.parametrize("flag", ["is_private", "is_link_local", "is_loopback", "is_reserved", "is_multicast", "is_unspecified"])
def test_special_address_classifications_fail_closed(monkeypatch, flag):
    # Exercise classifications without publishing private network coordinates.
    flags = {key: False for key in ("is_private", "is_link_local", "is_loopback", "is_reserved", "is_multicast", "is_unspecified")}
    flags[flag] = True
    flags["is_global"] = flag in {"is_reserved", "is_multicast"}
    monkeypatch.setattr(proxy, "resolve", lambda *a, **k: [answer("192.0.2.1")])
    monkeypatch.setattr(proxy.ipaddress, "ip_address", lambda value: SimpleNamespace(**flags))
    with pytest.raises(proxy.Denied):
        proxy.public_addresses("issuer.example.test", 443)


def test_ipv4_mapped_documentation_address_is_denied(monkeypatch):
    # Construct the standard IPv4-mapped form of a documentation-only address.
    mapped = ipaddress.IPv6Address((65535 << 32) | int(ipaddress.IPv4Address("192.0.2.1")))
    monkeypatch.setattr(proxy, "resolve", lambda *a, **k: [answer(str(mapped))])
    with pytest.raises(proxy.Denied):
        proxy.public_addresses("issuer.example.test", 443)


# Construct standard special-use test addresses from their protocol components.
# These are synthetic classifications, never deployment coordinates or probes.
SPECIAL_ADDRESSES = [
    pytest.param(ipaddress.IPv4Address(bytes((10, 0, 0, 1))), id="rfc1918-10"),
    pytest.param(ipaddress.IPv4Address(bytes((172, 16, 0, 1))), id="rfc1918-172"),
    pytest.param(ipaddress.IPv4Address(bytes((192, 168, 0, 1))), id="rfc1918-192"),
    pytest.param(ipaddress.IPv4Address(bytes((169, 254, 1, 1))), id="link-local"),
    pytest.param(ipaddress.IPv4Address(bytes((169, 254, 169, 254))), id="metadata"),
    pytest.param(ipaddress.IPv4Address(bytes((100, 100, 100, 200))), id="shared-address-metadata"),
    pytest.param(ipaddress.IPv6Address((0xfe80 << 112) | 1), id="ipv6-link-local"),
    pytest.param(ipaddress.IPv6Address((0xfd00 << 112) | 1), id="ipv6-unique-local"),
    pytest.param(ipaddress.IPv6Address((0xfd00 << 112) | (0xec2 << 96) | 0x254), id="ipv6-aws-metadata"),
    pytest.param(ipaddress.IPv6Address((0xfd20 << 112) | (0xce << 96) | 0x254), id="ipv6-google-metadata"),
    pytest.param(ipaddress.IPv6Address((0x2002 << 112) | (int(ipaddress.IPv4Address("192.0.2.1")) << 80) | 1), id="6to4"),
    pytest.param(ipaddress.IPv6Address((0x2001 << 112) | 1), id="teredo"),
    pytest.param(ipaddress.IPv6Address((0x64 << 112) | (0xff9b << 96) | 1), id="nat64-well-known"),
    pytest.param(ipaddress.IPv6Address((0x64 << 112) | (0xff9b << 96) | (1 << 80) | 1), id="nat64-local-use"),
]


@pytest.mark.parametrize("address", SPECIAL_ADDRESSES)
def test_actual_special_address_semantics_are_denied(monkeypatch, address):
    monkeypatch.setattr(proxy, "resolve", lambda *a, **k: [answer("8.8.8.8"), answer(str(address))])
    with pytest.raises(proxy.Denied):
        proxy.public_addresses("issuer.example.test", 443)


def test_nat64_range_checks_are_subsumed_by_reserved_policy():
    well_known = ipaddress.IPv6Address((0x64 << 112) | (0xff9b << 96) | 1)
    local_use = ipaddress.IPv6Address((0x64 << 112) | (0xff9b << 96) | (1 << 80) | 1)
    assert well_known.is_reserved and local_use.is_reserved
    assert not local_use.is_global


def test_dns_validated_address_is_used_without_second_resolution(monkeypatch):
    calls, connects = [], []
    def resolve(*a, **k):
        calls.append(a)
        return [answer("8.8.8.8")] if len(calls) == 1 else [answer("127.0.0.1")]
    class Connection:
        def settimeout(self, value): pass
        def connect(self, address): connects.append(address)
        def close(self): pass
    monkeypatch.setattr(proxy, "resolve", resolve)
    def open_connection(family, socktype, proto):
        return Connection()

    monkeypatch.setattr(proxy.socket, "socket", open_connection)
    proxy.dial("issuer.example.test", 443)
    assert len(calls) == 1 and connects == [("8.8.8.8", 443)]


@pytest.mark.parametrize("allowed,payload,success", [
    (("issuer.example.test", 443), b"CONNECT issuer.example.test:443 HTTP/1.1\r\nHost: issuer.example.test:443\r\n\r\n", True),
    (("issuer.example.test", 443), b"CONNECT foreign.example.test:443 HTTP/1.1\r\n\r\n", False),
    (None, b"CONNECT issuer.example.test:443 HTTP/1.1\r\n\r\n", False),
    (("issuer.example.test", 443), b"GET https://issuer.example.test HTTP/1.1\r\n\r\n", False),
    (("issuer.example.test", 443), b"CONNECT issuer.example.test:443 HTTP/1.1\r\nHost: evil.test:443\r\n\r\n", False),
    (("issuer.example.test", 443), b"CONNECT issuer.example.test:443 HTTP/1.1\r\nContent-Length: 4\r\n\r\n", False),
])
def test_proxy_positive_negative_socket_smoke(monkeypatch, allowed, payload, success):
    # AF_UNIX socket pairs exercise the actual handler without a host TCP listener.
    client, server = socket.socketpair()
    upstream, peer = socket.socketpair()
    calls = []
    def dial(host, port, pins):
        calls.append((host, port))
        return upstream
    monkeypatch.setattr(proxy, "dial", dial)
    thread = threading.Thread(target=lambda: proxy.Handler(server, None, SimpleNamespace(allowed=allowed, pins=frozenset())))
    thread.start()
    try:
        client.settimeout(3)
        peer.settimeout(3)
        client.sendall(payload)
        response = client.recv(4096)
        assert response.startswith(b"HTTP/1.1 200" if success else b"HTTP/1.1 403")
        if success:
            client.sendall(b"synthetic TLS bytes")
            assert peer.recv(4096) == b"synthetic TLS bytes"
            peer.sendall(b"synthetic reply")
            assert client.recv(4096) == b"synthetic reply"
            assert calls == [allowed]
        else:
            assert calls == []
    finally:
        client.close()
        peer.close()
        thread.join(5)
        server.close()
        upstream.close()
    assert not thread.is_alive()


def test_private_pins_are_exact_and_bound_to_issuer(monkeypatch):
    issuer = "https://issuer.example.test/realm"
    address = str(ipaddress.IPv4Address(bytes((10, 0, 0, 25))))
    encoded = json.dumps({"issuer": issuer, "addresses": [address]})
    pins = proxy.issuer_pins(encoded, issuer)
    monkeypatch.setattr(proxy, "resolve", lambda *a: [answer(address)])
    assert proxy.public_addresses("issuer.example.test", 443, pins) == [answer(address)]
    with pytest.raises(proxy.Denied):
        proxy.public_addresses("issuer.example.test", 443)
    for changed in ("", "https://other.example.test/realm", "https://issuer.example.test/other", "https://issuer.example.test:8443/realm"):
        with pytest.raises(proxy.Denied):
            proxy.issuer_pins(encoded, changed)
    monkeypatch.setattr(proxy, "resolve", lambda *a: [answer(address), answer("8.8.8.8")])
    with pytest.raises(proxy.Denied):
        proxy.public_addresses("issuer.example.test", 443, pins)


@pytest.mark.parametrize("address", SPECIAL_ADDRESSES[3:])
def test_pins_never_authorize_special_destinations(address):
    # ULA is deliberately pinnable; link-local, metadata and transitions are not.
    encoded = json.dumps({"issuer": "https://issuer.example.test", "addresses": [str(address)]})
    if address == ipaddress.IPv6Address((0xfd00 << 112) | 1):
        assert proxy.issuer_pins(encoded, "https://issuer.example.test") == {address}
    else:
        with pytest.raises(proxy.Denied):
            proxy.issuer_pins(encoded, "https://issuer.example.test")


@pytest.mark.parametrize("prefix,service", [(0xfd00, 0xec2), (0xfd20, 0xce)])
def test_metadata_dns_answer_is_denied_even_with_supplied_pin(monkeypatch, prefix, service):
    address = ipaddress.IPv6Address((prefix << 112) | (service << 96) | 0x254)
    monkeypatch.setattr(proxy, "resolve", lambda *a: [answer(str(address))])
    with pytest.raises(proxy.Denied, match="destination address denied"):
        proxy.public_addresses("issuer.example.test", 80, frozenset({address}))


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["::1"], ["0.0.0.0/0"], ["8.8.8.8", "8.8.8.8"], [], ["8.8.8.8"] * 17])
def test_invalid_pins_fail_closed(addresses):
    with pytest.raises(proxy.Denied):
        proxy.issuer_pins(json.dumps({"issuer": "https://issuer.example.test", "addresses": addresses}), "https://issuer.example.test")


def test_dns_child_timeout_is_killed_and_later_request_recovers(monkeypatch):
    real_popen = subprocess.Popen
    children = []
    def launch(*args, **kwargs):
        code = "import time; time.sleep(30)" if not children else "print('[]')"
        child = real_popen([sys.executable, "-I", "-c", code], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(proxy.subprocess, "Popen", launch)
    start = time.monotonic()
    with pytest.raises(proxy.Denied, match="resolver deadline"):
        proxy.resolve("issuer.example.test", 443, start + .1)
    assert time.monotonic() - start < 2
    assert children[0].poll() is not None
    assert proxy.resolve("issuer.example.test", 443, time.monotonic() + 2) == []
    assert children[1].poll() == 0


def test_all_dial_attempts_share_one_deadline(monkeypatch):
    clock, attempts = [0.0], []
    monkeypatch.setattr(proxy.time, "monotonic", lambda: clock[0])
    def resolve(*args):
        clock[0] += 2  # DNS consumes part of the same setup budget.
        return [answer("8.8.8.8")] * 32
    monkeypatch.setattr(proxy, "resolve", resolve)
    class Connection:
        def settimeout(self, value): self.timeout = value
        def connect(self, address):
            attempts.append(self.timeout)
            clock[0] += self.timeout
            raise TimeoutError()
        def close(self): pass
    def open_connection(family, socktype, proto):
        return Connection()

    monkeypatch.setattr(proxy.socket, "socket", open_connection)
    with pytest.raises(proxy.Denied):
        proxy.dial("issuer.example.test", 443)
    assert len(attempts) == 32
    assert sum(attempts) == pytest.approx(3)
    assert clock[0] == pytest.approx(proxy.SETUP_SECONDS)


def test_slot_exhaustion_and_failure_release_without_listener(monkeypatch):
    server = object.__new__(proxy.Server)
    server.slots = threading.BoundedSemaphore(1)
    closed, started = [], []
    monkeypatch.setattr(server, "shutdown_request", lambda request: closed.append(request))
    monkeypatch.setattr(socketserver := proxy.socketserver.ThreadingTCPServer, "process_request",
                        lambda *args: started.append(args[1]))
    server.process_request("first", None)
    server.process_request("exhausted", None)
    assert started == ["first"] and closed == ["exhausted"]
    def failed(*args): raise OSError("synthetic resolver failure")
    monkeypatch.setattr(socketserver, "process_request_thread", failed)
    with pytest.raises(OSError):
        server.process_request_thread("first", None)
    server.process_request("recovered", None)
    assert started == ["first", "recovered"]
