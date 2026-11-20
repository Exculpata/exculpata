"""Opt-in migration from a saved bundle, with synthetic node and Docker commands."""
import json
import subprocess
import sys

import pytest

from tests.test_oss_installer import ROOT, installer, saved_gpu_node


@pytest.mark.parametrize("bundle,profiles", [("review", []), ("transcription", ["transcription-history"])])
@pytest.mark.parametrize("rollback", [False, True])
def test_update_an_existing_bundle(tmp_path, monkeypatch, bundle, profiles, rollback):
    root, initial, _ = saved_gpu_node(tmp_path, models=bundle)
    app = installer._app_env(root)
    if bundle == "review":
        installer._replace_env(app, "CASE_INTELLIGENCE_GENERATOR_URL", "http://generator:8000")
        installer._replace_env(app, "CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    else:
        installer._replace_env(app, "CASE_INTELLIGENCE_TRANSCRIPTION_URL", "http://transcription-api:8510")
    installer._replace_env(app, "CASE_INTELLIGENCE_EMBEDDING_URL", "https://embeddings.example.test")
    original = {path: path.read_bytes() for path in (app, root / "compose.env", root / "installation.json")}
    commands = []
    monkeypatch.setattr(installer, "_preflight", lambda console, **options: (
        None if options["models"] == "none" else pytest.fail("retirement selected a GPU")))
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **kw: (initial["release_id"], ROOT))
    monkeypatch.setattr(installer, "_verify_installed_package", lambda root: None)
    monkeypatch.setattr(installer, "_wait_health", lambda *a, **kw: None)
    monkeypatch.setattr(installer, "_handoff", lambda *a, **kw: None)

    def run(console, command, **kwargs):
        commands.append(command)
        if rollback and "build" in command:
            raise RuntimeError("synthetic failed image build")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(sys, "argv", ["install", "update", "--root", str(root),
                                      "--retire-bundled-models", "--no-backup"])
    assert installer.main() == int(rollback)
    assert any("stop" in cmd for cmd in commands)
    if rollback:
        assert {p: p.read_bytes() for p in original} == original
        assert any("up" in cmd for cmd in commands)
        return
    saved = json.loads((root / "installation.json").read_text())
    assert saved["models"] == "none" and saved["profiles"] == profiles
    assert saved["storage_root"] == initial["storage_root"]
    assert installer._dotenv(app)["CASE_INTELLIGENCE_EMBEDDING_URL"] == "https://embeddings.example.test"
    assert installer._dotenv(root / "compose.env")["COMPOSE_PROFILES"] == ",".join(profiles)
    assert installer._dotenv(root / "compose.env")["CASE_INTELLIGENCE_RETRIEVAL_WORKER_URL"] == ""
    if bundle == "review":
        assert installer._dotenv(app)["CASE_INTELLIGENCE_GENERATOR_BACKEND"] == ""
    else:
        assert installer._dotenv(app)["CASE_INTELLIGENCE_TRANSCRIPTION_HISTORY_ONLY"] == "1"
    targets = next(cmd[cmd.index("build") + 1:] for cmd in commands if "build" in cmd)
    assert not {"retrieval", "generator", "transcription-worker", "model-stager"}.intersection(targets)
    assert ("transcription-api" in targets) == bool(profiles)
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume"])
    installer._saved_node_arguments(args, root)
    assert args.models == "none"

def test_interrupted_cutover_restores_saved_config_before_reading_state(tmp_path, monkeypatch):
    root, _, _ = saved_gpu_node(tmp_path, models="review")
    migration = installer.bundle_migration
    app_path = installer._app_env(root)
    paths = migration.configuration(root, app_path)
    before = {path: path.read_bytes() for path in paths}
    migration.write_journal(root, paths, installer._atomic_private_write)
    installer._atomic_private_write(root / "installation.json", "{}")
    installer._replace_env(root / "compose.env", "COMPOSE_PROFILES", "")
    def stop_after_recovery(*args, **kwargs):
        raise RuntimeError("stop after recovery")

    monkeypatch.setattr(installer, "_preflight", stop_after_recovery)
    monkeypatch.setattr(installer, "_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["install", "update", "--root", str(root), "--retire-bundled-models", "--no-backup"])
    assert installer.main() == 1
    assert {p: p.read_bytes() for p in paths} == before
    assert migration.journal_exists(root)


def test_private_history_client_never_submits_a_recording(tmp_path, monkeypatch):
    from case_intelligence.media_evidence import TranscriptionV2Client, MediaProcessorError

    monkeypatch.setenv("CASE_INTELLIGENCE_TRANSCRIPTION_ALLOWED_HOSTS", "transcription-api")
    client = TranscriptionV2Client("http://transcription-api:8510", tmp_path / "token", history_only=True)
    monkeypatch.setattr(client, "_request", lambda *a, **kw: pytest.fail("unexpected private upload"))
    with pytest.raises(MediaProcessorError, match="past jobs"):
        client.submit("synthetic-owner", tmp_path / "recording", "audio/wav", "a" * 64)
