"""Fresh ungated staging and saved-backend compatibility, using synthetic files."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_oss_installer import installer, preflight_args, ready_host  # noqa: F401 - pytest fixture registration
from tests.test_first_run_handoff import configured_node

ROOT = Path(__file__).parents[1]


def stager_module():
    spec = importlib.util.spec_from_file_location("synthetic_nemotron_stager", ROOT / "scripts/stage-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("models", ["transcription", "all"])
def test_new_nemotron_preflight_needs_no_gated_access(tmp_path, ready_host, monkeypatch, models):
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0,
        "0, Synthetic GPU, 49152, 47000, 8.9" if command[0] == "nvidia-smi" else '{"nvidia": {}}', ""))
    monkeypatch.setattr(installer, "_hf_token", lambda *a: pytest.fail("ungated staging asked for a token"))
    args = preflight_args(tmp_path, "--models", models, "--enable-diarization",
                          "--non-interactive", "--password-stdin", "--accept-model-terms")
    assert args.diarization_backend == "nemotron"
    result = installer._collect_preflight(models, args)
    assert result.ready
    assert not {"model-terms", "model-token-input"}.intersection(row.name for row in result.checks)


def test_stager_downloads_only_selected_ungated_files_and_binds_backend(tmp_path, monkeypatch):
    module = stager_module()
    calls = []
    def snapshot_download(**kwargs):
        calls.append(kwargs)
        assert kwargs["token"] is False
        assert kwargs["repo_id"] == "nvidia/Nemotron-3-Diarization"
        assert kwargs["revision"] == "0f087031414a6616bda8228f447d915a70a25720"
        snapshot = kwargs["cache_dir"] / ("models--" + kwargs["repo_id"].replace("/", "--")) / "snapshots" / kwargs["revision"]
        snapshot.mkdir(parents=True)
        for name in kwargs["allow_patterns"]:
            (snapshot / name).write_bytes(b"synthetic model bytes")
        return str(snapshot)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setattr(module, "_stage_tokenizer", lambda *a: [])
    args = ["stage-models", "--model-root", str(tmp_path), "--catalog", str(ROOT / "config/models.json"),
            "--groups", "transcription-diarization", "--accept-model-terms"]
    monkeypatch.setattr(sys, "argv", args)
    assert module.main() == 0
    assert len(calls) == 1
    assert set(calls[0]["allow_patterns"]) == {"config.json", "processor_config.json", "model.safetensors", "README.md"}
    manifest = json.loads((tmp_path / "huggingface/hub/approved-model-manifest.json").read_text())
    assert len(manifest["artifacts"]) == 1
    assert manifest["artifacts"][0]["license"] == "OpenMDW-1.1"
    assert all("snapshot_path" in row for row in manifest["artifacts"][0]["files"])
    assert all(row["snapshot_path"].startswith("models--nvidia--Nemotron-3-Diarization/snapshots/")
               for row in manifest["artifacts"][0]["files"])
    groups = frozenset({"transcription-diarization"})
    module._verify_stage(tmp_path, ROOT / "config/models.json", groups, "portable", diarization_backend="nemotron")
    with pytest.raises(RuntimeError, match="selection changed"):
        module._verify_stage(tmp_path, ROOT / "config/models.json", groups, "portable", diarization_backend="community-1")
    monkeypatch.setattr(sys, "argv", [*args, "verify"])
    assert module.main() == 0
    assert len(calls) == 1  # offline verification does not contact the hub


def test_configured_backend_agreement_and_legacy_default(tmp_path):
    root, args, paths = configured_node(tmp_path)
    installation = json.loads((root / "installation.json").read_text())
    config = installer._dotenv(paths["config"] / "transcription.env")
    compose = installer._dotenv(root / "compose.env")
    installation["transcription_diarization"] = True
    installer._restore_model_options(args, installation, compose, config)
    assert args.diarization_backend == "nemotron"
    config["TRANSCRIPTION_V2_DIARIZATION_BACKEND"] = "community-1"
    with pytest.raises(RuntimeError, match="diarization selections disagree"):
        installer._restore_model_options(args, installation, compose, config)
    del installation["diarization_backend"]
    del config["TRANSCRIPTION_V2_DIARIZATION_BACKEND"]
    installer._restore_model_options(args, installation, compose, config)
    assert args.diarization_backend == "community-1"


def test_reference_asr_staging_records_logical_snapshot_bindings(tmp_path, monkeypatch):
    import hashlib
    module = stager_module()
    def snapshot_download(**kwargs):
        repository = kwargs["cache_dir"] / ("models--" + kwargs["repo_id"].replace("/", "--"))
        snapshot = repository / "snapshots" / kwargs["revision"]
        snapshot.mkdir(parents=True)
        blobs = repository / "blobs"
        blobs.mkdir()
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
            data = ("synthetic " + name).encode()
            blob = blobs / hashlib.sha256(data).hexdigest()
            blob.write_bytes(data)
            (snapshot / name).symlink_to(blob)
        return str(snapshot)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setattr(module, "_stage_tokenizer", lambda *a: [])
    monkeypatch.setattr(sys, "argv", ["stage-models", "--model-root", str(tmp_path),
        "--catalog", str(ROOT / "config/models.json"), "--groups", "transcription-asr", "--accept-model-terms"])
    assert module.main() == 0
    manifest = json.loads((tmp_path / "huggingface/hub/approved-model-manifest.json").read_text())
    artifacts = [row for row in manifest["artifacts"] if row["role"] == "asr"]
    assert artifacts
    for artifact in artifacts:
        prefix = "models--" + artifact["model_id"].replace("/", "--")
        assert len(artifact["files"]) == 4
        for row in artifact["files"]:
            assert row["path"].startswith(prefix + "/blobs/")
            assert row["snapshot_path"].startswith(prefix + "/snapshots/" + artifact["revision"] + "/")


def test_api_and_worker_share_explicit_tokenizer_root():
    import re
    compose = (ROOT / "compose.yaml").read_text()
    for name in ("transcription-api", "transcription-worker"):
        section = compose.split("\n  " + name + ":\n", 1)[1]
        section = re.split(r"\n  [a-z][a-z-]*:\n", section, maxsplit=1)[0]
        assert "      NLTK_DATA: /models/nltk_data\n" in section
        assert "        target: /models\n        read_only: true" in section
