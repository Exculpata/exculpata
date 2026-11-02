"""Content-free administrator bindings, offline imports and historical provenance."""
import copy
import hashlib
import json
import socket
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("punkt_inventory")

from transcription_v2.model_manifest import ModelManifestError, verify_model_manifest
from transcription_v2.asr_models import approved_asr_snapshot, profile_model_metadata
from transcription_v2.model_import import import_model_cache
from transcription_v2.profile_availability import ProfileAvailability
from transcription_v2.profiles import get_profile
from transcription_v2.settings import Settings


def inventory(root, *, translation=True):
    root.mkdir()
    artifacts, bindings = [], {}
    for slot, model, revision in (("primary", "example/synthetic-speech-a", "a" * 40),
                                  ("fast", "example/synthetic-speech-b", "b" * 40)):
        snapshot = root / ("models--" + model.replace("/", "--")) / "snapshots" / revision
        snapshot.mkdir(parents=True)
        files = []
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
            data = ("synthetic bytes: " + slot + name).encode()
            path = snapshot / name
            path.write_bytes(data)
            files.append({"path": path.relative_to(root).as_posix(), "size_bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        artifacts.append({"role": "asr", "model_id": model, "revision": revision,
                          "license": "Synthetic-test-terms", "files": files})
        bindings[slot] = {"adapter": "faster-whisper", "model_id": model, "revision": revision,
                          "supports_translation": translation if slot == "primary" else False}
    name = "wav2vec2_fairseq_base_ls960_asr_ls960.pth"
    data = b"synthetic alignment bytes"
    (root / name).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    artifacts.append({"role": "alignment_en", "model_id": "torchaudio/WAV2VEC2_ASR_BASE_960H",
                      "revision": "sha256-" + digest, "license": "Synthetic-test-terms",
                      "files": [{"path": name, "size_bytes": len(data), "sha256": digest}]})
    payload = {"schema_version": "transcription-v2-model-manifest-v2", "artifacts": artifacts,
               "asr_bindings": bindings}
    save(root, payload)
    return payload


def save(root, payload):
    (root / "approved-model-manifest.json").write_text(json.dumps(payload))


def verify(root):
    return verify_model_manifest(root, root / "approved-model-manifest.json")


def config(root):
    return Settings(data_root=root.parent / "jobs", database_path=root.parent / "jobs/jobs.sqlite3",
                    model_cache_dir=root, pipeline_backend="whisperx", minimum_free_disk_bytes=0)


def test_two_alternatives_resolve_exact_bytes_and_survive_restart(tmp_path):
    root = tmp_path / "source"
    payload = inventory(root)
    for _ in range(2):
        ready = verify(root)
        for alias, slot in (("large-v3", "primary"), ("turbo", "fast")):
            path = approved_asr_snapshot(root, ready, alias)
            assert path.name == payload["asr_bindings"][slot]["revision"]
        gate = ProfileAvailability(config(root))
        assert all(row["available"] for row in gate.public_profiles())
        fast = next(row for row in gate.public_profiles() if row["name"] == "fast")
        assert fast["asr_model"] == "example/synthetic-speech-b"
        assert fast["asr_artifact"]["license"] == "Synthetic-test-terms"
        assert fast["translation_available"] is True  # separate primary pass
        assert str(root) not in json.dumps(gate.public_profiles())


@pytest.mark.parametrize("mutation", ["adapter", "revision", "escape", "capability", "unknown-slot", "dangling", "extra"])
def test_invalid_binding_refuses_whole_inventory(tmp_path, mutation):
    root = tmp_path / "source"
    payload = inventory(root)
    binding = payload["asr_bindings"]["primary"]
    if mutation == "adapter": binding["adapter"] = "load-python"
    if mutation == "revision": binding["revision"] = "main"
    if mutation == "escape": binding["model_id"] = "../../escape"
    if mutation == "capability": binding["supports_translation"] = "true"
    if mutation == "unknown-slot": payload["asr_bindings"]["arbitrary"] = binding
    if mutation == "dangling": binding["revision"] = "c" * 40
    if mutation == "extra": binding["path"] = "/outside/model"
    save(root, payload)
    with pytest.raises(ModelManifestError): verify(root)
    assert not ProfileAvailability(config(root)).available(get_profile("balanced"))


def test_v2_missing_binding_never_falls_back_and_translation_is_explicit(tmp_path):
    root = tmp_path / "source"
    payload = inventory(root, translation=False)
    gate = ProfileAvailability(config(root))
    assert gate.available(get_profile("fast"))
    assert not gate.available(get_profile("fast"), translate=True)
    with pytest.raises(ValueError): approved_asr_snapshot(root, verify(root), "large-v3", translate=True)
    del payload["asr_bindings"]["fast"]
    save(root, payload)
    assert not ProfileAvailability(config(root)).available(get_profile("fast"))


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra", "symlink"])
def test_artifact_changes_rejected_after_start(tmp_path, mutation):
    root = tmp_path / "source"
    inventory(root)
    ready = verify(root)
    snapshot = approved_asr_snapshot(root, ready, "large-v3")
    target = snapshot / "model.bin"
    if mutation == "missing": target.unlink()
    if mutation == "changed": target.write_bytes(b"mutated fixture")
    if mutation == "extra": (snapshot / "unapproved.bin").write_bytes(b"extra fixture")
    if mutation == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
    with pytest.raises(ValueError): approved_asr_snapshot(root, ready, "large-v3")


def test_provenance_is_frozen_and_does_not_claim_default_weights(tmp_path):
    root = tmp_path / "source"
    payload = inventory(root)
    ready = verify(root)
    old = profile_model_metadata(get_profile("balanced"), ready)
    frozen = copy.deepcopy(old)
    assert old["asr_slot"] == "primary"
    assert old["asr_model"] == "example/synthetic-speech-a"
    assert old["asr_artifact"]["revision"] == "a" * 40
    assert not any(c["name"] == "OpenAI Whisper weights" for c in old["components"])
    payload["asr_bindings"]["primary"] = payload["asr_bindings"]["fast"]
    save(root, payload)
    new = profile_model_metadata(get_profile("balanced"), verify(root))
    assert old == frozen
    assert new["asr_model"] != old["asr_model"]


def test_local_import_is_network_free_and_preserves_original_and_separate_restore(tmp_path):
    source = tmp_path / "source"
    inventory(source)
    original = (source / "approved-model-manifest.json").read_bytes()
    with patch.object(socket, "socket", side_effect=AssertionError("no network")):
        for name in ("imported", "restored"):
            target = tmp_path / name
            receipt = import_model_cache(source, target, max_bytes=1024 * 1024)
            assert receipt["status"] == "ready"
            assert str(source) not in json.dumps(receipt)
            assert (target / "approved-model-manifest.json").read_bytes() == original
            assert ProfileAvailability(config(target)).available(get_profile("fast"))
    assert (source / "approved-model-manifest.json").read_bytes() == original


@pytest.mark.parametrize("failure", ["exists", "symlink", "capacity", "bad-hash"])
def test_import_refuses_without_overwriting_existing_data(tmp_path, failure):
    source = tmp_path / "source"
    inventory(source)
    target = tmp_path / "destination"
    if failure == "exists":
        target.mkdir()
        (target / "sentinel").write_bytes(b"keep")
    if failure == "symlink": target.symlink_to(source, target_is_directory=True)
    if failure == "bad-hash": next(source.glob("models--*/snapshots/*/model.bin")).write_bytes(b"changed")
    with pytest.raises((ValueError, ModelManifestError)):
        import_model_cache(source, target, max_bytes=1 if failure == "capacity" else 1024 * 1024)
    if failure == "exists": assert (target / "sentinel").read_bytes() == b"keep"
    if failure in ("capacity", "bad-hash"): assert not target.exists()


def test_worker_uses_bound_snapshot_and_translation_capability_before_loader(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from transcription_v2.pipeline import LocalWhisperXEngine, TranscriptionRequest, PipelineConfigurationError
    root = tmp_path / "source"
    inventory(root, translation=False)
    engine = LocalWhisperXEngine(model_cache_dir=root, model_readiness=verify(root))
    calls = []
    monkeypatch.setattr(engine, "_import_whisperx", lambda: SimpleNamespace(
        load_model=lambda *args, **kwargs: calls.append((args, kwargs)) or object()))
    request = TranscriptionRequest(audio_path=tmp_path / "synthetic.wav", device="cpu")
    engine._base_asr_pipeline(request, get_profile("fast"), model_name="turbo", task="transcribe")
    assert calls[0][0][0] == str(approved_asr_snapshot(root, verify(root), "turbo"))
    assert calls[0][1]["local_files_only"] is True
    with pytest.raises(PipelineConfigurationError):
        engine._base_asr_pipeline(request, get_profile("fast"), model_name="large-v3", task="translate")
    assert len(calls) == 1


def test_custom_api_rejects_translation_and_changed_manifest_before_enqueue(tmp_path):
    import os
    from fastapi.testclient import TestClient
    with patch.dict(os.environ, {"TRANSCRIPTION_V2_DATA_ROOT": str(tmp_path / "api-import")}, clear=True):
        from transcription_v2.api import create_app
    root = tmp_path / "source"
    payload = inventory(root, translation=False)
    app = create_app(config(root))
    with TestClient(app) as client:
        headers = {"X-User-ID": "synthetic-user"}
        response = client.get("/v1/profiles", headers=headers)
        assert response.status_code == 200
        assert response.json()["profiles"][0]["asr_model"] == "example/synthetic-speech-a"
        def submit(translate):
            return client.post("/v1/jobs", headers=headers,
                               data={"options": json.dumps({"profile": "fast", "translate_to_english": translate})},
                               files=[("files", ("synthetic.wav", b"synthetic bytes", "audio/wav"))])
        assert submit(True).status_code == 409
        assert app.state.runtime.store.retained_usage() == (0, 0)
        assert submit(False).status_code == 201
        usage = app.state.runtime.store.retained_usage()
        payload["asr_bindings"]["fast"] = payload["asr_bindings"]["primary"]
        save(root, payload)
        assert submit(False).status_code == 409
        assert app.state.runtime.store.retained_usage() == usage


def test_pipeline_result_freezes_custom_provenance(tmp_path):
    from transcription_v2.pipeline import MockPipelineEngine, TranscriptionPipeline, TranscriptionRequest
    root = tmp_path / "source"
    payload = inventory(root)
    engine = MockPipelineEngine()
    engine.model_readiness = verify(root)
    media = tmp_path / "synthetic.wav"
    media.write_bytes(b"synthetic fixture")
    result = TranscriptionPipeline(engine).run(TranscriptionRequest(audio_path=media, device="cpu", language="es", translation_target="en"))
    historical = json.dumps(result.to_dict(), sort_keys=True)
    assert result.profile["asr_model"] == "example/synthetic-speech-a"
    assert result.stage("source_transcription").details["model"] == result.profile["asr_model"]
    assert result.translation["model"] == result.profile["translation_model"]
    assert result.translation["status"] == "succeeded"
    payload["asr_bindings"]["primary"] = payload["asr_bindings"]["fast"]
    save(root, payload)
    engine.model_readiness = verify(root)
    assert json.dumps(result.to_dict(), sort_keys=True) == historical
    later = TranscriptionPipeline(engine).run(TranscriptionRequest(audio_path=media, device="cpu", language="es", translation_target="en"))
    assert later.profile["asr_model"] == "example/synthetic-speech-b"


def test_import_failure_during_copy_leaves_no_approved_manifest(tmp_path, monkeypatch):
    from transcription_v2 import model_import as module
    source, target = tmp_path / "source", tmp_path / "target"
    inventory(source)
    original = module.os.fsync
    def fail(_):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(ValueError): import_model_cache(source, target, max_bytes=1024 * 1024)
    assert target.is_dir()
    assert not (target / "approved-model-manifest.json").exists()
    monkeypatch.setattr(module.os, "fsync", original)
    assert verify(source).public_dict()["ready"] is True


def test_cli_has_content_free_failure_and_bound_copy_size(tmp_path, capsys):
    from transcription_v2.model_import import main
    source, target = tmp_path / "source", tmp_path / "target"
    inventory(source)
    assert main([str(source), str(target), "--max-bytes", "1"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["code"] == "model_import_failed"
    assert str(tmp_path) not in output
    assert main([str(source), str(target), "--max-bytes", "1048576"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True


def test_special_file_import_fails_without_waiting_for_a_writer(tmp_path):
    import os
    import subprocess
    import sys
    source, target = tmp_path / "source", tmp_path / "target"
    inventory(source)
    weights = next(source.glob("models--*/snapshots/*/model.bin"))
    weights.unlink()
    os.mkfifo(weights)
    result = subprocess.run([sys.executable, "-m", "transcription_v2.model_import", str(source),
                             str(target), "--max-bytes", "1048576"], capture_output=True,
                            text=True, timeout=3)
    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "model_import_failed"
    assert not target.exists()
