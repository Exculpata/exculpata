from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from case_intelligence.local_accounts import LocalAccountRepository
from tests.test_oss_installer import installer

ROOT = Path(__file__).parents[1]
PASSWORD = "synthetic-handoff-password"


def configured_node(tmp_path, *, storage_root=None, names="recordbench"):
    root = tmp_path / "Synthetic Node"
    console = installer.Console(color=False)
    args = installer._parser().parse_args(["install", "--names", names, "--auth", "local", "--enable-account-management",
        "--models", "none", "--non-interactive", "--prepare-only", "--admin-username", "alice.admin", "--admin-display-name", "Alice Administrator"])
    installer._collect_identity_choices(args)
    paths = installer._prepare_directories(console, root, storage_root=storage_root, resume=False, dry_run=False)
    cert, key = paths["tls"] / "synthetic.crt", paths["tls"] / "synthetic.key"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    args.tls_cert, args.tls_key = cert, key
    installer._configure(console, args, root, paths, "synthetic-release", ROOT, ())
    return root, args, paths


def test_prepared_handoff_does_not_claim_browser_login_or_running(tmp_path, capsys):
    root, args, paths = configured_node(tmp_path)
    installer._install_phase(root, "prepared", "complete")
    installer._handoff(installer.Console(color=False), root)
    output = capsys.readouterr().out
    assert "https://recordbench.example.test:8443/auth/login" in output
    assert "Initial administrator username: alice.admin" in output
    assert "prepared: complete" in output and "running: not verified" in output
    assert "Browser sign-in: not verified" in output and "ACCESS GRANTED" not in output
    assert "'" + str(root) + "' --resume" in output
    assert all(path.read_text().strip() not in output for path in paths["secrets"].iterdir() if path.is_file())
    receipt = root / "state/install-progress.json"
    assert receipt.stat().st_mode & 0o777 == 0o600
    value = json.loads(receipt.read_text())
    assert value["format_version"] == 1
    assert "alice.admin" not in receipt.read_text()


@pytest.mark.parametrize("interrupted_after", ["configuration", "administrator", "models"])
def test_resume_keeps_identity_sources_secrets_and_verified_models(tmp_path, monkeypatch, interrupted_after):
    root, args, paths = configured_node(tmp_path)
    installation = json.loads((root / "installation.json").read_text())
    installation["models"] = "review"
    (root / "installation.json").write_text(json.dumps(installation))
    args.models = "review"
    counts = {"account": 0, "stage": 0, "storage": 0}
    repository = LocalAccountRepository(paths["accounts"] / "local-accounts.json")
    model = paths["models"] / "synthetic-pinned-model"
    source = paths["storage"] / "synthetic-source.txt"
    secrets_before = {p.name: p.read_bytes() for p in paths["secrets"].iterdir() if p.is_file()}
    monkeypatch.setattr(installer, "_password", lambda args: PASSWORD)
    monkeypatch.setattr(installer, "_preflight", lambda *a, **kw: ())
    def command(console, command, **kwargs):
        if "accounts" in command and "init" in command:
            counts["account"] += 1
            repository.initialize("alice.admin", "Alice Administrator", PASSWORD, actor="synthetic-operator")
        elif "accounts" in command and "list" in command:
            assert repository.read()["alice.admin"].enabled
        elif "storage" in command and "init" in command:
            counts["storage"] += 1
            (paths["storage"] / ".recordbench-managed-storage.json").write_text("synthetic marker")
            source.write_text("synthetic source remains intact")
        elif "storage" in command and "status" in command:
            assert source.read_text() == "synthetic source remains intact"
        elif "model-stager" in command and "verify" in command:
            return subprocess.CompletedProcess(command, 0 if model.exists() else 1, "", "")
        elif "model-stager" in command and "stage" in command:
            counts["stage"] += 1
            model.write_text("synthetic pinned model")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(installer, "_run", command)
    phase = installer._install_phase
    def interrupt(root, name, state):
        phase(root, name, state)
        if name == interrupted_after and state == "complete":
            raise RuntimeError("synthetic interruption")
    if interrupted_after != "configuration":
        monkeypatch.setattr(installer, "_install_phase", interrupt)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            installer._provision(installer.Console(color=False), args, root, "local", "review", "alice.admin", "Alice Administrator")
    before_account = repository.path.read_bytes() if repository.path.exists() else None
    monkeypatch.setattr(installer, "_install_phase", phase)
    resumed = installer._parser().parse_args(["install", "--resume", "--prepare-only", "--non-interactive"])
    installer._resume_node(installer.Console(color=False), resumed, root)
    after_account = repository.path.read_bytes()
    if before_account is not None:
        assert after_account == before_account
    installer._resume_node(installer.Console(color=False), resumed, root)
    assert repository.path.read_bytes() == after_account
    assert counts == {"account": 1, "stage": 1, "storage": 1}
    assert source.read_text() == "synthetic source remains intact"
    assert {p.name: p.read_bytes() for p in paths["secrets"].iterdir() if p.is_file()} == secrets_before
    assert installer._install_progress(root)["phases"]["prepared"] == "complete"


def test_health_records_useful_cpu_capability_without_claiming_failed_model_ready(tmp_path, monkeypatch):
    root, _, _ = configured_node(tmp_path)
    installation = json.loads((root / "installation.json").read_text())
    installation["models"] = "review"
    (root / "installation.json").write_text(json.dumps(installation))
    payload = {"product": "RecordBench", "status": "degraded", "storage": {"status": "ready"},
        "capabilities": {"source_review": "ready", "malware_scan": "ready", "answering": "temporarily unavailable", "search": "word search only"}}
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(installer, "_login_reachable", lambda root: True)
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0, '{"auth_mode": "local"}', ""))
    clock = iter([0, 0, 1, 2, 901])
    monkeypatch.setattr(installer.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(installer.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="selected capabilities"):
        installer._wait_health(installer.Console(color=False), root)
    phases = installer._install_progress(root)["phases"]
    assert phases["running"] == phases["basic_review"] == phases["login_reachable"] == "complete"
    assert phases["selected_capabilities"] == "incomplete"


def test_model_resume_receipt_verifies_exact_bytes_without_network_or_writes(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("synthetic_model_stager", ROOT / "scripts/stage-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"schema_version": 1, "models": [{"revision": "a" * 40, "license": "Apache-2.0"}]}))
    root = tmp_path / "models"
    root.mkdir()
    artifact = root / "synthetic-model.bin"
    artifact.write_bytes(b"synthetic pinned artifact")
    groups = frozenset({"review"})
    module._stage_receipt(root, catalog, groups, "portable", [artifact])
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("verification must stay offline"))
    module._verify_stage(root, catalog, groups, "portable")
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()} == before
    artifact.write_bytes(b"synthetic changed artifact")
    with pytest.raises(RuntimeError, match="missing or changed"):
        module._verify_stage(root, catalog, groups, "portable")
    with pytest.raises(RuntimeError, match="selection changed"):
        module._verify_stage(root, catalog, groups, "quality")


@pytest.mark.parametrize("mutation", ["remove-link", "repoint-link", "replace-with-file", "change-blob"])
def test_model_receipt_binds_snapshot_links_as_well_as_blob_bytes(tmp_path, mutation):
    spec = importlib.util.spec_from_file_location("synthetic_link_stager", ROOT / "scripts/stage-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"schema_version": 1, "models": []}))
    root = tmp_path / "models"
    repo = root / "huggingface/hub/models--synthetic--model"
    blobs = repo / "blobs"
    snapshot = repo / "snapshots" / ("a" * 40)
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    blob = blobs / "synthetic-blob"
    blob.write_bytes(b"synthetic model bytes")
    link = snapshot / "weights.bin"
    link.symlink_to("../../blobs/synthetic-blob")
    groups = frozenset({"review"})
    module._stage_receipt(root, catalog, groups, "portable", [link])
    module._verify_stage(root, catalog, groups, "portable")
    if mutation == "remove-link":
        link.unlink()
    elif mutation == "repoint-link":
        (blobs / "same-bytes-different-blob").write_bytes(blob.read_bytes())
        link.unlink()
        link.symlink_to("../../blobs/same-bytes-different-blob")
    elif mutation == "replace-with-file":
        link.unlink()
        link.write_bytes(blob.read_bytes())
    else:
        blob.write_bytes(b"synthetic altered bytes")
    assert blob.exists()
    with pytest.raises(RuntimeError, match="missing or changed"):
        module._verify_stage(root, catalog, groups, "portable")


@pytest.mark.parametrize("added", ["file", "nested-file", "file-link", "broken-link", "directory-link"])
@pytest.mark.parametrize("scope", ["snapshot", "tokenizer"])
def test_model_receipt_rejects_unrecorded_snapshot_artifacts(tmp_path, monkeypatch, added, scope):
    spec = importlib.util.spec_from_file_location("synthetic_inventory_stager", ROOT / "scripts/stage-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema_version": 1, "models": []}')
    root = tmp_path / "models"
    snapshot = (root / "huggingface/hub/models--synthetic--model/snapshots" / ("c" * 40)
                if scope == "snapshot" else root / "nltk_data/tokenizers/punkt_tab/en")
    snapshot.mkdir(parents=True)
    artifact = snapshot / "weights.bin"
    artifact.write_bytes(b"synthetic pinned bytes")
    groups = frozenset({"review"})
    module._stage_receipt(root, catalog, groups, "portable", [artifact])
    module._verify_stage(root, catalog, groups, "portable")
    candidate = snapshot / "unrecorded.json"
    if added == "file":
        candidate.write_text('{"synthetic": "configuration"}')
    elif added == "nested-file":
        (snapshot / "tokenizer").mkdir()
        (snapshot / "tokenizer/unrecorded.json").write_text('{"synthetic": "tokenizer"}')
    elif added == "file-link":
        candidate.symlink_to(artifact.name)
    elif added == "broken-link":
        candidate.symlink_to("missing-artifact")
    else:
        extra_directory = root / "unselected-content"
        extra_directory.mkdir()
        (extra_directory / "config.json").write_text('{"synthetic": "extra"}')
        candidate.symlink_to(extra_directory)
    before = (root / ".recordbench-stage-receipt.json").read_bytes()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: pytest.fail("inventory verification must stay offline"))
    with pytest.raises(RuntimeError, match="inventory|unrecorded|linked directory"):
        module._verify_stage(root, catalog, groups, "portable")
    assert (root / ".recordbench-stage-receipt.json").read_bytes() == before


def test_model_receipt_limits_inventory_to_selected_snapshot(tmp_path):
    spec = importlib.util.spec_from_file_location("synthetic_selected_inventory", ROOT / "scripts/stage-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema_version": 1, "models": []}')
    root = tmp_path / "models"
    snapshots = root / "repo/snapshots"
    selected = snapshots / ("a" * 40)
    selected.mkdir(parents=True)
    artifact = selected / "weights.bin"
    artifact.write_bytes(b"synthetic selected bytes")
    groups = frozenset({"review"})
    module._stage_receipt(root, catalog, groups, "portable", [artifact])
    unselected = snapshots / ("b" * 40)
    unselected.mkdir()
    (unselected / "config.json").write_text('{"synthetic": "unselected"}')
    module._verify_stage(root, catalog, groups, "portable")
    (selected / "unrecorded.json").write_text('{"synthetic": "selected"}')
    with pytest.raises(RuntimeError, match="inventory"):
        module._stage_receipt(root, catalog, groups, "portable", [artifact])


@pytest.mark.parametrize("command", ["resume", "update"])
@pytest.mark.parametrize("boundary", ["node", "storage"])
def test_resume_and_update_refuse_changed_storage_ancestry_before_any_compose(tmp_path, monkeypatch, command, boundary):
    import os
    storage_parent = tmp_path / "separate-storage-parent"
    storage_parent.mkdir(mode=0o700)
    root, _, paths = configured_node(tmp_path, storage_root=storage_parent / "managed")
    changed_parent = tmp_path if boundary == "node" else storage_parent
    changed_parent.chmod(0o777)
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/" + name)

    def no_compose_probe(command):
        if "compose" in command:
            pytest.fail("unsafe saved storage reached a Compose diagnostic")
        return subprocess.CompletedProcess(command, 0, "1.0", "")

    monkeypatch.setattr(installer, "_probe", no_compose_probe)
    monkeypatch.setattr(installer, "_run", lambda *a, **kw: pytest.fail("unsafe saved storage reached an external command"))
    args = installer._parser().parse_args(["install" if command == "resume" else "update", "--root", str(root), "--resume", "--no-backup"])
    action = installer._resume_node if command == "resume" else installer._update
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    try:
        with pytest.raises(RuntimeError, match="storage"):
            action(installer.Console(color=False, quiet=True), args, root)
        assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    finally:
        changed_parent.chmod(0o700)


def test_model_receipt_retains_distinct_snapshot_paths_sharing_a_blob(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("synthetic_shared_blob_stager", ROOT / "scripts/stage-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema_version": 1}')
    root = tmp_path / "models"
    blobs = root / "repo/blobs"
    snapshot = root / "repo/snapshots" / ("b" * 40)
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    (blobs / "shared").write_bytes(b"synthetic shared bytes")
    links = [snapshot / "weights.bin", snapshot / "duplicate.bin"]
    for link in links:
        link.symlink_to("../../blobs/shared")
    module._stage_receipt(root, catalog, frozenset({"review"}), "portable", links)
    receipt = root / ".recordbench-stage-receipt.json"
    value = json.loads(receipt.read_text())
    assert value["format_version"] == 2
    assert set(value["files"]) == {link.relative_to(root).as_posix() for link in links}
    for metadata in value["files"].values():
        assert metadata["symlink_target"] == "../../blobs/shared"
        assert metadata["resolved_path"] == "repo/blobs/shared"
        assert metadata["sha256"] == module._sha256(blobs / "shared")
    before = receipt.read_bytes(), receipt.stat().st_mtime_ns
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("verification must stay offline"))
    module._verify_stage(root, catalog, frozenset({"review"}), "portable")
    assert (receipt.read_bytes(), receipt.stat().st_mtime_ns) == before
    value["format_version"] = 1
    receipt.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="selection changed"):
        module._verify_stage(root, catalog, frozenset({"review"}), "portable")


@pytest.mark.parametrize("kind", ["broken-link", "outside-target", "linked-directory"])
def test_model_receipt_rejects_unsafe_snapshot_relationships(tmp_path, kind):
    spec = importlib.util.spec_from_file_location("synthetic_unsafe_link_stager", ROOT / "scripts/stage-models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "models"
    root.mkdir()
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"schema_version": 1}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "artifact.bin").write_bytes(b"synthetic outside bytes")
    candidate = root / "snapshot.bin"
    if kind == "broken-link":
        candidate.symlink_to("missing")
    elif kind == "outside-target":
        candidate.symlink_to(outside / "artifact.bin")
    else:
        (root / "linked-directory").symlink_to(outside)
        candidate = root / "linked-directory/artifact.bin"
    with pytest.raises((OSError, RuntimeError)):
        module._stage_receipt(root, catalog, frozenset({"review"}), "portable", [candidate])
    assert not (root / ".recordbench-stage-receipt.json").exists()


@pytest.mark.parametrize("command", ["resume", "update"])
def test_resume_and_update_preflight_uses_saved_coordinates_and_model_options(tmp_path, monkeypatch, command):
    # Provisioning is stubbed in this coordinate/input test; supply its one-use bootstrap credential.
    monkeypatch.setattr(installer, '_password', lambda args: 'synthetic-bootstrap-password')
    storage = tmp_path / "separate-storage"
    root, _, paths = configured_node(tmp_path, storage_root=storage)
    installation = json.loads((root / "installation.json").read_text())
    installation.update(models="review", review_model_profile="quality", transcription_languages=["es"],
                        server_name="synthetic-node.example.test", profiles=["ai"])
    (root / "installation.json").write_text(json.dumps(installation))
    for key, value in {"RECORDBENCH_SERVER_NAME": "synthetic-node.example.test", "RECORDBENCH_HTTPS_PORT": "9443",
                       "RECORDBENCH_BIND_ADDRESS": "0.0.0.0", "RECORDBENCH_GENERATOR_GPU": "7",
                       "RECORDBENCH_GENERATOR_GPU_UTILIZATION": "0.72", "COMPOSE_PROFILES": "ai"}.items():
        installer._replace_env(root / "compose.env", key, value)
    calls = []

    def preflight(console, *, models, dry_run, args, needs_model_staging, defer_gpu_free_check=False):
        assert args.root == root and args.storage_root == storage
        assert args.auth == "local" and models == args.models == "review"
        assert args.enable_account_management and args.resume
        assert args.server_name == "synthetic-node.example.test" and args.https_port == 9443
        assert args.bind_address == "0.0.0.0"
        assert args.tls_cert == paths["tls"] / "synthetic.crt"
        assert args.tls_key == paths["tls"] / "synthetic.key"
        assert args.review_model_profile == "quality" and args.generator_gpus == "7"
        assert args.generator_gpu_utilization == 0.72 and args.transcription_languages == "es"
        assert needs_model_staging is (command == "resume")
        assert defer_gpu_free_check is (command == "update")
        calls.append("validated")
        return ()

    monkeypatch.setattr(installer, "_preflight", preflight)
    monkeypatch.setattr(installer, "_run", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(installer, "_provision", lambda *a, **kw: calls.append("provisioned"))
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **kw: (installation["release_id"], ROOT))
    monkeypatch.setattr(installer, "_doctor", lambda *a, **kw: calls.append("doctor"))
    args = installer._parser().parse_args(["install" if command == "resume" else "update", "--root", str(root),
        "--no-backup", "--auth", "oidc", "--models", "none", "--server-name", "ignored.example.test",
        "--storage-root", str(tmp_path / "ignored-storage"), "--review-model-profile", "portable"])
    action = installer._resume_node if command == "resume" else installer._update
    action(installer.Console(color=False, quiet=True), args, root)
    assert calls == ["validated", "provisioned" if command == "resume" else "doctor"]
    assert not (tmp_path / "ignored-storage").exists()


@pytest.mark.parametrize("mutation", ["storage-coordinate", "runtime-symlink", "config-symlink"])
def test_resume_refuses_conflicting_or_replaced_saved_mounts_before_external_commands(tmp_path, monkeypatch, mutation):
    root, _, paths = configured_node(tmp_path)
    if mutation == "storage-coordinate":
        other = tmp_path / "other-storage"
        other.mkdir(mode=0o700)
        installer._replace_env(root / "compose.env", "RECORDBENCH_STORAGE_ROOT", str(other))
    else:
        name = "runtime" if mutation == "runtime-symlink" else "config"
        retained = root / (name + "-retained")
        paths[name].rename(retained)
        paths[name].symlink_to(retained)
    monkeypatch.setattr(installer, "_probe", lambda *a, **kw: pytest.fail("unsafe saved mount reached runtime diagnostics"))
    monkeypatch.setattr(installer, "_run", lambda *a, **kw: pytest.fail("unsafe saved mount reached an external command"))
    args = installer._parser().parse_args(["install", "--resume", "--root", str(root)])
    with pytest.raises(RuntimeError, match="storage"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)


@pytest.mark.parametrize("receipt_state", ["missing", "broken-link", "unrecorded-file", "valid"])
def test_unattended_resume_requires_staging_inputs_only_when_cache_is_not_verified(tmp_path, monkeypatch, receipt_state):
    # Provisioning is stubbed in this coordinate/input test; supply its one-use bootstrap credential.
    monkeypatch.setattr(installer, '_password', lambda args: 'synthetic-bootstrap-password')
    root, _, paths = configured_node(tmp_path)
    installation = json.loads((root / "installation.json").read_text())
    installation.update(models="transcription", transcription_diarization=True, profiles=["transcription"],
                        diarization_backend="community-1")
    env_file = root / "config/transcription.env"
    env_file.write_text(env_file.read_text().replace('TRANSCRIPTION_V2_DIARIZATION_BACKEND="nemotron"',
                                                   'TRANSCRIPTION_V2_DIARIZATION_BACKEND="community-1"'))
    (root / "installation.json").write_text(json.dumps(installation))
    if receipt_state != "missing":
        spec = importlib.util.spec_from_file_location("synthetic_resume_stager", ROOT / "scripts/stage-models.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        artifact = paths["models"] / "synthetic-blob"
        artifact.write_bytes(b"synthetic cached speaker model")
        snapshot = paths["models"] / "repo/snapshots" / ("a" * 40)
        snapshot.mkdir(parents=True)
        link = snapshot / "snapshot.bin"
        link.symlink_to("../../../synthetic-blob")
        module._stage_receipt(paths["models"], ROOT / "config/models.json",
            frozenset({"transcription-asr", "transcription-alignment-en", "transcription-diarization"}), "portable", [link])
        if receipt_state == "broken-link":
            link.unlink()
        elif receipt_state == "unrecorded-file":
            (snapshot / "config.json").write_text('{"synthetic": "unrecorded"}')
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    calls = []

    def preflight(console, **kwargs):
        assert receipt_state == "valid", "missing staging input reached preflight runtime probes"
        assert kwargs["needs_model_staging"] is False
        calls.append("validated")
        return ()

    def run(console, command, **kwargs):
        assert receipt_state == "valid", "missing staging input reached a Compose command"
        calls.append("compose")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer, "_preflight", preflight)
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_provision", lambda *a, **kw: calls.append("provisioned"))
    monkeypatch.setattr(installer, "_hf_token", lambda *a, **kw: pytest.fail("cached resume requested a token"))
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume", "--non-interactive", "--prepare-only"])
    if receipt_state == "valid":
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
        assert calls == ["validated", "compose", "provisioned"]
    else:
        with pytest.raises(RuntimeError, match="model staging"):
            installer._resume_node(installer.Console(color=False, quiet=True), args, root)
        assert calls == []
        assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_resume_keeps_a_safe_external_canonical_account_directory(tmp_path, monkeypatch):
    root, _, paths = configured_node(tmp_path)
    external = tmp_path / "canonical-accounts"
    paths["accounts"].rename(external)
    repository = LocalAccountRepository(external / "local-accounts.json")
    repository.initialize("alice.admin", "Alice Administrator", PASSWORD, actor="synthetic-operator")
    installer._replace_env(root / "compose.env", "RECORDBENCH_LOCAL_ACCOUNT_ROOT", str(external))
    original = repository.path.read_bytes()
    monkeypatch.setattr(installer, "_preflight", lambda *a, **kw: ())
    commands = []

    def run(console, command, **kwargs):
        commands.append(command)
        if "accounts" in command:
            assert "init" not in command
            assert command[-1] == "/var/lib/recordbench-accounts/local-accounts.json"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_password", lambda *a: pytest.fail("existing external accounts were reinitialized"))
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume", "--non-interactive", "--prepare-only"])
    installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert repository.path.read_bytes() == original
    assert args.account_root == external
    assert not paths["accounts"].exists()
    assert any("accounts" in command and "list" in command for command in commands)


@pytest.mark.parametrize('receipt', ['missing', 'previous-release'])
def test_doctor_recovers_prepared_progress_without_reprovisioning(tmp_path, monkeypatch, capsys, receipt):
    root, args, _ = configured_node(tmp_path)
    installation, _ = installer._installed_release(root)
    installer._seal_provisioning(root, installation)
    progress = root / 'state/install-progress.json'
    if receipt == 'missing':
        progress.unlink()
    else:
        value = json.loads(progress.read_text()); value['release_id'] = 'old-release'; progress.write_text(json.dumps(value))
    monkeypatch.setattr(installer, '_run', lambda *a, **k: subprocess.CompletedProcess([], 0, '', ''))
    def healthy(console, root):
        for phase in ('running', 'login_reachable', 'basic_review', 'selected_capabilities'):
            installer._install_phase(root, phase, 'complete')
    monkeypatch.setattr(installer, '_wait_health', healthy)
    installer._doctor(installer.Console(color=False), args, root)
    capsys.readouterr()
    installer._handoff(installer.Console(color=False), root)
    output = capsys.readouterr().out
    assert 'prepared: complete' in output
    assert 'Continue with:' not in output
    assert 'Browser sign-in: not verified' in output


def test_successful_update_handoff_retains_completed_preparation(tmp_path, monkeypatch, capsys):
    root, args, paths = configured_node(tmp_path)
    LocalAccountRepository(paths['accounts'] / 'local-accounts.json').initialize(
        'alice.admin', 'Alice Administrator', PASSWORD, actor='synthetic-operator')
    installation, _ = installer._installed_release(root)
    installer._seal_provisioning(root, installation)
    args.no_backup = True
    monkeypatch.setattr(installer, '_preflight', lambda *a, **k: ())
    monkeypatch.setattr(installer, '_stage_release', lambda *a, **k: ('synthetic-updated-release', ROOT))
    monkeypatch.setattr(installer, '_run', lambda *a, **k: subprocess.CompletedProcess([], 0, '', ''))
    def healthy(console, root):
        for phase in ('running', 'login_reachable', 'basic_review', 'selected_capabilities'):
            installer._install_phase(root, phase, 'complete')
    monkeypatch.setattr(installer, '_wait_health', healthy)
    installer._update(installer.Console(color=False), args, root)
    capsys.readouterr()
    installer._handoff(installer.Console(color=False), root)
    output = capsys.readouterr().out
    assert 'prepared: complete' in output and 'Continue with:' not in output
    assert installer._install_progress(root)['release_id'] == 'synthetic-updated-release'


def test_bootstrap_password_is_read_once_and_consumed_at_account_initialization(tmp_path, monkeypatch):
    root, args, paths = configured_node(tmp_path)
    reads = []
    monkeypatch.setattr(installer, '_password', lambda args: reads.append('read') or PASSWORD)
    values_seen = []
    def command(console, command, **kwargs):
        assert reads == ['read']
        if 'accounts' in command and 'init' in command:
            assert kwargs['input_value'] == PASSWORD + '\n'
            assert values_seen[0] == []
            LocalAccountRepository(paths['accounts'] / 'local-accounts.json').initialize(
                'alice.admin', 'Alice Administrator', PASSWORD, actor='synthetic-operator')
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(installer, '_run', command)
    with installer._bootstrap_password_input(args, root, 'local') as password_input:
        values_seen.append(password_input)
        installer._provision(installer.Console(color=False), args, root, 'local', 'none',
                             'alice.admin', 'Alice Administrator', password_input=password_input)
    assert reads == ['read'] and password_input == []
