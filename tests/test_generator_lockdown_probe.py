"""Offline controls for the container probe and its production-derived smoke graph."""
import errno
import importlib.util
import io
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("networks", "dns", "dns_search", "user", "read_only", "cap_drop", "security_opt", "tmpfs")


def load(relative):
    spec = importlib.util.spec_from_file_location("lockdown_fixture", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe():
    return load("tests/network_smoke/generator_lockdown_probe.py")


@pytest.mark.parametrize("ipv4,ipv6,expected", [
    ("Iface Destination Gateway Flags RefCnt Use Metric Mask\neth0 000012AC 00000000 0001 0 0 0 0000FFFF", "", False),
    ("eth0 00000000 010012AC 0003 0 0 0 00000000", "", True),
    ("eth0 00000000 00000000 0001 0 0 0 000000FF", "", False),
    ("", "0" * 32 + " 00 " + "0" * 32 + " 00 " + "0" * 32 + " 00000000 00000000 00000000 00000001 eth0", True),
    ("", "0" * 32 + " 00 " + "0" * 32 + " 00 " + "0" * 32 + " ffffffff 00000001 00000000 00200200 lo", False),
])
def test_default_routes_fail(probe, ipv4, ipv6, expected):
    assert probe.has_default_route(ipv4, ipv6) is expected


@pytest.mark.parametrize("error,expected", [
    (None, "leak"), (ConnectionRefusedError(errno.ECONNREFUSED, "synthetic"), "blocked"),
    (OSError(errno.ENETUNREACH, "synthetic"), "blocked"), (TimeoutError(), "blocked"),
    (OSError(errno.EINVAL, "synthetic"), "invalid"),
])
def test_connection_result_classes(probe, error, expected):
    assert probe.connection_result(error) == expected


def test_peer_control_required_and_output_bounded(probe):
    results = {"peer_control": True, "default_route": False,
               "targets": {name: "blocked" for name in probe.TARGETS}}
    assert probe.verdict(results) == "passed"
    results["peer_control"] = False
    assert probe.verdict(results) == "invalid"
    del results["peer_control"]
    assert probe.verdict(results) == "invalid"
    results["peer_control"] = True
    results["default_route"] = True
    assert probe.verdict(results) == "leak"
    results["default_route"] = False
    results["targets"][next(iter(probe.TARGETS))] = "leak"
    assert probe.verdict(results) == "leak"
    assert "never leaves" not in json.dumps(results)
    assert "never leaves" not in Path(probe.__file__).read_text().lower()


@pytest.mark.parametrize('reply,expected', [(b'HTTP/1.0 200 OK\r\n', True), (b'', False),
                                           (b'HTTP/1.0 503 Unavailable\r\n', False)])
def test_peer_must_answer(probe, monkeypatch, reply, expected):
    class Peer:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def sendall(self, data): assert data.startswith(b'GET / HTTP/1.0')
        def makefile(self, mode): return io.BytesIO(reply)
    def connect(address, timeout):
        return Peer()
    monkeypatch.setattr(probe.socket, 'create_connection', connect)
    assert probe.peer_answers('retrieval', 8080) is expected


def test_egress_smoke_graph_preserves_generator_settings(monkeypatch, tmp_path):
    smoke = load("scripts/test-egress-smoke.py")
    source = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["generator"]
    graph = smoke.production_graph("synthetic-lockdown", tmp_path)
    standin = graph["services"]["generator-standin"]
    assert {key: standin[key] for key in KEYS} == {key: source[key] for key in KEYS}
    original = smoke.generator_standin

    def mutated(generator, project):
        service = original(generator, project)
        service["dns"] = ["127.0.0.11"]
        return service

    monkeypatch.setattr(smoke, "generator_standin", mutated)
    with pytest.raises(AssertionError):
        smoke.production_graph("synthetic-lockdown", tmp_path)
