"""Synthetic orchestration checks; no Docker or GPU required."""

import subprocess

import pytest

from tests import model_canary_live as live


def test_opt_in_precedes_external_commands(monkeypatch):
    monkeypatch.delenv("EXCULPATA_MODEL_CANARY", raising=False)
    monkeypatch.setattr(live, "docker", lambda *a, **k: pytest.fail("must not invoke Docker"))
    with pytest.raises(pytest.skip.Exception, match="EXCULPATA_MODEL_CANARY=1"):
        live.preflight()


@pytest.mark.parametrize("missing", ["docker", "image", "gpu", "runtime", None])
def test_missing_prerequisites_skip(monkeypatch, missing):
    monkeypatch.setenv("EXCULPATA_MODEL_CANARY", "1")
    service = {"image": "synthetic@sha256:" + "a" * 64, "runtime": "nvidia"}
    def fake(*args, **kwargs):
        if missing == "docker":
            raise FileNotFoundError("docker")
        if args[:2] == ("compose", "-f"):
            assert missing != "gpu", "GPU absence must skip before config resolution"
            return '{"services": {"generator": ' + live.json.dumps(service) + '}}'
        if args[:2] == ("image", "inspect"):
            if missing == "image":
                raise subprocess.CalledProcessError(1, args)
            return "[]"
        return '{"runc": {}}' if missing == "runtime" else '{"nvidia": {}}'
    def gpu(*args, **kwargs):
        if missing == "gpu":
            raise FileNotFoundError("nvidia-smi")
        return "Synthetic GPU"
    monkeypatch.setattr(live.subprocess, "check_output", gpu)
    monkeypatch.setattr(live, "docker", fake)
    if missing is None:
        assert live.preflight() == service
    else:
        with pytest.raises(pytest.skip.Exception):
            live.preflight()


def test_isolated_config_retains_generator_settings():
    service = dict(image="synthetic", command=["--model", "synthetic"],
                   runtime="nvidia", environment={"HF_HUB_OFFLINE": "1"},
                   volumes=[{"target": "/models", "read_only": True}],
                   tmpfs=["/tmp/cache:exec"], dns=["127.0.0.1"], dns_search=["."],
                   user="1001", security_opt=["no-new-privileges:true"],
                   cap_drop=["ALL"], networks={"models": None}, restart="unless-stopped")
    config = live.isolated_config(service, "synthetic-run")
    resolved = config["services"]["generator"]
    for key in service.keys() - {"networks", "restart"}:
        assert resolved[key] == service[key]
    assert resolved["networks"] == ["canary"]
    assert config["networks"]["canary"] == {"external": True, "name": "synthetic-run"}
    assert service["networks"] == {"models": None}


def capture():
    return [{"state": "ready"}, {"state": "control"},
            {"phase": "control", "reasons": ["dns_question"], "honeytokens": [],
             "dns_name": "canary-control.example", "destination_port": 53},
            {"phase": "control", "reasons": ["tcp_syn"], "honeytokens": [],
             "source": "192.0.2.2", "destination": "192.0.2.3", "destination_port": 9},
            {"state": "complete", "drops": 0}]


def test_control_exclusion_is_narrow():
    records = capture()
    hit = {**records[2], "phase": "suite"}
    other = {**records[3], "destination_port": 443}
    token = {**records[2], "honeytokens": ["cnry-7f3a91"]}
    records[1:1] = [hit]
    records[-1:-1] = [other, token]
    assert live.capture_result(records, 0, "192.0.2.2", "192.0.2.3") == ([hit, other, token], True, True)


@pytest.mark.parametrize("fault", ["exit", "drops", "ready", "complete", "order", "phase"])
def test_capture_integrity_faults_cannot_pass(fault):
    records = capture()
    if fault == "drops":
        records[-1]["drops"] = 1
    elif fault in {"ready", "complete"}:
        records = [r for r in records if r.get("state") != fault]
    elif fault == "order":
        records.reverse()
    elif fault == "phase":
        records[2]["phase"] = "suite"
    with pytest.raises(ValueError, match="capture"):
        live.capture_result(records, int(fault == "exit"), "192.0.2.2", "192.0.2.3")


@pytest.mark.parametrize("fault", [None, "drops", "control", "suite", "trap_exit", "cleanup"])
def test_runner_receipt_and_cleanup(tmp_path, monkeypatch, fault):
    from io import BytesIO
    from types import SimpleNamespace

    calls, requests = [], []
    service = {"image": "synthetic@sha256:" + "a" * 64, "command": [
        "--model", "synthetic", "--revision", "b" * 40, "--served-model-name", "canary"]}
    service["environment"] = {"SYNTHETIC_VALUE": "$literal"}
    def fake_docker(*args, **kwargs):
        calls.append(args)
        if fault == "cleanup" and args[:2] == ("rm", "--force") and args[-1].endswith("-trap"):
            raise subprocess.CalledProcessError(1, args)
        if args[0] == "inspect":
            address = "192.0.2.3" if args[-1].endswith("-client") else "192.0.2.2"
            return live.json.dumps([{"NetworkSettings": {"Networks": {
                "exculpata-canary-synthetic": {"IPAddress": address}}}}])
        if args[:2] == ("exec", "exculpata-canary-synthetic-generator"):
            if "import torch" in args[-1]:
                return "Synthetic GPU"
        if args[0] == "wait":
            return "1" if fault == "trap_exit" else "0"
        if args[0] == "logs":
            requests.extend(live.json.loads(sent.getvalue()))
            records = capture()
            if fault == "drops":
                records[-1]["drops"] = 1
            if fault == "control":
                records.pop(2)
            return "\n".join(live.json.dumps(r) for r in records)
        return ""
    events = iter([{"server": ["192.0.2.2", 8000], "client": ["192.0.2.3", 41000]}]
                  + [{"output": "synthetic"}] * 300 + [{"state": "done"}])
    def read_event(client):
        event = next(events)
        if fault == "suite" and "output" in event:
            raise TimeoutError("Synthetic interrupted suite")
        return event
    sent = BytesIO()
    def wait(timeout):
        return 0
    process = SimpleNamespace(stdin=sent, stdout=BytesIO(), wait=wait)
    def start_client(command, **kwargs):
        calls.append(tuple(command[1:]))
        return process
    monkeypatch.delenv("EXCULPATA_CANARY_OUTPUT", raising=False)
    monkeypatch.setattr(live.uuid, "uuid4", lambda: SimpleNamespace(hex="synthetic"))
    monkeypatch.setattr(live, "docker", fake_docker)
    monkeypatch.setattr(live.subprocess, "Popen", start_client)
    monkeypatch.setattr(live, "read_event", read_event)
    monkeypatch.setattr(live, "wait_state", lambda *args: None)
    receipt = live.run_canary(service, tmp_path)
    config = live.json.loads((tmp_path / "canary-compose.json").read_text())
    assert config["services"]["generator"]["environment"]["SYNTHETIC_VALUE"] == "$$literal"
    assert len(requests) == 300
    assert all(path == "/v1/chat/completions" and "tools" not in body
               and body["response_format"]["json_schema"]["schema"]["required"] == ["answer"]
               for path, body in requests)
    assert receipt["status"] == ("passed" if fault is None else "invalid")
    assert receipt["upper_bound_95"] == (0.01 if fault is None else None)
    if fault == "cleanup":
        assert receipt["control_seen"] is True
    assert live.json.loads((tmp_path / "canary-receipt.json").read_text()) == receipt
    assert [c[0] for c in calls[-4:]] == ["rm", "rm", "rm", "network"]
    assert calls[0][0:3] == ("network", "create", "--internal")
    for command in [c for c in calls if c[0] == "run"]:
        assert command[command.index("--pull") + 1] == "never"
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert "--read-only" in command and "no-new-privileges" in command
    trap = next(c for c in calls if "NET_RAW" in c)
    assert trap[trap.index("--network") + 1].startswith("container:")
    assert trap[trap.index("--user") + 1] == "0"


def test_compose_placeholders_leave_model_settings_intact(monkeypatch):
    monkeypatch.setenv("EXCULPATA_MODEL_ROOT", "/synthetic/models")
    monkeypatch.setenv("EXCULPATA_GENERATOR_REVISION", "a" * 40)
    environment = live.compose_environment()
    assert environment["EXCULPATA_MODEL_ROOT"] == "/synthetic/models"
    assert environment["EXCULPATA_GENERATOR_REVISION"] == "a" * 40
    assert environment["EXCULPATA_TLS_CERT"] == "/nonexistent/exculpata-canary-unused"
