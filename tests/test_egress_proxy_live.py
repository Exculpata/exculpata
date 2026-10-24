"""Opt-in Linux Docker smoke; never silently use an operator's default runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.environ.get("RUN_NETWORK_SMOKE") != "1", reason="requires explicitly approved disposable Docker runtime")


@pytest.fixture(scope="module")
def network_smoke_summary(tmp_path_factory):
    context = os.environ.get("NETWORK_SMOKE_DOCKER_CONTEXT")
    assert context, "select the approved disposable Docker context explicitly"
    output = Path(os.environ.get("NETWORK_SMOKE_OUTPUT") or tmp_path_factory.mktemp("network-smoke"))
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "scripts/test-egress-smoke.py"),
                             "--docker-context", context, "--output", str(output)],
                            capture_output=True, text=True, timeout=1100)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((output / "summary.json").read_text())


def test_egress_proxy_denies_non_issuer(network_smoke_summary):
    assert network_smoke_summary['passed'] is True


def test_egress_proxy_generator_slot_live(network_smoke_summary):
    generator = network_smoke_summary['generator']
    for name in ('configured_tunnel', 'other_host_denied', 'wrong_port_denied',
                 'pin_mismatch_denied', 'unconfigured_denied'):
        assert generator[name] is True, name
