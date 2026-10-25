"""Synthetic installer tier choices and documentation/catalog agreement."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from case_intelligence.hardware_profile import HardwareReading, load_profiles
from tests.test_oss_installer import ROOT, installer
from tests.test_bring_your_own_model import _configure_node


@pytest.fixture
def detected_gpu(monkeypatch):
    reading = HardwareReading(8, 32 * 1024**3, "nvidia", 12 * 1024**3, False, (),
                              (12 * 1024**3,), 12 * 1024**3, 1)
    calls = []
    monkeypatch.setattr(installer, "detect_hardware", lambda: calls.append(True) or reading)
    return calls


def scripted_input(monkeypatch, answers):
    prompts = []
    responses = iter(answers)

    def answer(prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", answer)
    return prompts


@pytest.mark.parametrize("answers, expected", [
    ([""], "gpu-12"), (["cpu"], "cpu"), (["unknown", "gpu-8"], "gpu-8"),
])
def test_prompt_accepts_default_override_and_retries(monkeypatch, capsys, detected_gpu, answers, expected):
    args = installer._parser().parse_args(["install"])
    prompts = scripted_input(monkeypatch, answers)
    assert installer._select_model_tier(installer.Console(color=False), args) == expected
    assert args.model_tier == expected
    assert "[gpu-12]" in prompts[0]
    assert "remote" not in prompts[0]
    output = capsys.readouterr().out
    assert "8 CPU cores; 32.0 GiB RAM; NVIDIA" in output
    assert "suggested tier :: gpu-12" in output
    profiles = {p["id"]: p for p in load_profiles()["profiles"]}
    assert profiles["gpu-12"]["tradeoff"] in output
    assert profiles[expected]["tradeoff"] in output
    assert len(detected_gpu) == 1
    if "unknown" in answers:
        assert "Choose one of:" in output


@pytest.mark.parametrize("tier", [None, "cpu", "apple-32"])
def test_noninteractive_tier_never_reads_input(monkeypatch, detected_gpu, tier):
    arguments = ["install", "--non-interactive"] + (["--model-tier", tier] if tier else [])
    args = installer._parser().parse_args(arguments)
    scripted_input(monkeypatch, [])
    assert installer._select_model_tier(installer.Console(color=False), args) == (tier or "gpu-12")


def test_explicit_interactive_flag_skips_tier_prompt(monkeypatch, detected_gpu):
    args = installer._parser().parse_args(["install", "--model-tier", "potato"])
    scripted_input(monkeypatch, [])
    assert installer._select_model_tier(installer.Console(color=False), args) == "potato"


def test_invalid_flag_is_rejected_before_writes(tmp_path):
    root = tmp_path / "node"
    with pytest.raises(SystemExit) as exc:
        installer._parser().parse_args(["install", "--root", str(root), "--model-tier", "remote"])
    assert exc.value.code == 2
    assert not root.exists()


@pytest.mark.parametrize("failure", [OSError("synthetic"), ValueError("synthetic"),
                                    RuntimeError("synthetic"), subprocess.TimeoutExpired("synthetic", 1)])
def test_detection_failure_uses_cpu_without_retrying_detector(monkeypatch, capsys, failure):
    def fail():
        raise failure

    monkeypatch.setattr(installer, "detect_hardware", fail)
    scripted_input(monkeypatch, [""])
    args = installer._parser().parse_args(["install"])
    assert installer._select_model_tier(installer.Console(color=False), args) == "cpu"
    assert "hardware detection :: using the CPU suggestion" in capsys.readouterr().out


def test_main_selects_tier_before_preflight(monkeypatch, tmp_path, detected_gpu):
    monkeypatch.setattr(sys, "argv", ["install", "install", "--root", str(tmp_path / "node"),
                                    "--auth", "local", "--models", "none", "--non-interactive",
                                    "--model-tier", "gpu-8", "--dry-run"])
    seen = []

    def preflight(console, *, args, **kwargs):
        seen.append(args.model_tier)
        raise RuntimeError("synthetic stop before provisioning")

    monkeypatch.setattr(installer, "_preflight", preflight)
    assert installer.main() == 1
    assert seen == ["gpu-8"]
    assert not (tmp_path / "node").exists()


def test_main_collects_identity_before_tier(monkeypatch, tmp_path, detected_gpu):
    root = tmp_path / "node"
    monkeypatch.setattr(sys, "argv", ["install", "install", "--root", str(root),
                                    "--storage-root", str(tmp_path / "matters"),
                                    "--bind-address", "127.0.0.1", "--server-name", "node.example.test",
                                    "--auth", "local", "--models", "none", "--dry-run"])
    prompts = scripted_input(monkeypatch, ["synthetic.admin", "Synthetic Administrator", ""])
    seen = []

    def preflight(console, *, args, **kwargs):
        seen.append((args.admin_username, args.admin_display_name, args.model_tier))
        raise RuntimeError("synthetic stop before provisioning")

    monkeypatch.setattr(installer, "_preflight", preflight)
    assert installer.main() == 1
    assert seen == [("synthetic.admin", "Synthetic Administrator", "gpu-12")]
    assert prompts[0].startswith("Initial administrator username")
    assert prompts[1].startswith("Administrator display name")
    assert prompts[2].startswith("Hardware model tier")
    assert not root.exists()


@pytest.mark.parametrize("tier", [None, "gpu-8"])
def test_selected_tier_is_saved_and_preserved_on_resume(tmp_path, monkeypatch, detected_gpu, tier):
    _, root, _, _ = _configure_node(tmp_path, ["--model-tier", tier] if tier else [])
    expected = tier or "gpu-12"
    path = root / "installation.json"
    original = path.read_bytes()
    record = json.loads(original)
    assert record["model_tier"] == expected
    assert "external_generator" not in record
    assert len(detected_gpu) == (0 if tier else 1)
    seen = []
    monkeypatch.setattr(installer, "detect_hardware", lambda: pytest.fail("resume redetected hardware"))
    scripted_input(monkeypatch, [])
    monkeypatch.setattr(installer, "_password", lambda args: "synthetic-bootstrap-password")
    monkeypatch.setattr(installer, "_preflight", lambda *a, **kw: ())
    monkeypatch.setattr(installer, "_run", lambda *a, **kw: None)
    monkeypatch.setattr(installer, "_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(installer, "_provision", lambda console, args, *a, **kw: seen.append(args.model_tier))
    monkeypatch.setattr(sys, "argv", ["install", "install", "--resume", "--root", str(root),
                                    "--model-tier", "gpu-24", "--non-interactive"])
    assert installer.main() == 0
    assert seen == [expected]
    assert path.read_bytes() == original


@pytest.mark.parametrize("tier", [None, "unknown", []])
def test_saved_tier_compatibility_and_validation(tmp_path, detected_gpu, tier):
    _, root, _, _ = _configure_node(tmp_path, ["--model-tier", "cpu"])
    path = root / "installation.json"
    record = json.loads(path.read_text())
    if tier is None:
        record.pop("model_tier")
    else:
        record["model_tier"] = tier
    # fixture-write: Synthetic legacy or invalid tier metadata for saved-node validation.
    path.write_text(json.dumps(record))
    resumed = installer._parser().parse_args(["install", "--root", str(root), "--model-tier", "gpu-24"])
    if tier is None:
        installer._saved_node_arguments(resumed, root)
        assert resumed.model_tier is None
    else:
        with pytest.raises(RuntimeError, match="installed model tier is invalid"):
            installer._saved_node_arguments(resumed, root)


def test_endpoint_default_stays_declined(monkeypatch, detected_gpu):
    args = installer._parser().parse_args(["install", "--models", "none"])
    prompts = scripted_input(monkeypatch, ["", ""])
    installer._select_model_tier(installer.Console(color=False), args)
    assert installer._collect_external_generator(installer.Console(color=False), args) is None
    assert "[no]" in prompts[-1]
    assert args.external_generator_url is None
    assert not args.confirm_remote_generator


def test_models_tier_table_matches_catalog():
    text = (ROOT / "docs/MODELS.md").read_text()
    table = text.split("<!-- model-profile-table:start -->")[1].split("<!-- model-profile-table:end -->")[0]
    rows = [line.split(" | ") for line in table.strip().splitlines()[2:]]
    expected = [["| `" + p["id"] + "`", p["label"], p["generator"], p["embed_rerank"],
                 p["asr"], p["tradeoff"] + " |"] for p in load_profiles()["profiles"]]
    assert rows == expected
