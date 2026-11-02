"""Synthetic, GPU-free protocol and model-authority regressions."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from transcription_v2 import nemotron
from transcription_v2.model_manifest import MODEL_MANIFEST_SCHEMA, verify_model_manifest
from transcription_v2.pipeline import MockPipelineEngine, TranscriptionPipeline, TranscriptionRequest
from transcription_v2.profiles import get_profile


def runner(tmp_path, monkeypatch, body):
    script = tmp_path / "nemotron_runner.py"
    script.write_text(body)
    monkeypatch.setattr(nemotron, "__file__", str(tmp_path / "nemotron.py"))
    return dict(python=sys.executable, model_path="synthetic-model", audio_path="synthetic-audio",
                device="cpu", timeout_seconds=3)


def test_process_protocol_is_offline_and_does_not_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "synthetic-token")
    monkeypatch.setenv("HTTPS_PROXY", "synthetic-proxy")
    monkeypatch.setenv("TRITON_CACHE_DIR", "/untrusted-parent-cache")
    args = runner(tmp_path, monkeypatch, '''import json, os, sys
from pathlib import Path
request = json.load(sys.stdin)
assert os.environ['HF_HUB_OFFLINE'] == '1'
assert os.environ['TRANSFORMERS_OFFLINE'] == '1'
assert 'HF_TOKEN' not in os.environ and 'HTTPS_PROXY' not in os.environ
assert request['audio_path'] not in sys.argv
native_cache = Path(os.environ['TRITON_CACHE_DIR'])
assert native_cache.parent == Path(os.environ['NUMBA_CACHE_DIR']).parent
assert native_cache != Path('/untrusted-parent-cache')
native_cache.mkdir()
(native_cache / 'synthetic-cache-entry').write_bytes(b'synthetic')
print(json.dumps({'schema': 'recordbench-nemotron-v1', 'turns': [
    {'start': 1, 'end': 3, 'speaker': 1}, {'start': 0, 'end': 2, 'speaker': 0}]}))
''')
    turns = nemotron.run_diarization(**args)
    assert turns == [{"start": 0., "end": 2., "speaker": "SPEAKER_00"},
                     {"start": 1., "end": 3., "speaker": "SPEAKER_01"}]


@pytest.mark.parametrize("body", [
    "print('unstructured dependency output')",
    "print('{\"schema\":\"recordbench-nemotron-v1\",\"turns\":[]}'); print('extra')",
    "raise RuntimeError('synthetic dependency detail must not escape')",
    "print('x' * (5 * 1024 * 1024))",
])
def test_process_failures_are_closed_and_content_free(tmp_path, monkeypatch, body):
    args = runner(tmp_path, monkeypatch, body)
    with pytest.raises(nemotron.NemotronError) as caught:
        nemotron.run_diarization(**args)
    assert "synthetic dependency" not in str(caught.value)


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_reap_process_group(tmp_path, monkeypatch, cancel):
    pid_file = tmp_path / "child-pid"
    # The process-group leader exits on kill, and its descendant must receive
    # the same signal even if the decoder outlives the interpreter.
    body = f'''import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path({str(pid_file)!r}).write_text(str(child.pid))
time.sleep(30)
'''
    args = runner(tmp_path, monkeypatch, body)
    args["timeout_seconds"] = 0.5
    with pytest.raises(nemotron.NemotronError, match="canceled" if cancel else "timed out"):
        nemotron.run_diarization(**args, cancel_requested=lambda: cancel and pid_file.exists())
    pid = int(pid_file.read_text())
    # Linux may report zombie (Z) or dead (X) before removing the process.
    # Read once per poll: the proc entry may disappear between filesystem calls.
    import time
    status = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 2
    while True:
        try:
            state = status.read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            break
        if state in {"Z", "X", "x"}:
            break
        assert time.monotonic() < deadline, "descendant is still running"
        time.sleep(0.01)



@pytest.mark.parametrize("row", [
    {"start": -1, "end": 2, "speaker": 0},
    {"start": 1, "end": 1, "speaker": 0},
    {"start": 3, "end": 2, "speaker": 0},
    {"start": float("nan"), "end": 2, "speaker": 0},
    {"start": 0, "end": float("inf"), "speaker": 0},
    {"start": 0, "end": 2, "speaker": 8},
    {"start": 0, "end": 2, "speaker": True},
    {"start": 0, "end": 2, "speaker": "speaker_0"},
    {"start": 0, "end": 2},
])
def test_invalid_turns_are_rejected(row):
    with pytest.raises(nemotron.NemotronError):
        nemotron.validate_turns([row])


def test_silence_is_an_empty_result():
    assert nemotron.validate_turns([]) == []


def test_snapshot_requires_all_files_exact_revision_and_verified_bytes(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    files = []
    for name in nemotron.MODEL_FILES:
        path = root / name
        path.write_bytes(b"independently constructed model placeholder")
        files.append({"path": f"snapshot/{name}", "size_bytes": path.stat().st_size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = tmp_path / "approved-model-manifest.json"
    artifact = {"role": "diarization", "model_id": nemotron.MODEL_ID,
                "revision": nemotron.MODEL_REVISION, "license": nemotron.MODEL_LICENSE,
                "files": files}
    def approve():
        manifest.write_text(json.dumps({"schema_version": MODEL_MANIFEST_SCHEMA,
                                        "artifacts": [artifact]}))
        return verify_model_manifest(tmp_path, manifest)
    def allowed(readiness):
        return nemotron.approved_snapshot(root, cache_root=tmp_path, model_readiness=readiness)
    assert allowed(approve())
    artifact["revision"] = "a" * 40
    assert not allowed(approve())
    artifact["revision"] = nemotron.MODEL_REVISION
    artifact["files"] = files[:-1]
    assert not allowed(approve())
    artifact["files"] = files
    readiness = approve()
    (root / "model.safetensors").write_bytes(b"tampered synthetic artifact")
    assert not allowed(readiness)


class SyntheticNemotronEngine(MockPipelineEngine):
    diarization_backend = "nemotron"


def test_profile_and_exported_outcome_identify_actual_backend(tmp_path):
    profile = get_profile(diarization_backend="nemotron")
    assert profile.diarization_model == nemotron.MODEL_ID
    assert not any(item.gated for item in profile.components)
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"synthetic input; mock never decodes it")
    result = TranscriptionPipeline(SyntheticNemotronEngine()).run(
        TranscriptionRequest(audio, min_speakers=2, max_speakers=4))
    assert result.diarization["model"] == nemotron.MODEL_ID
    assert result.profile["diarization_model"] == nemotron.MODEL_ID
    assert "speaker_hints_unsupported" in {item["code"] for item in result.warnings}
    skipped = TranscriptionPipeline(SyntheticNemotronEngine()).run(
        TranscriptionRequest(audio, diarize_speakers=False))
    assert skipped.diarization["status"] == "skipped"
    assert skipped.diarization["model"] is None
    assert "speaker_hints_unsupported" not in {item["code"] for item in skipped.warnings}


@pytest.fixture
def linked_nemotron(tmp_path):
    root = tmp_path / "cache"
    snapshot = root / "models--nvidia--Nemotron-3-Diarization" / "snapshots" / nemotron.MODEL_REVISION
    snapshot.mkdir(parents=True)
    blobs = snapshot.parent.parent / "blobs"
    blobs.mkdir()
    files = []
    for name in nemotron.MODEL_FILES:
        data = ("Synthetic Nemotron " + name).encode()
        digest = hashlib.sha256(data).hexdigest()
        blob = blobs / digest
        blob.write_bytes(data)
        (snapshot / name).symlink_to(blob)
        files.append({"path": blob.relative_to(root).as_posix(),
                      "snapshot_path": (snapshot / name).relative_to(root).as_posix(),
                      "size_bytes": len(data), "sha256": digest})
    manifest = root / "approved-model-manifest.json"
    manifest.write_text(json.dumps({"schema_version": MODEL_MANIFEST_SCHEMA, "artifacts": [{
        "role": "diarization", "model_id": nemotron.MODEL_ID, "revision": nemotron.MODEL_REVISION,
        "license": nemotron.MODEL_LICENSE, "files": files}]}))
    return root, snapshot, manifest


@pytest.mark.parametrize("restart", [False, True])
def test_nemotron_rejects_retargeted_approved_blob(linked_nemotron, restart):
    root, snapshot, manifest = linked_nemotron
    readiness = verify_model_manifest(root, manifest)
    config = snapshot / "config.json"
    config.unlink()
    config.symlink_to((snapshot / "processor_config.json").resolve())
    if restart:
        readiness = verify_model_manifest(root, manifest)
    assert not nemotron.approved_snapshot(snapshot, cache_root=root, model_readiness=readiness)


def test_nemotron_accepts_bound_links_but_rejects_legacy_blob_inventory(linked_nemotron):
    root, snapshot, manifest = linked_nemotron
    readiness = verify_model_manifest(root, manifest)
    assert nemotron.approved_snapshot(snapshot, cache_root=root, model_readiness=readiness)
    document = json.loads(manifest.read_text())
    for row in document["artifacts"][0]["files"]:
        row.pop("snapshot_path")
    manifest.write_text(json.dumps(document))
    assert not nemotron.approved_snapshot(snapshot, cache_root=root,
                                         model_readiness=verify_model_manifest(root, manifest))


@pytest.mark.parametrize("change", ["extra-alias", "directory-link", "snapshot-link"])
def test_nemotron_rejects_unapproved_snapshot_topology(linked_nemotron, change):
    root, snapshot, manifest = linked_nemotron
    if change == "extra-alias":
        (snapshot / "extra.json").symlink_to((snapshot / "config.json").resolve())
    elif change == "directory-link":
        (snapshot / "extra").symlink_to(snapshot, target_is_directory=True)
    else:
        alias = snapshot.with_name("alias")
        alias.symlink_to(snapshot, target_is_directory=True)
        snapshot = alias
    assert not nemotron.approved_snapshot(snapshot, cache_root=root,
                                         model_readiness=verify_model_manifest(root, manifest))


def test_import_rejects_mapped_nemotron_blobs_before_creating_destination(linked_nemotron, tmp_path):
    from transcription_v2.model_import import import_model_cache
    root, _, _ = linked_nemotron
    target = tmp_path / "imported"
    with pytest.raises(ValueError, match="Local model import failed"):
        import_model_cache(root, target, max_bytes=1024 * 1024)
    assert not target.exists()


@pytest.mark.parametrize("restart", [False, True])
def test_every_approved_nemotron_member_must_remain_present(linked_nemotron, restart):
    root, snapshot, manifest = linked_nemotron
    data = b"Synthetic model card"
    digest = hashlib.sha256(data).hexdigest()
    blob = snapshot.parent.parent / "blobs" / digest
    blob.write_bytes(data)
    path = snapshot / "README.md"
    path.symlink_to(blob)
    document = json.loads(manifest.read_text())
    document["artifacts"][0]["files"].append({"path": blob.relative_to(root).as_posix(),
        "snapshot_path": path.relative_to(root).as_posix(), "sha256": digest, "size_bytes": len(data)})
    manifest.write_text(json.dumps(document))
    readiness = verify_model_manifest(root, manifest)
    assert nemotron.approved_snapshot(snapshot, cache_root=root, model_readiness=readiness)
    path.unlink()
    if restart:
        readiness = verify_model_manifest(root, manifest)
    assert not nemotron.approved_snapshot(snapshot, cache_root=root, model_readiness=readiness)


@pytest.mark.parametrize("change", ["repoint", "replace", "remove"])
def test_nemotron_rechecks_snapshot_before_each_subprocess(linked_nemotron, monkeypatch, change):
    import sys
    from types import SimpleNamespace
    from transcription_v2.pipeline import LocalWhisperXEngine, ModelApprovalError
    root, snapshot, manifest = linked_nemotron
    readiness = verify_model_manifest(root, manifest)
    engine = LocalWhisperXEngine(model_cache_dir=root, model_readiness=readiness,
        diarization_backend="nemotron", diarization_model_path=snapshot)
    calls = []
    monkeypatch.setattr(nemotron, "run_diarization", lambda **kw: calls.append(kw) or [])
    monkeypatch.setitem(sys.modules, "pandas", SimpleNamespace(DataFrame=lambda *a, **kw: object()))
    request = TranscriptionRequest(root / "synthetic-not-read.wav", device="cpu")
    profile = get_profile(diarization_backend="nemotron")
    engine.diarize(request, profile)
    engine.diarize(request, profile)
    assert len(calls) == 2
    target = snapshot / "config.json"
    if change == "replace":
        target.resolve().write_bytes(b"Changed synthetic model configuration")
    else:
        target.unlink()
        if change == "repoint":
            target.symlink_to((snapshot / "processor_config.json").resolve())
    with pytest.raises(ModelApprovalError):
        engine.diarize(request, profile)
    assert len(calls) == 2
