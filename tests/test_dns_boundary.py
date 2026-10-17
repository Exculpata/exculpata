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
def test_engine_floor_is_conservative_and_value_free(monkeypatch, version, allowed):
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0, version, ""))
    assert installer._dns_engine_supported() is allowed


def test_engine_probe_failure_is_not_supported(monkeypatch):
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 1, "29.5.2", ""))
    assert not installer._dns_engine_supported()


def test_doctor_accepts_only_verified_runtime_and_streams_public_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "_dns_engine_supported", lambda: True)
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
        return subprocess.CompletedProcess(command, 0, "{}", "")
    monkeypatch.setattr(installer.subprocess, "run", streamed)
    installer._verify_installed_dns(tmp_path)
    assert all(".Config.Env" not in " ".join(command) for command in calls)


@pytest.mark.parametrize("failure", ["version", "dns", "search", "network", "services", "probe"])
def test_doctor_blocks_unknown_dns_runtime(monkeypatch, failure, tmp_path):
    monkeypatch.setattr(installer, "_dns_engine_supported", lambda: failure != "version")
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


@pytest.mark.parametrize("answer", [(0, 1), (0, 0), (3, 1)])
def test_external_answer_is_not_denial(monkeypatch, answer):
    monkeypatch.setattr(probe, "query", lambda name, *args: (0, 1) if name == "postgres" else answer)
    with pytest.raises(ValueError, match="outside DNS"):
        probe.verify(["postgres"])


def test_udp_tcp_a_aaaa_txt_are_all_probed(monkeypatch):
    calls = []
    def query(name, kind, protocol):
        calls.append((name, kind, protocol))
        return (0, 1) if name == "postgres" else (2, 0)
    monkeypatch.setattr(probe, "query", query)
    result = probe.verify(["postgres"])
    assert result["internal_dns_udp_tcp"]
    assert {(kind, protocol) for name, kind, protocol in calls if name != "postgres"} == {
        (kind, protocol) for kind in (1, 28, 16) for protocol in ("udp", "tcp")}
    assert all(name.endswith(".example.invalid") for name, _, _ in calls if name != "postgres")
