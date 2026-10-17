"""Bounded source contract, not a live egress test or an overlay firewall audit."""
from pathlib import Path
import importlib.util
import json
import re
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).parents[1]


def test_reference_compose_network_boundary_and_explicit_exceptions():
    text = (ROOT / "compose.yaml").read_text()
    service_text = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    parts = re.split(r"(?m)^  ([a-z][a-z0-9-]*):\n", service_text)
    services = dict(zip(parts[1::2], parts[2::2], strict=True))
    networks = {
        "postgres": "services", "clamav": "services",
        "clamav-updater": "updates", "retrieval": "models",
        "generator": "models", "transcription-api": "services",
        "app": "app-egress, services, models", "gateway": "front, services",
        "egress-proxy": "app-egress, front",
        "model-stager": "front",
    }
    disconnected = {
        "transcription-worker", "transcription-cleanup",
        "transcription-cleanup-daemon", "account-admin",
    }
    assert set(services) == set(networks) | disconnected
    for name, section in services.items():
        if name in disconnected:
            assert re.findall(r"(?m)^    network_mode: (.+)$", section) == ["none"]
            assert not re.search(r"(?m)^    networks:", section)
        else:
            assert re.findall(r"(?m)^    networks: \[(.+)\]$", section) == [networks[name]]
            assert not re.search(r"(?m)^    network_mode:", section)
        if name != "gateway":
            assert not re.search(r"(?m)^    ports:", section), name
    assert ':-127.0.0.1}}' in services["gateway"]
    network_text = text.split("\nnetworks:\n", 1)[1].split("\nsecrets:\n", 1)[0]
    assert network_text.strip() == "front:\n  updates:\n  services:\n    internal: true\n  models:\n    internal: true\n  app-egress:\n    internal: true"
    for name in ("retrieval", "generator", "transcription-worker"):
        for flag in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            assert f'{flag}: "1"' in services[name], (name, flag)
        assert "target: /models\n        read_only: true" in services[name] or ":/models:ro" in services[name]
    assert 'VLLM_NO_USAGE_STATS: "1"' in services["generator"]
    assert 'PYANNOTE_METRICS_ENABLED: "0"' in services["transcription-worker"]
    assert 'CLAMAV_NO_FRESHCLAMD: "true"' in services["clamav"]
    updater = services["clamav-updater"]
    mounts = updater.split("    volumes:\n", 1)[1].split("    tmpfs:", 1)[0]
    assert mounts.strip() == "- clamav-signatures:/var/lib/clamav"
    assert '"freshclam"' in updater and '"--checks=6"' in updater
    assert "profiles: [tools]" in services["model-stager"]
    assert "profiles: [ai]" in services["generator"]
    assert "profiles: [ai]" in services["retrieval"]


def test_app_has_no_direct_egress():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    app = compose["services"]["app"]
    assert "network_mode" not in app
    assert app["networks"]
    assert all((compose["networks"][name] or {}).get("internal") is True for name in app["networks"])


def test_internal_services_use_container_local_dns_upstream():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    restricted = {"app", "postgres", "clamav", "retrieval", "generator", "transcription-api"}
    for name in restricted:
        service = compose["services"][name]
        assert service.get("dns") == ["127.0.0.1"], name
        assert service.get("dns_search") == ["."], name
        assert all(compose["networks"][network]["internal"] for network in service["networks"])
    for name in {"egress-proxy", "gateway", "model-stager", "clamav-updater"}:
        assert "dns" not in compose["services"][name]


def test_network_smoke_runs_for_merge_groups_and_weekly():
    workflow = yaml.safe_load((ROOT / ".github/workflows/network-smoke.yml").read_text())
    events = workflow[True]  # PyYAML's YAML 1.1 spelling of `on`.
    assert "merge_group" in events
    assert events["schedule"] == [{"cron": "43 6 * * 1"}]
    assert workflow["jobs"]["network-smoke"]["if"] == (
        "github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'network-smoke')"
    )


@pytest.fixture
def contract_tree(tmp_path):
    for name in ("src", "services", "deploy", "config"):
        shutil.copytree(ROOT / name, tmp_path / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for path in ROOT.glob("compose*.yaml"):
        shutil.copy(path, tmp_path / path.name)
    return tmp_path


def run_contract(root):
    # Missing checker is an explicit red-before failure, not a skipped test.
    assert (ROOT / "scripts/network-contract.py").is_file()
    return subprocess.run([sys.executable, str(ROOT / "scripts/network-contract.py"),
                           "--root", str(root)], capture_output=True, text=True, timeout=45)


def test_network_contract_current_tree():
    result = run_contract(ROOT)
    assert result.returncode == 0, result.stderr


def test_network_contract_rejects_loosening(contract_tree):
    path = contract_tree / "compose.yaml"
    path.write_text(path.read_text().replace("  models:\n    internal: true", "  models: {}"))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "compose.yaml:" in result.stderr and "network definitions changed" in result.stderr


@pytest.mark.parametrize("replacement", ["dns: [127.0.0.11]", "dns: []", "dns: [192.0.2.53]"])
def test_network_contract_rejects_dns_upstream_changes(contract_tree, replacement):
    path = contract_tree / "compose.yaml"
    path.write_text(path.read_text().replace("dns: [127.0.0.1]", replacement))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "network membership" in result.stderr


def test_network_contract_rejects_new_outbound_host(contract_tree):
    (contract_tree / "src/synthetic_probe.py").write_text('import httpx\nhttpx.get("https://example.invalid")\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "src/synthetic_probe.py:2:" in result.stderr
    assert "example.invalid" in result.stderr and "outbound client" in result.stderr


@pytest.mark.parametrize("host", ["outbound.example.invalid", "203.0.113.25", "2001:db8::25"])
def test_network_contract_rejects_bare_host_literal(contract_tree, host):
    (contract_tree / "src/synthetic_probe.py").write_text(f'TARGET = "{host}"\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:1: unreviewed outbound host: {host}" in result.stderr


@pytest.mark.parametrize("change", [
    {"networks": ["front"]}, {"network_mode": "host"},
    {"network_mode": "service:gateway"}, {"networks": None},
    {"cap_add": ["NET_ADMIN"]}, {"privileged": True},
    {"ports": [{"target": 8786, "published": "8786", "host_ip": "127.0.0.1"}]},
    {"ports": ["8786:8786"]}, {"extra_hosts": ["issuer.example.test:host-gateway"]},
])
def test_contract_rejects_overlay_escape(contract_tree, change):
    path = contract_tree / "compose.local-accounts.yaml"
    data = yaml.safe_load(path.read_text())
    data["services"]["app"].update(change)
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1 and "compose.local-accounts.yaml:" in result.stderr


@pytest.mark.parametrize("change", [
    {"volumes": ["/var/run/docker.sock:/run/control:ro"]},
    {"volumes": [{"type": "bind", "source": "/run/containerd/containerd.sock", "target": "/run/control"}]},
    {"volumes": ["/:/host:ro"]},
    {"volumes": ["${SYNTHETIC_CONTROL_SOCKET}:/run/control"]},
    {"volumes_from": ["gateway"]},
    {"use_api_socket": True},
])
def test_contract_rejects_host_control_mounts(contract_tree, change):
    path = contract_tree / "compose.local-accounts.yaml"
    data = yaml.safe_load(path.read_text())
    data["services"]["app"].update(change)
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "compose.local-accounts.yaml:" in result.stderr and "mounts or escape capability changed" in result.stderr


@pytest.mark.parametrize("definition", [
    {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/run"}},
    {"external": True, "name": "synthetic-host-control"},
])
def test_contract_rejects_named_volume_host_control(contract_tree, definition):
    path = contract_tree / "compose.yaml"
    data = yaml.safe_load(path.read_text())
    data["volumes"]["clamav-signatures"] = definition
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1 and "volume definitions changed" in result.stderr


@pytest.mark.parametrize("source", [
    'import httpx as h\nh.get(destination)\n',
    'from urllib.request import urlopen as fetch\nfetch(destination)\n',
    'from requests import Session as S\ns = S()\ns.get(destination)\n',
    'import aiohttp\nasync def probe():\n async with aiohttp.ClientSession() as c:\n  await c.get(destination)\n',
    'from socket import create_connection as dial\ndial((destination, 443))\n',
    'import httpx\nclient = httpx.Client()\nget = client.get\nget(destination)\n',
    'import httpx\nclass Sender:\n def __init__(self):\n  self.client = httpx.Client()\n def send(self):\n  return self.client.get(destination)\n',
    'import httpx\nasync def probe(client: httpx.AsyncClient):\n await client.post(destination)\n',
    'not valid Python @\n',
])
def test_contract_rejects_alias_clients_and_invalid_source(contract_tree, source):
    (contract_tree / "src/synthetic_probe.py").write_text(source)
    assert run_contract(contract_tree).returncode == 1


@pytest.mark.parametrize("baseline,addition,detail", [
    ('import httpx as h\ndef probe(destination):\n    pass\n    # NEW\n', 'h.get(destination)', 'httpx.get'),
    ('from urllib.request import urlopen as fetch\ndef probe(destination):\n    pass\n    # NEW\n', 'fetch(destination)', 'urllib.request.urlopen'),
    ('from requests import Session as S\ndef probe(destination):\n    c = S()\n    # NEW\n', 'c.get(destination)', 'requests.Session.get'),
    ('import httpx\nasync def probe(c: httpx.AsyncClient, destination):\n    pass\n    # NEW\n', 'await c.post(destination)', 'httpx.AsyncClient.post'),
    ('import httpx\ndef probe(destination):\n    c = httpx.Client()\n    get = c.get\n    # NEW\n', 'get(destination)', 'httpx.Client.get'),
    ('import httpx\ndef probe(destination):\n    c = httpx.Client()\n    c.get(destination)\n    # NEW\n', 'c.get(destination)', 'httpx.Client.get'),
])
def test_contract_detects_only_new_call_with_approved_imports(contract_tree, baseline, addition, detail):
    path = contract_tree / "src/synthetic_probe.py"
    path.write_text(baseline)
    spec = importlib.util.spec_from_file_location("network_contract", ROOT / "scripts/network-contract.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    policy = contract_tree / "config/egress-allowlist.toml"
    with policy.open("a") as stream:
        for relative, _, kind, digest, description in checker.source_inventory(contract_tree):
            if relative == "src/synthetic_probe.py":
                stream.write('\n[[source]]\n' + '\n'.join(
                    f'{key} = {json.dumps(value)}' for key, value in
                    (("path", relative), ("kind", kind), ("digest", digest), ("detail", description))) + '\n')
    assert run_contract(contract_tree).returncode == 0
    line = baseline.splitlines().index("    # NEW") + 1
    path.write_text(baseline.replace("# NEW", addition))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert result.stderr.strip() == f"src/synthetic_probe.py:{line}: unreviewed outbound client: {detail}"


def test_contract_rejects_new_compose_and_duplicate_keys(contract_tree):
    (contract_tree / "compose.escape.yaml").write_text("services: {app: {network_mode: host}}\n")
    assert run_contract(contract_tree).returncode == 1
    (contract_tree / "compose.escape.yaml").unlink()
    path = contract_tree / "compose.yaml"
    path.write_text(path.read_text() + "\nnetworks: {}\n")
    assert run_contract(contract_tree).returncode == 1


def test_proxy_configuration_and_internal_clients(monkeypatch):
    import httpx
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    env = compose["services"]["app"]["environment"]
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, env[key])
    with httpx.Client() as client:
        issuer = client._transport_for_url(httpx.URL("https://issuer.example.test"))
        assert isinstance(issuer._pool, __import__('httpcore').HTTPProxy)
        assert isinstance(client._transport_for_url(httpx.URL("http://retrieval:8787"))._pool, __import__("httpcore").HTTPProxy)
        assert isinstance(client._transport_for_url(httpx.URL("https://generator:8000"))._pool, __import__("httpcore").HTTPProxy)
    proxy = compose["services"]["egress-proxy"]
    assert set(proxy["environment"]) == {"OIDC_ISSUER", "OIDC_ISSUER_PINS"}
    assert not any(key in proxy for key in ("volumes", "secrets", "env_file", "ports"))
    assert compose["services"]["gateway"]["networks"] == ["front", "services"]
    overlay = yaml.safe_load((ROOT / "compose.kerberos.yaml").read_text())
    assert "services" in overlay["services"]["kerberos-proxy"]["networks"]


@pytest.mark.parametrize('key,value', [('HTTP_PROXY',''),('NO_PROXY','postgres'),('CASE_INTELLIGENCE_OIDC_PROXY','http://foreign.example.test:3128')])
def test_contract_rejects_proxy_routing_overrides(contract_tree, key, value):
    path = contract_tree / 'compose.local-accounts.yaml'
    data = yaml.safe_load(path.read_text())
    data['services']['app'].setdefault('environment', {})[key] = value
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1 and 'compose.local-accounts.yaml:' in result.stderr
