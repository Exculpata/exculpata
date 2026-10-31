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


@pytest.fixture
def offline_network(probe, monkeypatch):
    state = {"open_egress": False, "peer_available": True}

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): assert timeout == 3
        def sendall(self, data): assert data.startswith(b'GET / HTTP/1.0')
        def makefile(self, mode): return io.BytesIO(b'HTTP/1.0 200 OK\r\n')
        def send(self, data): return len(data)
        def recv(self, size): return b'synthetic DNS response'

        def connect(self, address):
            if not state["open_egress"]:
                raise OSError(errno.ENETUNREACH, "synthetic blocked egress")

    def connect(address, timeout):
        assert timeout == 3
        if address == ("x", 8080):
            if not state["peer_available"]:
                raise ConnectionRefusedError(errno.ECONNREFUSED, "synthetic missing peer")
        else:
            assert address in probe.TCP_TARGETS.values()
            Socket().connect(address)
        return Socket()

    def udp_socket(family, kind):
        assert (family, kind) == (probe.socket.AF_INET, probe.socket.SOCK_DGRAM)
        return Socket()

    def resolve(command, *, capture_output, timeout):
        assert command[:2] == [probe.sys.executable, "-c"]
        assert "socket.getaddrinfo(" in command[2]
        assert capture_output is True and timeout == 3
        return probe.subprocess.CompletedProcess(command, 0 if state["open_egress"] else 2)

    read_text = Path.read_text

    def routes(path, *args, **kwargs):
        if str(path) == "/proc/net/route":
            return "eth0 00000000 010012AC 0003 0 0 0 00000000" if state["open_egress"] else ""
        if str(path) == "/proc/net/ipv6_route":
            return ""
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(probe.socket, "create_connection", connect)
    monkeypatch.setattr(probe.socket, "socket", udp_socket)
    monkeypatch.setattr(probe.subprocess, "run", resolve)
    monkeypatch.setattr(Path, "read_text", routes)
    monkeypatch.setattr(probe.sys, "argv", ["p", "--peer", "x"])
    monkeypatch.setenv("RES_OPTIONS", "")
    return state


def test_main_open_egress_fails(probe, offline_network, capsys):
    offline_network["open_egress"] = True
    assert probe.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "peer_control": True, "default_route": True, "status": "leak",
        "targets": {name: "leak" for name in probe.TARGETS},
    }


def test_tcp_result_connected_loopback_is_leak(probe):
    with probe.socket.socket(probe.socket.AF_INET, probe.socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert probe.tcp_result(*listener.getsockname()) == "leak"


@pytest.mark.parametrize("check_passes", [True, False])
def test_main_hardening_failure_rejects_isolated_network(
        probe, offline_network, monkeypatch, capsys, check_passes):
    checks = {"nonroot": True, "seccomp": check_passes}
    monkeypatch.setattr(probe.sys, "argv", ["p", "--peer", "x", "--hardening"])
    monkeypatch.setattr(probe, "hardening", lambda: checks)
    assert probe.main() == (0 if check_passes else 1)
    assert json.loads(capsys.readouterr().out) == {
        "peer_control": True, "default_route": False,
        "status": "passed" if check_passes else "invalid", "hardening": checks,
        "targets": {name: "blocked" for name in probe.TARGETS},
    }


def test_main_missing_peer_control_is_invalid(probe, offline_network, capsys):
    offline_network["peer_available"] = False
    assert probe.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "peer_control": False, "default_route": False, "status": "invalid",
        "targets": {name: "blocked" for name in probe.TARGETS},
    }


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
