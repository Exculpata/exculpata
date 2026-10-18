"""DNS policy failures must not be confused with direct TCP denial."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import exculpata_install as installer
import dns_boundary_probe as probe
sys.path.pop(0)


@pytest.mark.parametrize("version,allowed", [
    ("25.0.5", False), ("23.0.11", False), ("24.0.9", False),
    ("26.0.0", True), ("28.5.1", True), ("29.5.2", True),
    ("29.5.2+package1", True), ("29.5.2-rc1", False), ("unknown", False),
    ("30.0.0-dev", False), ("29.5", False), ("29.5.2\nprivate-data", False),
    ("26.0.0~rc1", False), ("30.0.0~dev", False), ("29.5.2+vendor~rc1", False),
])
def test_engine_version_enforces_conservative_minimum(monkeypatch, version, allowed):
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0, version, ""))
    assert installer._dns_engine_known_fix() is allowed


def test_engine_probe_failure_is_not_supported(monkeypatch):
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 1, "29.5.2", ""))
    assert not installer._dns_engine_known_fix()


def test_doctor_accepts_only_verified_runtime_and_streams_public_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "_dns_engine_known_fix", lambda: True)
    monkeypatch.setattr(installer, "_installed_release", lambda root: ({"profiles": []}, ROOT))
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    calls = []
    def run(command):
        calls.append(command)
        if "ps" in command:
            value = "\n".join(json.dumps({"Service": name, "ID": name}) for name in ["app", "postgres", "clamav"])
        elif "network" in command:
            value = "true"
        else:
            value = json.dumps({"dns": ["127.0.0.1"], "search": ["."], "networks": {"synthetic-internal": {}}})
        return subprocess.CompletedProcess(command, 0, value, "")
    monkeypatch.setattr(installer, "_probe", run)
    monkeypatch.setattr(installer, "_compose_environment", lambda command: {})
    def streamed(command, **kwargs):
        assert command[-6:] == ["python", "-I", "-", "postgres", "clamav", "egress-proxy"]
        assert kwargs["input"] == (ROOT / "scripts/dns_boundary_probe.py").read_text()
        assert kwargs["timeout"] == 50 and kwargs["capture_output"]
        return subprocess.CompletedProcess(command, 0, json.dumps({"internal_dns_udp_tcp": True, "container_loopback_challenges": 6, "dns_behavior_verified": True}), "")
    monkeypatch.setattr(installer.subprocess, "run", streamed)
    installer._verify_installed_dns(tmp_path)
    assert all(".Config.Env" not in " ".join(command) for command in calls)


@pytest.mark.parametrize("failure", ["dns", "search", "network", "services", "probe"])
def test_doctor_blocks_unknown_dns_runtime(monkeypatch, failure, tmp_path):
    monkeypatch.setattr(installer, "_dns_engine_known_fix", lambda: True)
    monkeypatch.setattr(installer, "_installed_release", lambda root: ({"profiles": []}, ROOT))
    monkeypatch.setattr(installer, "_compose", lambda *args: ["docker", "compose"])
    def run(command):
        if "ps" in command:
            value = json.dumps([{"Service": name, "ID": name} for name in (
                ["app"] if failure == "services" else ["app", "postgres", "clamav"])])
        elif "network" in command:
            value = "false" if failure == "network" else "true"
        else:
            value = json.dumps({"dns": [] if failure == "dns" else ["127.0.0.1"],
                "search": [] if failure == "search" else ["."], "networks": {"synthetic-internal": {}}})
        return subprocess.CompletedProcess(command, 0, value, "")
    monkeypatch.setattr(installer, "_probe", run)
    monkeypatch.setattr(installer, "_compose_environment", lambda command: {})
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "private-diagnostic", ""))
    with pytest.raises(RuntimeError, match="DNS isolation is unverified") as error:
        installer._verify_installed_dns(tmp_path)
    assert "private-diagnostic" not in str(error.value)


def test_negative_dns_requires_live_positive_control(monkeypatch):
    monkeypatch.setattr(probe, "query", lambda *args: (2, 0))
    with pytest.raises(ValueError, match="internal service"):
        probe.verify(["postgres"])


class FakeChallenge:
    def __init__(self, names):
        self.names, self.seen = names, set()
    def close(self): pass


@pytest.mark.parametrize("answer", [(0, 0), (3, 0), (2, 0), (5, 0), (3, 1)])
def test_negative_answers_are_not_behavioral_acceptance(monkeypatch, answer):
    monkeypatch.setattr(probe, "LoopbackChallenge", FakeChallenge)
    monkeypatch.setattr(probe, "query", lambda name, *args: (0, 1) if name == "postgres" else answer)
    with pytest.raises(ValueError, match="challenge"):
        probe.verify(["postgres"])


def test_positive_answer_without_local_observation_also_fails(monkeypatch):
    monkeypatch.setattr(probe, "LoopbackChallenge", FakeChallenge)
    monkeypatch.setattr(probe, "query", lambda *args: (0, 1))
    with pytest.raises(ValueError, match="not observed locally"):
        probe.verify(["postgres"])


def test_udp_tcp_a_aaaa_txt_are_observed_locally(monkeypatch):
    challenges = []
    def factory(names):
        challenge = FakeChallenge(names); challenges.append(challenge); return challenge
    monkeypatch.setattr(probe, "LoopbackChallenge", factory)
    def query(name, kind, protocol):
        if name != "postgres":
            assert challenges[0].names[(protocol, kind)] == name
            challenges[0].seen.add((protocol, kind, name))
        return (0, 1)
    monkeypatch.setattr(probe, "query", query)
    result = probe.verify(["postgres"])
    assert result == {"internal_dns_udp_tcp": True, "container_loopback_challenges": 6, "dns_behavior_verified": True}
    assert len(challenges[0].seen) == 6


def test_challenge_listener_unavailable_fails_closed(monkeypatch):
    monkeypatch.setattr(probe, "query", lambda *a: (0, 1))
    def unavailable(names): raise OSError("synthetic unavailable listener")
    monkeypatch.setattr(probe, "LoopbackChallenge", unavailable)
    with pytest.raises(OSError): probe.verify(["postgres"])


def test_responder_only_records_its_exact_nonce_and_question_type():
    import threading, struct
    challenge = object.__new__(probe.LoopbackChallenge)
    challenge.names = {(protocol, kind): "synthetic-" + str(kind) + ".example.invalid"
                       for protocol in ("udp", "tcp") for kind in (1, 28, 16)}
    challenge.seen, challenge.lock = set(), threading.Lock()
    for (protocol, kind), name in challenge.names.items():
        response = challenge.answer(probe.question(name, kind), protocol)
        assert struct.unpack("!6H", response[:12])[3] == 1
    assert len(challenge.seen) == 6
    challenge.seen.clear()
    assert challenge.answer(b"bad", "udp") is None
    challenge.answer(probe.question("synthetic-foreign.example.invalid", 1), "udp")
    assert not challenge.seen
