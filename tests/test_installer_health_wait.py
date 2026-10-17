"""Synthetic clock and health responses; no host services or real waits."""
import io
import json
import re

import pytest

from case_intelligence.legacy_names import CURRENT_PRODUCT, LEGACY_PRODUCT
from tests.test_first_run_handoff import configured_node
from tests.test_oss_installer import installer


@pytest.mark.parametrize("product_name", [CURRENT_PRODUCT, LEGACY_PRODUCT])
@pytest.mark.parametrize("outcome", ["ready", "timeout", "incomplete"])
def test_health_wait_is_calm_bounded_and_retains_final_reason(tmp_path, monkeypatch, capsys, outcome, product_name):
    root, _, _ = configured_node(tmp_path)
    clock = [0.0]
    probes = []
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(installer, "_running_auth_mode", lambda *a: "local")
    monkeypatch.setattr(installer, "_login_reachable", lambda *a: True)

    def health(*args, **kwargs):
        probes.append(clock[0])
        if outcome == "timeout" or clock[0] < 96:
            clock[0] += 5  # Model a real probe reaching its transport timeout.
            raise TimeoutError("synthetic private transport detail")
        payload = {"product": product_name, "status": "ok",
                   "storage": {"status": "ready"},
                   "capabilities": {"source_review": "ready", "malware_scan": "ready"}}
        if outcome == "incomplete":
            payload["storage"] = {"status": "unavailable", "reserve_satisfied": False}
            payload["capabilities"]["malware_scan"] = "unavailable"
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(installer.urllib.request, "urlopen", health)
    console = installer.Console(color=False)
    if outcome == "ready":
        installer._wait_health(console, root)
        assert clock[0] == 96
        assert installer._install_progress(root)["phases"]["selected_capabilities"] == "complete"
    else:
        with pytest.raises(RuntimeError, match="node did not reach health before timeout") as error:
            installer._wait_health(console, root)
        assert 900 <= clock[0] < 908
        assert all(probe < 900 for probe in probes)
        assert installer._install_progress(root)["phases"]["selected_capabilities"] != "complete"
        message = str(error.value)
        if outcome == "timeout":
            assert "TimeoutError" in message
        else:
            assert "basic review needs attention" in message
            assert "storage reserve is unsatisfied" in message
            assert "malware scanning is unavailable" in message
        assert installer._startup_remedy(root) in message
        assert "synthetic private transport detail" not in message
    output = capsys.readouterr().out
    assert "TimeoutError" not in output
    assert "synthetic private transport detail" not in output
    assert "health consensus" not in output
    elapsed = [int(value) for value in re.findall(
        r"Waiting for application health and sign-in readiness; elapsed (\d+)s", output)]
    assert len(elapsed) >= 3 and elapsed[0] == 0
    assert all(later - earlier >= 30 for earlier, later in zip(elapsed, elapsed[1:]))
    assert all(value <= clock[0] for value in elapsed)
    assert "\r" not in output and "\x1b" not in output
