"""Synthetic inventories exercise admission without models, CUDA or downloads."""
import hashlib
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("punkt_inventory")
from fastapi.testclient import TestClient
from transcription_v2.model_manifest import MODEL_MANIFEST_SCHEMA
from transcription_v2.profile_availability import ProfileAvailability
from transcription_v2.profiles import get_profile
from transcription_v2.settings import Settings
from ui.api_client import ApiError
from ui.streamlit_app import _available_profiles, _render_new_job

_IMPORT_RUNTIME = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"TRANSCRIPTION_V2_DATA_ROOT": _IMPORT_RUNTIME.name}, clear=True):
    from transcription_v2.api import create_app

REPOS = {"large-v3": "Systran/faster-whisper-large-v3",
         "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}


def inventory(root, models):
    root.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for model in models:
        repo = REPOS[model]
        snapshot = root / ("models--" + repo.replace("/", "--")) / "snapshots" / ("a" * 40)
        snapshot.mkdir(parents=True)
        files = []
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
            data = ("Synthetic fixture " + model + name).encode()
            target = snapshot / name
            target.write_bytes(data)
            files.append({"path": str(target.relative_to(root)), "size_bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        artifacts.append({"role": "asr", "model_id": repo, "revision": "a" * 40,
                          "license": "MIT", "files": files})
    from transcription_v2.alignment_models import ALIGNMENT_MODELS
    for language, (bundle, filename) in ALIGNMENT_MODELS.items():
        data = ("Synthetic alignment " + language).encode()
        (root / filename).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        artifacts.append({"role": "alignment_" + language, "model_id": "torchaudio/" + bundle,
                          "revision": "sha256-" + digest, "license": "BSD-2-Clause",
                          "files": [{"path": filename, "size_bytes": len(data), "sha256": digest}]})
    (root / "approved-model-manifest.json").write_text(json.dumps({
        "schema_version": MODEL_MANIFEST_SCHEMA, "artifacts": artifacts}))


def settings(tmp_path, models):
    cache = tmp_path / "cache"
    inventory(cache, models)
    return Settings(data_root=tmp_path / "data", database_path=tmp_path / "data/jobs.sqlite3",
                    pipeline_backend="whisperx", model_cache_dir=cache,
                    minimum_free_disk_bytes=0, diarization_backend="nemotron")


@pytest.mark.parametrize("models, expected", [([], set()), (["large-v3"], {"balanced", "high_accuracy"}),
                                              (["large-v3", "turbo"], {"balanced", "high_accuracy", "fast"})])
def test_api_inventory_matches_worker_snapshots_and_selected_diarizer(tmp_path, models, expected):
    with TestClient(create_app(settings(tmp_path, models))) as client:
        response = client.get("/v1/profiles", headers={"X-User-ID": "synthetic-user"})
        assert response.status_code == 200
        rows = response.json()["profiles"]
        assert {r["name"] for r in rows if r["available"]} == expected
        assert all("nemotron" in r["diarization_backend"] for r in rows)
        assert all(r["unavailable_reason"] for r in rows if not r["available"])
        assert str(tmp_path) not in response.text


def submit(client, name, translate=False):
    return client.post("/v1/jobs", headers={"X-User-ID": "synthetic-user"},
                       data={"options": json.dumps({"profile": name, "translate_to_english": translate})},
                       files=[("files", ("synthetic.wav", b"synthetic media", "audio/wav"))])


def test_unavailable_fast_cannot_be_submitted_by_bypassing_ui(tmp_path):
    app = create_app(settings(tmp_path, ["large-v3"]))
    with TestClient(app) as client:
        response = submit(client, "fast")
        assert response.status_code == 409
        assert response.json() == {"code": "profile_unavailable"}
        assert app.state.runtime.store.retained_usage() == (0, 0)
        assert submit(client, "balanced").status_code == 201


def test_turbo_only_does_not_authorize_english_translation(tmp_path):
    app = create_app(settings(tmp_path, ["turbo"]))
    with TestClient(app) as client:
        rows = client.get("/v1/profiles", headers={"X-User-ID": "synthetic-user"}).json()["profiles"]
        fast = next(r for r in rows if r["name"] == "fast")
        assert fast["available"] is True and fast["translation_available"] is False
        assert submit(client, "fast", translate=True).status_code == 409
        assert app.state.runtime.store.retained_usage() == (0, 0)
        assert submit(client, "fast").status_code == 201


@pytest.mark.parametrize("change", ["manifest", "weights", "extra", "link"])
def test_changed_inventory_fails_closed_before_enqueue(tmp_path, change):
    config = settings(tmp_path, ["large-v3"])
    app = create_app(config)
    snapshot = next(config.model_cache_dir.glob("models--*/snapshots/*"))
    if change == "manifest":
        config.approved_model_manifest_path.write_text("{}")
    elif change == "weights":
        (snapshot / "model.bin").write_bytes(b"changed")
    elif change == "extra":
        (snapshot / "unapproved.json").write_text("{}")
    else:
        outside = tmp_path / "outside"
        outside.write_bytes((snapshot / "model.bin").read_bytes())
        (snapshot / "model.bin").unlink()
        (snapshot / "model.bin").symlink_to(outside)
    with TestClient(app) as client:
        assert submit(client, "balanced").status_code == 409
        assert app.state.runtime.store.retained_usage() == (0, 0)


def test_missing_inventory_keeps_health_available_without_admitting_work(tmp_path):
    config = settings(tmp_path, [])
    config.approved_model_manifest_path.unlink()
    app = create_app(config)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert submit(client, "high_accuracy").status_code == 409
        assert app.state.runtime.store.retained_usage() == (0, 0)


def test_real_api_hashes_inventory_once_and_never_imports_ml(tmp_path):
    config = settings(tmp_path, ["large-v3"])
    from transcription_v2 import profile_availability as module
    with patch.object(module, "verify_model_manifest", wraps=module.verify_model_manifest) as verify:
        gate = ProfileAvailability(config)
        for _ in range(3):
            assert gate.available(get_profile("balanced"))
        assert verify.call_count == 1


def test_ui_operator_configuration_cannot_enable_unstaged_option(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_V2_PROFILE_NAMES", "fast,balanced,high_accuracy")
    client = MagicMock()
    client.profiles.return_value = {"profiles": [
        {"name": "balanced", "available": True}, {"name": "fast", "available": False},
        {"name": "high_accuracy", "available": True}]}
    assert _available_profiles(client) == {"Balanced": "balanced", "Highest accuracy": "high_accuracy"}
    monkeypatch.setenv("TRANSCRIPTION_V2_PROFILE_NAMES", "invalid")
    assert _available_profiles(client) == {}


@pytest.mark.parametrize("payload", [{}, {"profiles": []}, {"profiles": "bad"},
                                     {"profiles": [{"name": "balanced"}]}])
def test_ui_never_invents_a_ready_profile(payload, monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_V2_PROFILE_NAMES", raising=False)
    client = MagicMock()
    client.profiles.return_value = payload
    st = MagicMock()
    st.session_state = {}
    _render_new_job(st, client)
    st.info.assert_called_once()
    st.file_uploader.assert_not_called()
    client.submit_job.assert_not_called()


def test_ui_profile_lookup_failure_does_not_offer_upload():
    client, st = MagicMock(), MagicMock()
    client.profiles.side_effect = ApiError("Synthetic failure")
    _render_new_job(st, client)
    st.error.assert_called_once()
    st.file_uploader.assert_not_called()


def test_unavailable_profile_error_is_not_described_as_transcript_edit_conflict():
    error = ApiError.from_status(409, code="profile_unavailable")
    assert "processing option" in str(error)
    assert "transcript changed" not in str(error)


@pytest.mark.parametrize("language", ["en", "es"])
def test_supported_alignment_uses_approved_bundle_before_any_loader(tmp_path, monkeypatch, language):
    from types import SimpleNamespace
    import sys
    from transcription_v2.alignment_models import ALIGNMENT_MODELS
    from transcription_v2.model_manifest import verify_model_manifest
    from transcription_v2.pipeline import LocalWhisperXEngine, TranscriptionRequest
    config = settings(tmp_path, ["large-v3"])
    verified = verify_model_manifest(config.model_cache_dir, config.approved_model_manifest_path)
    engine = LocalWhisperXEngine(model_cache_dir=config.model_cache_dir, model_readiness=verified)
    calls = []
    monkeypatch.setitem(sys.modules, "nltk", SimpleNamespace(data=SimpleNamespace(find=lambda _: object())))
    monkeypatch.setitem(sys.modules, "whisperx.utils", SimpleNamespace(PUNKT_LANGUAGES={}))
    fake = SimpleNamespace(load_align_model=lambda **kw: calls.append(kw) or (object(), {}),
                           load_audio=lambda _: [], align=lambda *args, **kw: {"segments": []})
    monkeypatch.setattr(engine, "_import_whisperx", lambda: fake)
    engine.align_source(TranscriptionRequest(audio_path=tmp_path / "synthetic.wav", device="cpu"),
                        get_profile("balanced"), {"language": language, "segments": []})
    assert calls[0]["model_name"] == ALIGNMENT_MODELS[language][0]
    assert calls[0]["model_cache_only"] is True


@pytest.mark.parametrize("change", ["unsupported-language", "missing", "changed", "override"])
def test_alignment_never_reaches_loader_for_unapproved_request(tmp_path, monkeypatch, change):
    from transcription_v2.alignment_models import ALIGNMENT_MODELS
    from transcription_v2.model_manifest import verify_model_manifest
    from transcription_v2.pipeline import LocalWhisperXEngine, TranscriptionRequest
    config = settings(tmp_path, ["large-v3"])
    verified = verify_model_manifest(config.model_cache_dir, config.approved_model_manifest_path)
    engine = LocalWhisperXEngine(model_cache_dir=config.model_cache_dir, model_readiness=verified)
    monkeypatch.setattr(engine, "_import_whisperx", lambda: pytest.fail("unapproved alignment reached ML loader"))
    checkpoint = config.model_cache_dir / ALIGNMENT_MODELS["en"][1]
    if change == "missing": checkpoint.unlink()
    if change == "changed": checkpoint.write_bytes(b"changed")
    requested = "unapproved-model" if change == "override" else None
    language = "fr" if change == "unsupported-language" else "en"
    with pytest.raises(ValueError, match="not approved and staged") as caught:
        engine.align_source(TranscriptionRequest(audio_path=tmp_path / "synthetic.wav", device="cpu", alignment_model=requested),
                            get_profile("balanced"), {"language": language, "segments": []})
    assert str(tmp_path) not in str(caught.value)


def test_explicit_unstaged_language_is_rejected_before_job_creation(tmp_path):
    app = create_app(settings(tmp_path, ["large-v3"]))
    with TestClient(app) as client:
        response = client.post("/v1/jobs", headers={"X-User-ID": "synthetic-user"},
                               data={"options": json.dumps({"source_language": "fr"})},
                               files=[("files", ("synthetic.wav", b"synthetic", "audio/wav"))])
        assert response.status_code == 409
        assert response.json() == {"code": "language_unavailable"}
        assert app.state.runtime.store.retained_usage() == (0, 0)
        assert client.get("/v1/profiles", headers={"X-User-ID": "synthetic-user"}).json()["languages"] == ["en", "es"]


def test_ui_never_falls_back_to_an_unstaged_language(monkeypatch):
    from ui.streamlit_app import _available_languages
    monkeypatch.setenv("TRANSCRIPTION_V2_LANGUAGE_CODES", "auto,en,es,fr")
    client = MagicMock()
    client.profiles.return_value = {"languages": ["es"]}
    assert _available_languages(client) == {"Auto-detect (recommended)": None, "Spanish": "es"}
    client.profiles.return_value = {"languages": []}
    assert _available_languages(client) == {}


def test_nemotron_api_readiness_does_not_probe_worker_interpreter(tmp_path, monkeypatch):
    import transcription_v2.resources as resources
    configuration = settings(tmp_path, ["large-v3"])
    assert configuration.diarization_backend == "nemotron"
    assert not configuration.allow_degraded_diarization
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("TRANSCRIPTION_V2_NEMOTRON_PYTHON", str(tmp_path / "worker-only-python"))
    monkeypatch.setattr(resources, "_diarization_config_present", lambda *a, **kw: pytest.fail("API inspected worker interpreter"))
    monkeypatch.setattr(resources, "gpu_states", lambda: pytest.fail("API inspected worker GPU"))
    monkeypatch.setattr(resources, "_whisperx_runtime_checks", lambda: pytest.fail("API inspected worker packages"))
    monkeypatch.setattr(resources.shutil, "which", lambda name: "/usr/bin/" + name)
    with TestClient(create_app(configuration)) as client:
        response = client.get("/ready", headers={"X-User-ID": "synthetic-user"})
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert response.json()["scope"] == "queue"
        (configuration.model_cache_dir / "approved-model-manifest.json").unlink()
        assert client.get("/ready", headers={"X-User-ID": "synthetic-user"}).json()["status"] == "not_ready"


def test_missing_punkt_blocks_languages_and_job_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("NLTK_DATA", str(tmp_path / "absent-tokenizers"))
    app = create_app(settings(tmp_path, ["large-v3"]))
    with TestClient(app) as client:
        headers = {"X-User-ID": "synthetic-user"}
        assert client.get("/v1/profiles", headers=headers).json()["languages"] == []
        response = submit(client, "balanced")
        assert response.status_code == 409
        assert response.json() == {"code": "language_unavailable"}
        assert app.state.runtime.store.retained_usage() == (0, 0)


@pytest.mark.parametrize("failure", ["missing-file", "directory", "file-link", "language-link", "unreadable"])
def test_punkt_inventory_changes_remove_only_affected_language(tmp_path, punkt_inventory, failure):
    app = create_app(settings(tmp_path, ["large-v3"]))
    directory = punkt_inventory / "tokenizers/punkt_tab/spanish"
    target = directory / "ortho_context.tab"
    if failure == "unreadable":
        target.chmod(0)
    elif failure == "language-link":
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    else:
        target.unlink()
        if failure == "directory":
            target.mkdir()
        elif failure == "file-link":
            target.symlink_to(directory / "abbrev_types.txt")
    with TestClient(app) as client:
        headers = {"X-User-ID": "synthetic-user"}
        assert client.get("/v1/profiles", headers=headers).json()["languages"] == ["en"]
        response = client.post("/v1/jobs", headers=headers,
            data={"options": json.dumps({"source_language": "es"})},
            files=[("files", ("synthetic.wav", b"synthetic", "audio/wav"))])
        assert response.status_code == 409
        assert response.json() == {"code": "language_unavailable"}
        assert app.state.runtime.store.retained_usage() == (0, 0)


def test_worker_rejects_lost_punkt_before_importing_model(tmp_path, punkt_inventory, monkeypatch):
    from transcription_v2.model_manifest import verify_model_manifest
    from transcription_v2.pipeline import LocalWhisperXEngine, TranscriptionRequest
    config = settings(tmp_path, ["large-v3"])
    readiness = verify_model_manifest(config.model_cache_dir, config.approved_model_manifest_path)
    engine = LocalWhisperXEngine(model_cache_dir=config.model_cache_dir, model_readiness=readiness)
    (punkt_inventory / "tokenizers/punkt_tab/english/abbrev_types.txt").unlink()
    monkeypatch.setattr(engine, "_import_whisperx", lambda: pytest.fail("missing tokenizer reached ML import"))
    with pytest.raises(ValueError, match="tokenizer data"):
        engine.align_source(TranscriptionRequest(audio_path=tmp_path / "synthetic.wav", device="cpu"),
                            get_profile("balanced"), {"language": "en", "segments": []})


def test_punkt_checks_use_configured_search_order_without_imports(tmp_path, punkt_inventory, monkeypatch):
    import builtins
    import os
    from transcription_v2.tokenizer_resources import punkt_language_ready
    original = builtins.__import__
    def no_ml(name, *args, **kwargs):
        if name.split(".")[0] in {"nltk", "torch", "whisperx"}:
            pytest.fail("tokenizer readiness imported a model dependency")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_ml)
    monkeypatch.delenv("NLTK_DATA")
    assert not punkt_language_ready("en")
    monkeypatch.setenv("NLTK_DATA", os.pathsep.join([str(tmp_path / "absent"), str(punkt_inventory)]))
    assert punkt_language_ready("en")
    assert punkt_language_ready("es")
    assert not punkt_language_ready("fr")
    early = tmp_path / "early"
    (early / "tokenizers/punkt_tab/english").mkdir(parents=True)
    monkeypatch.setenv("NLTK_DATA", os.pathsep.join([str(early), str(punkt_inventory)]))
    assert not punkt_language_ready("en")
