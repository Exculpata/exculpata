"""Opt-in Linux Docker smoke; never silently use an operator's default runtime."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("RUN_NETWORK_SMOKE") != "1", reason="requires explicitly approved disposable Docker runtime")
def test_egress_proxy_denies_non_issuer(tmp_path):
    context = os.environ.get("NETWORK_SMOKE_DOCKER_CONTEXT")
    assert context, "select the approved disposable Docker context explicitly"
    output = Path(os.environ.get("NETWORK_SMOKE_OUTPUT", str(tmp_path / "network-smoke")))
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "scripts/test-egress-smoke.py"),
                             "--docker-context", context, "--output", str(output)],
                            capture_output=True, text=True, timeout=1100)
    assert result.returncode == 0, result.stdout + result.stderr
