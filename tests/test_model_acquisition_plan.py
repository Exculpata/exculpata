"""Acquisition disclosure and consent use synthetic/no-network boundaries."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from tests.test_nemotron_installation import stager_module
from tests.test_tokenizer_staging import archive_bytes

ROOT = Path(__file__).resolve().parents[1]


def test_plan_needs_no_hub_package_token_or_destination(tmp_path, monkeypatch, capsys):
    module = stager_module()
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    monkeypatch.setattr(module, "_token", lambda *a: pytest.fail("plan read a token"))
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: pytest.fail("plan used network"))
    root = tmp_path / "uncreated"
    monkeypatch.setattr(sys, "argv", ["stage-models", "plan", "--catalog", str(ROOT / "config/models.json"),
        "--model-root", str(root), "--groups", "transcription-asr,transcription-diarization"])
    assert module.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["resources"]["punkt_tab"]["license"] == "UPSTREAM-TERMS-REVIEW-REQUIRED"
    assert any(m["model_id"] == "nvidia/Nemotron-3-Diarization" for m in plan["models"])
    assert all(not m.get("gated") for m in plan["models"])
    assert not root.exists()


def test_missing_acknowledgement_blocks_before_writes_or_import(tmp_path, monkeypatch):
    module = stager_module()
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    monkeypatch.setattr(module, "_token", lambda *a: pytest.fail("consumed token before acknowledgement"))
    root = tmp_path / "uncreated"
    monkeypatch.setattr(sys, "argv", ["stage-models", "--catalog", str(ROOT / "config/models.json"),
        "--model-root", str(root), "--groups", "transcription-asr"])
    with pytest.raises(RuntimeError, match="accept-model-terms"):
        module.main()
    assert not root.exists()


def test_supplied_pinned_tokenizer_archive_uses_no_network(tmp_path, monkeypatch):
    module = stager_module()
    data = archive_bytes([("punkt_tab/english/synthetic.tab", b"synthetic tokenizer")])
    archive = tmp_path / "supplied.zip"
    archive.write_bytes(data)
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: pytest.fail("local archive downloaded"))
    resource = {"sha256": hashlib.sha256(data).hexdigest()}
    files = module._stage_tokenizer(root, resource, archive)
    assert len(files) == 1 and files[0].read_bytes() == b"synthetic tokenizer"
    resource["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="verification"):
        module._stage_tokenizer(root, resource, archive)


def test_supplied_fifo_fails_without_blocking(tmp_path):
    import os
    module = stager_module()
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(RuntimeError, match="regular file"):
        module._stage_tokenizer(tmp_path, {}, pipe)


from tests.test_oss_installer import installer, preflight_args, ready_host  # noqa: F401 - pytest fixture registration
import subprocess


@pytest.mark.parametrize("accepted,needs_staging,blocked", [(False, True, True), (True, True, False), (False, False, False)])
def test_unattended_terms_preflight_and_verified_resume(tmp_path, ready_host, monkeypatch,
                                                        accepted, needs_staging, blocked):
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0,
        "0, Synthetic GPU, 49152, 47000, 8.9" if command[0] == "nvidia-smi" else '{"nvidia": {}}', ""))
    args = preflight_args(tmp_path, "--models", "all", "--enable-diarization", "--non-interactive",
                          "--password-stdin", *(["--accept-model-terms"] if accepted else []))
    result = installer._collect_preflight("all", args, needs_model_staging=needs_staging)
    assert (any(row.name == "resource-terms" and row.state == "fail" for row in result.checks)) == blocked
    assert result.ready == (not blocked)
    assert not args.root.exists()


def test_installer_previews_and_passes_acknowledgement_to_stager(tmp_path, monkeypatch):
    calls = []
    def run(console, command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if "verify" in command else 0, "", "")
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_hf_token", lambda *a: pytest.fail("Nemotron requested hub token"))
    args = installer._parser().parse_args(["install", "--root", str(tmp_path / "node"), "--models", "all",
        "--auth", "local", "--non-interactive", "--dry-run", "--prepare-only", "--accept-model-terms",
        "--transcription-languages", "en,es", "--enable-diarization"])
    installer._provision(installer.Console(color=False), args, args.root, "local", "all",
                         "synthetic.admin", "Synthetic Operator", password_input=["synthetic-password"])
    plan = next(i for i, command in enumerate(calls) if "model-stager" in command and "plan" in command)
    stage = next(i for i, command in enumerate(calls) if "model-stager" in command and "stage" in command)
    assert plan < stage
    assert "--accept-model-terms" in calls[stage]
    assert "--token-stdin" not in calls[stage]
    assert not args.root.exists()
