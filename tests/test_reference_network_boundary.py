"""Bounded source contract, not a live egress test or an overlay firewall audit."""
from pathlib import Path
import re


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
        "app": "front, services, models", "gateway": "front, services",
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
    assert network_text.strip() == "front:\n  updates:\n  services:\n    internal: true\n  models:\n    internal: true"
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
