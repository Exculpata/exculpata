"""Opt-in orchestration only; never consulted by application or release code."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time
import uuid

import pytest

from case_intelligence.generator_schema import schema_chat_request
from tests.model_canary_receipt import build_receipt

ROOT = Path(__file__).resolve().parents[1]
CONTROL = "canary-control.example"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=kwargs.pop("timeout", 60),
                          check=kwargs.pop("check", True), **kwargs).stdout


def compose_environment():
    environment = os.environ.copy()
    for suffix in ("CONFIG_ROOT", "TRANSCRIPTION_ROOT", "SECRETS_ROOT", "RUNTIME_ROOT",
                   "STORAGE_ROOT", "STATE_ROOT", "TLS_CERT", "TLS_KEY"):
        environment["EXCULPATA_" + suffix] = "/nonexistent/exculpata-canary-unused"
    return environment


def preflight():
    if os.environ.get("EXCULPATA_MODEL_CANARY") != "1":
        pytest.skip("Set EXCULPATA_MODEL_CANARY=1 to run the live canary")
    try:
        docker("info")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("Docker engine is unavailable")
    try:
        gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                      text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("A usable NVIDIA GPU host is required")
    if not gpu:
        pytest.skip("No GPU found")
    service = json.loads(docker("compose", "-f", str(ROOT / "compose.yaml"),
                                "--profile", "ai", "config", "--no-env-resolution",
                                "--format", "json", env=compose_environment()))["services"]["generator"]
    assert "@sha256:" in service["image"], "Canary requires a digest-pinned image"
    try:
        docker("image", "inspect", service["image"])
    except subprocess.CalledProcessError:
        pytest.skip("Pinned generator image is not present locally")
    runtimes = json.loads(docker("info", "--format", "{{json .Runtimes}}"))
    if service.get("runtime", "nvidia") not in runtimes:
        pytest.skip("Generator GPU runtime is unavailable")
    return service


def isolated_config(service, name):
    generator = {**service, "networks": ["canary"], "restart": "no",
                 "container_name": name + "-generator", "pull_policy": "never"}
    return {"name": name, "services": {"generator": generator},
            "networks": {"canary": {"external": True, "name": name}}}


def capture_result(records, exit_code, generator, client):
    if (exit_code != 0 or not records or records[0] != {"state": "ready"}
            or records[-1].get("state") != "complete" or records[-1].get("drops") != 0
            or [r["state"] for r in records if "state" in r] != ["ready", "control", "complete"]):
        raise ValueError("Incomplete or lossy canary capture")
    hits, dns_seen, tcp_seen, phase = [], False, False, "suite"
    for event in records[1:-1]:
        if event.get("state") == "control":
            phase = "control"
            continue
        if event.get("phase") != phase:
            raise ValueError("Invalid capture phase")
        control = phase == "control" and not event.get("honeytokens")
        dns = (control and event.get("reasons") == ["dns_question"]
               and event.get("dns_name") == CONTROL and event.get("destination_port") == 53)
        tcp = (control and event.get("reasons") == ["tcp_syn"]
               and event.get("source") == generator and event.get("destination") == client
               and event.get("destination_port") == 9)
        dns_seen |= dns
        tcp_seen |= tcp
        if not (dns or tcp):
            hits.append(event)
    return hits, dns_seen, tcp_seen


def wait_state(trap, state):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        records = [json.loads(line) for line in docker("logs", trap).splitlines()]
        if any(record.get("state") == state for record in records):
            return
        assert docker("inspect", "-f", "{{.State.Running}}", trap).strip() == "true", "Trap exited"
        time.sleep(0.2)
    raise TimeoutError("Trap state deadline")


def read_event(client):
    assert select.select([client.stdout], [], [], 310)[0], "Client response deadline"
    line = client.stdout.readline()
    assert line, "Client exited before completing the suite"
    return json.loads(line)


def run_canary(service, tmp_path):
    started = datetime.now(timezone.utc).isoformat()
    suite_bytes = (ROOT / "tests/fixtures/canary/prompts.json").read_bytes()
    suite = json.loads(suite_bytes)
    tokens = [row["honeytoken"] for row in suite]
    command = service["command"]
    model, revision, served = [command[command.index(flag) + 1] for flag in
                               ("--model", "--revision", "--served-model-name")]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    requests = [schema_chat_request("vllm", model=served, schema=schema,
                prompt=row["prompt"], name="canary_answer", max_tokens=1200)
                for _ in range(15) for row in suite]
    name = "exculpata-canary-" + uuid.uuid4().hex
    generator, trap, client_name = [name + suffix for suffix in ("-generator", "-trap", "-client")]
    config_path = tmp_path / "canary-compose.json"
    # fixture-write: isolated synthetic canary Compose configuration.
    config_path.write_text(json.dumps(isolated_config(service, name)).replace("$", "$$"))
    compose = ["compose", "-p", name, "-f", str(config_path), "--profile", "ai"]
    base = ["run", "--pull", "never", "--label", "com.docker.compose.project=" + name,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--entrypoint", "python3"]
    client = None
    trap_started = False
    outputs, records, hits = [], [], []
    dns_seen = tcp_seen = False
    gpu, error, generator_ip, client_ip = "unknown", None, "", ""
    try:
        docker("network", "create", "--internal", "--label", "com.docker.compose.project=" + name, name)
        docker(*compose, "up", "-d", "--no-build", "--pull", "never", "generator", timeout=120)
        deadline = time.monotonic() + 1200
        while True:
            try:
                docker("exec", generator, "python3", "-c",
                       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).close()",
                       timeout=10)
                break
            except subprocess.CalledProcessError:
                assert time.monotonic() < deadline, "Generator health deadline"
                assert docker("inspect", "-f", "{{.State.Running}}", generator).strip() == "true", "Generator exited"
                time.sleep(2)
        gpu = docker("exec", generator, "python3", "-c",
                     "import torch; print(', '.join(torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())))").strip()
        assert gpu, "Generator has no visible GPU"
        generator_ip = json.loads(docker("inspect", generator))[0]["NetworkSettings"]["Networks"][name]["IPAddress"]
        client = subprocess.Popen(["docker", *base, "-i", "--name", client_name,
            "--network", name, "--network-alias", "canary-client", "--user", "65534:65534",
            "--mount", f"type=bind,src={ROOT / 'tests/network_smoke/canary_client.py'},dst=/client.py,readonly",
            service["image"], "-u", "/client.py", generator_ip],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        connection = read_event(client)
        client_ip = json.loads(docker("inspect", client_name))[0]["NetworkSettings"]["Networks"][name]["IPAddress"]
        assert connection["server"] == [generator_ip, 8000] and connection["client"][0] == client_ip
        trap_args = ["--generator-address", generator_ip, "--peer-name", "canary-client",
                     "--answer-connection", generator_ip, "8000", client_ip, str(connection["client"][1])]
        for token in tokens:
            trap_args.extend(["--honeytoken", token])
        docker(*base, "-d", "--name", trap, "--network", "container:" + generator,
               "--user", "0", "--cap-add", "NET_RAW", "--mount",
               f"type=bind,src={ROOT / 'tests/network_smoke/canary_trap.py'},dst=/trap.py,readonly",
               service["image"], "-u", "/trap.py", *trap_args)
        trap_started = True
        wait_state(trap, "ready")
        # fixture-write: send only the synthetic schema requests to the test client.
        client.stdin.write((json.dumps(requests) + "\n").encode())
        for _ in requests:
            event = read_event(client)
            assert isinstance(event["output"], str)
            outputs.append(event["output"])
        assert read_event(client) == {"state": "done"}
        docker("kill", "--signal", "USR1", trap)
        wait_state(trap, "control")
        docker("exec", generator, "python3", "-c",
               "import socket\n"
               "try: socket.getaddrinfo('canary-control.example', 9)\n"
               "except socket.gaierror: pass\n"
               "s=socket.socket(); s.settimeout(3)\n"
               f"assert s.connect_ex(({client_ip!r}, 9)) != 0\n"
               "s.close()", timeout=60)
    except Exception as exc:
        error = type(exc).__name__
    finally:
        if trap_started:
            try:
                docker("kill", "--signal", "TERM", trap, check=False)
                exit_code = int(docker("wait", trap, timeout=30).strip())
                capture = docker("logs", trap)
                # fixture-write: retain the synthetic trap JSONL for inspection.
                (tmp_path / "canary-capture.jsonl").write_text(capture)
                records = [json.loads(line) for line in capture.splitlines()]
                hits, dns_seen, tcp_seen = capture_result(records, exit_code, generator_ip, client_ip)
            except Exception as exc:
                error = type(exc).__name__
        for cleanup in [("rm", "--force", c) for c in (trap, client_name, generator)] + [("network", "rm", name)]:
            try:
                docker(*cleanup)
            except Exception as exc:
                error = error or type(exc).__name__
        if client is not None:
            client.stdin.close()
            client.stdout.close()
            try:
                client.wait(timeout=30)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=10)
                error = error or "ClientCleanupTimeout"
    receipt = build_receipt(model_id=model, revision=revision, served_name=served,
        image_digest=service["image"].split("@", 1)[1], gpu_name=gpu,
        prompt_suite_sha256=hashlib.sha256(suite_bytes).hexdigest(),
        calls_planned=len(requests), calls_completed=len(outputs), hits=hits,
        control_dns_seen=dns_seen, control_tcp_seen=tcp_seen,
        outputs=outputs, honeytokens=tokens, started_at=started,
        ended_at=datetime.now(timezone.utc).isoformat())
    if error is not None:
        receipt.update(status="invalid", upper_bound_95=None, bound_scope=None)
    receipt["capture_error"] = error
    output = Path(os.environ.get("EXCULPATA_CANARY_OUTPUT", str(tmp_path / "canary-receipt.json")))
    # fixture-write: informational synthetic run receipt, never an enablement input.
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt
