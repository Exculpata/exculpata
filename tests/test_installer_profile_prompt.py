"""Synthetic installer tier choices and documentation/catalog agreement."""
from __future__ import annotations

import io
import json
import sys

import pytest

from case_intelligence.hardware_profile import load_profiles
from tests.test_oss_installer import ROOT, installer
from tests.test_bring_your_own_model import _configure_node


def scripted_input(monkeypatch, answers):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    responses = iter(answers)

    def answer(prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", answer)
    return prompts


@pytest.mark.parametrize("stdin_flag, is_tty", [
    ("--password-stdin", False), ("--hf-token-stdin", False), (None, False),
    ("--password-stdin", True), ("--hf-token-stdin", True),
    ("--external-generator-api-key-stdin", False), ("--external-generator-api-key-stdin", True),
])
def test_main_preserves_stdin_for_password_reader(
    monkeypatch, tmp_path, capsys, stdin_flag, is_tty,
):
    secret = "synthetic-pass-0000"
    stream = io.StringIO(secret + "\n")
    monkeypatch.setattr(stream, "isatty", lambda: is_tty)
    monkeypatch.setattr(sys, "stdin", stream)
    root = tmp_path / "node"
    monkeypatch.setattr(sys, "argv", [
        "install", "install", "--root", str(root),
        "--storage-root", str(tmp_path / "matters"),
        "--bind-address", "127.0.0.1", "--server-name", "node.example.test",
        "--auth", "local", "--admin-username", "synthetic.admin",
        "--admin-display-name", "Synthetic Administrator",
    ] + ([stdin_flag] if stdin_flag else ["--models", "none"]))
    seen = []

    def password_reader(args):
        seen.append((args.model_tier, sys.stdin.readline().rstrip("\r\n")))
        return seen[-1][1]

    def stop_before_writes(*args, **kwargs):
        raise RuntimeError("synthetic stop after password input")

    monkeypatch.setattr(installer, "_preflight", lambda *a, **kw: ())
    monkeypatch.setattr(installer, "_password", password_reader)
    monkeypatch.setattr(installer, "_prepare_directories", stop_before_writes)
    assert installer.main() == 1
    assert seen == [(None, secret)]
    output = capsys.readouterr().out
    assert "synthetic stop after password input" in output
    assert "Hardware model tier (" not in output
    assert "EOFError" not in output
    assert secret not in output
    assert not root.exists()


def test_invalid_flag_is_rejected_before_writes(tmp_path):
    root = tmp_path / "node"
    with pytest.raises(SystemExit) as exc:
        installer._parser().parse_args(["install", "--root", str(root), "--model-tier", "remote"])
    assert exc.value.code == 2
    assert not root.exists()


def test_explicit_legacy_tier_is_accepted_as_metadata(monkeypatch, tmp_path):
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


def test_default_collects_identity_without_selecting_a_bundle_or_tier(monkeypatch, tmp_path):
    root = tmp_path / "node"
    monkeypatch.setattr(sys, "argv", ["install", "install", "--root", str(root),
                                    "--storage-root", str(tmp_path / "matters"),
                                    "--bind-address", "127.0.0.1", "--server-name", "node.example.test",
                                    "--auth", "local", "--dry-run"])
    prompts = scripted_input(monkeypatch, ["", "synthetic.admin", "Synthetic Administrator"])
    seen = []

    def preflight(console, *, args, **kwargs):
        seen.append((args.admin_username, args.admin_display_name, args.models, args.model_tier))
        raise RuntimeError("synthetic stop before provisioning")

    monkeypatch.setattr(installer, "_preflight", preflight)
    assert installer.main() == 1
    assert seen == [("synthetic.admin", "Synthetic Administrator", "none", None)]
    assert len(prompts) == 3
    assert prompts[0].startswith("Connect an answer server")
    assert prompts[1].startswith("Initial administrator username")
    assert prompts[2].startswith("Administrator display name")
    assert not root.exists()


@pytest.mark.parametrize("tier", [None, "gpu-8"])
def test_selected_tier_is_saved_and_preserved_on_resume(tmp_path, monkeypatch, tier):
    _, root, _, _ = _configure_node(tmp_path, ["--model-tier", tier] if tier else [])
    expected = tier
    path = root / "installation.json"
    original = path.read_bytes()
    record = json.loads(original)
    assert record.get("model_tier") == expected
    assert record["models"] == "none"
    assert record["profiles"] == []
    assert installer._dotenv(root / "compose.env")["CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL"] == ""
    assert "external_generator" not in record
    seen = []
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
def test_saved_tier_compatibility_and_validation(tmp_path, tier):
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


def test_endpoint_default_stays_declined(monkeypatch):
    args = installer._parser().parse_args(["install", "--models", "none"])
    prompts = scripted_input(monkeypatch, [""])
    assert installer._collect_external_generator(installer.Console(color=False), args) is None
    assert "[no]" in prompts[-1]
    assert args.external_generator_url is None
    assert not args.confirm_remote_generator


def test_models_tier_table_matches_catalog():
    import re

    text = (ROOT / "docs/MODELS.md").read_text()
    table = text.split("<!-- model-profile-table:start -->")[1].split("<!-- model-profile-table:end -->")[0]
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in table.strip().splitlines()[2:] if line.strip()]
    profiles = load_profiles()["profiles"]
    assert [row[0] for row in rows] == [f'`{p["id"]}`' for p in profiles]

    models = json.loads((ROOT / "config/models.json").read_text())["models"]
    generator_names = {name.casefold() for model in models if model["role"] == "generator"
                       for name in (model["model_id"], model["model_id"].rsplit("/", 1)[-1])}
    errors = []
    if any(name in table.casefold() for name in generator_names):
        errors.append("marked tier table names a catalog generator")
    for row, profile in zip(rows, profiles, strict=True):
        tier = profile["id"]
        if len(row) != 5:
            errors.append(f"{tier}: expected five usable-memory table columns")
        detection = row[1]
        # Detection bounds use explicit MiB values, independent of nominal hardware labels.
        memory_bounds = [int(value.replace(",", "")) for value in
                         re.findall(r"\b(\d[\d,]*)\s+MiB\b", detection)]
        if profile["accelerator"] == "multi-gpu":
            expected_bounds = [profile["min_device_vram_mib"]]
            device_count = re.search(r"\b(\d+)\s+(?:cards|GPUs|devices)\b", detection)
            if device_count is None or int(device_count[1]) != profile["min_devices"]:
                errors.append(f"{tier}: device count differs from config")
        else:
            resource = "vram" if profile["accelerator"] == "gpu" else "ram"
            expected_bounds = [profile[f"min_{resource}_mib"]]
            maximum = profile.get(f"max_{resource}_mib")
            if maximum is not None:
                expected_bounds.append(maximum)
        if memory_bounds != expected_bounds:
            errors.append(f"{tier}: detection memory bounds differ from config")
    assert errors == []
