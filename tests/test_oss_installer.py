from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import recordbench_install as installer  # noqa: E402


@pytest.mark.parametrize("enabled", [False, True])
def test_account_management_configuration_and_update_overlay(tmp_path, enabled):
    args = installer._parser().parse_args(["install", "--names", "recordbench", "--auth", "local", "--models", "none", "--non-interactive"] + (["--enable-account-management"] if enabled else []))
    installer._collect_identity_choices(args)
    console = installer.Console(color=False)
    paths = installer._prepare_directories(console, tmp_path / "node", storage_root=None, resume=False, dry_run=False)
    root = tmp_path / "node"
    cert, key = paths["tls"] / "synthetic.crt", paths["tls"] / "synthetic.key"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic private key")
    args.tls_cert, args.tls_key = cert, key
    installer._configure(console, args, root, paths, "synthetic-release", ROOT, ())
    application = (paths["config"] / "recordbench.env").read_text()
    record = json.loads((root / "installation.json").read_text())
    assert record["local_account_management"] is enabled
    if enabled:
        assert "/var/lib/recordbench-accounts/local-accounts.json" in application
        assert "CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT" in application
    else:
        assert "/run/recordbench-secrets/local-accounts.json" in application
        assert "CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT" not in application
    for release in (None, ROOT):
        command = installer._compose(root, (), release=release)
        assert (str(ROOT / "compose.local-accounts.yaml") in command) is enabled


@pytest.mark.parametrize("arguments", [
    ["install", "--auth", "oidc"], ["resume", "--auth", "local"], ["install"],
    ["preflight"], ["preflight", "--auth", "oidc"], ["update", "--auth", "local"],
    ["doctor", "--auth", "local"], ["backup", "--auth", "local"], ["restore", "--auth", "local"],
])
def test_management_flag_rejects_ambiguous_or_existing_node_before_writes(tmp_path, arguments):
    root = tmp_path / "node"
    response = subprocess.run([sys.executable, str(ROOT / "scripts/recordbench_install.py"), *arguments,
                               "--root", str(root), "--enable-account-management"], capture_output=True, text=True)
    assert response.returncode != 0
    assert not root.exists()


def test_interrupted_provisioning_resume_retains_canonical_account_profile(tmp_path, monkeypatch):
    # Provisioning is stubbed in this coordinate/input test; supply its one-use bootstrap credential.
    monkeypatch.setattr(installer, '_password', lambda args: 'synthetic-bootstrap-password')
    from tests.test_first_run_handoff import configured_node
    root, _, _ = configured_node(tmp_path)
    monkeypatch.setattr(installer, "_preflight", lambda *args, **kwargs: ())
    monkeypatch.setattr(installer, "_run", lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(installer, "_provision", lambda console, args, *rest, **kwargs: calls.append(args.enable_account_management))
    args = installer._parser().parse_args(["install", "--resume"])
    assert not args.enable_account_management
    installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert calls == [True]


def test_installer_dry_run_has_real_phases_and_writes_nothing(tmp_path, ready_host, monkeypatch, capsys) -> None:
    node = tmp_path.resolve() / "node"
    monkeypatch.setattr(sys, "argv", [
            str(ROOT / "install"),
            "install",
            "--root",
            str(node),
            "--auth",
            "local",
            "--models",
            "none",
            "--non-interactive",
            "--prepare-only",
            "--dry-run",
            "--no-color",
        ])
    assert installer.main() == 0
    output = capsys.readouterr().out
    for marker in (
        "HOST HANDSHAKE",
        "CLAIM NODE STORAGE",
        "LOAD RELEASE CAPSULE",
        "IDENTITY + FRONT DOOR",
        "FORGE RUNTIME",
        "SEAL CONTROL PLANE",
        "NODE ARMED",
        "PLAN COMPLETE",
    ):
        assert marker in output
    assert not node.exists()
    assert "password=" not in output.casefold()
    assert "token=" not in output.casefold()
    assert "ACCESS GRANTED" not in output


def test_capability_profiles_have_conservative_gpu_contracts() -> None:
    assert installer._required_gpu_count("none") == 0
    assert installer._required_gpu_count("review") == 1
    assert installer._required_gpu_count("transcription") == 1
    assert installer._required_gpu_count("all") == 1
    one = (installer.GpuDevice("0", "Synthetic GPU", 24_576, 24_000),)
    shared = installer._gpu_plan("all", one)
    assert shared.layout == "shared"
    assert shared.generator_gpus == ("0",)
    assert shared.transcription_gpu == "0"
    assert shared.retrieval_device == "cpu"

    many = tuple(
        installer.GpuDevice(str(index), f"Synthetic GPU {index}", 48_000, 40_000)
        for index in range(8)
    )
    split = installer._gpu_plan("all", many)
    assert split.layout == "split"
    assert split.generator_gpus == ("0",)
    assert split.transcription_gpu == "1"
    assert split.retrieval_device == "cuda"
    assert split.retrieval_gpu == "2"


def test_automatic_full_profile_resolves_on_one_or_eight_gpus() -> None:
    args = installer._parser().parse_args(["install", "--models", "all"])
    one = (
        installer.GpuDevice(
            "0", "Synthetic 24 GiB GPU", 24_576, 24_000, 8.0
        ),
    )
    one_gpu, one_model = installer._resolve_gpu_plans(args, "all", one)
    assert one_gpu.layout == "shared"
    assert one_gpu.generator_gpus == ("0",)
    assert one_gpu.transcription_gpu == "0"
    assert one_gpu.retrieval_device == "cpu"
    assert one_model.profile == "portable"

    eight = tuple(
        installer.GpuDevice(
            str(index), f"Synthetic GPU {index}", 48_000, 46_000, 8.9
        )
        for index in range(8)
    )
    eight_gpu, eight_model = installer._resolve_gpu_plans(args, "all", eight)
    assert eight_gpu.layout == "split"
    assert eight_gpu.generator_gpus == ("0",)
    assert eight_gpu.transcription_gpu == "1"
    assert eight_gpu.retrieval_gpu == "2"
    assert eight_model.profile == "quality"


def test_gpu_inventory_parses_capability_and_auto_ranks_mixed_cards() -> None:
    devices = installer._parse_gpu_inventory(
        "0, Synthetic Small, 16384, 15000, 7.5\n"
        "1, Synthetic Large Busy, 49152, 12000, 8.9\n"
        "2, Synthetic Large Free, 49152, 47000, 8.9\n"
    )
    plan = installer._gpu_plan("all", devices)
    assert plan.generator_gpus == ("2",)
    assert plan.transcription_gpu == "0"
    assert plan.retrieval_device == "cuda"
    assert plan.retrieval_gpu == "1"


def test_gpu_planner_ignores_incompatible_cards_and_rejects_explicit_use() -> None:
    devices = (
        installer.GpuDevice("0", "Synthetic Legacy", 48_000, 48_000, 7.0),
        installer.GpuDevice("1", "Synthetic Supported", 24_576, 24_000, 8.0),
    )
    plan = installer._gpu_plan("all", devices)
    assert plan.generator_gpus == ("1",)
    assert plan.transcription_gpu == "1"
    with pytest.raises(RuntimeError, match="not visible"):
        installer._gpu_plan("review", devices, generator_gpus="0")


def test_gpu_topology_supports_explicit_multi_gpu_overrides() -> None:
    devices = tuple(
        installer.GpuDevice(str(index), f"Synthetic GPU {index}", 48_000, 40_000)
        for index in range(8)
    )
    plan = installer._gpu_plan(
        "all",
        devices,
        generator_gpus="3,4,5,6",
        transcription_gpu="7",
        retrieval_device="cuda",
        retrieval_gpu="2",
    )
    assert plan.environment()["RECORDBENCH_GENERATOR_TENSOR_PARALLEL"] == "4"
    assert plan.generator_gpus == ("3", "4", "5", "6")
    assert plan.transcription_gpu == "7"
    assert plan.retrieval_gpu == "2"


def test_review_model_tier_adapts_to_gpu_memory_and_sharing() -> None:
    portable_device = (
        installer.GpuDevice("0", "Synthetic 24 GiB GPU", 24_576, 24_000),
    )
    shared = installer._gpu_plan("all", portable_device)
    portable = installer._review_model_plan(
        "all", portable_device, shared, requested_profile="auto"
    )
    assert portable.profile == "portable"
    assert portable.model_id == "Qwen/Qwen3.5-4B"
    assert portable.dtype == "half"
    assert portable.gpu_utilization == 0.50
    assert portable.max_sequences == 4
    assert portable.generation_concurrency == 2
    assert portable.transcription_min_free_vram_mib == 7_000

    quality_devices = (
        installer.GpuDevice("0", "Synthetic GPU 0", 48_000, 46_000),
        installer.GpuDevice("1", "Synthetic GPU 1", 48_000, 46_000),
    )
    split = installer._gpu_plan("all", quality_devices)
    quality = installer._review_model_plan(
        "all", quality_devices, split, requested_profile="auto"
    )
    assert quality.profile == "quality"
    assert quality.model_id == "Qwen/Qwen3.5-9B"
    assert quality.dtype == "half"
    assert quality.gpu_utilization == 0.72
    assert quality.max_sequences == 8
    assert quality.generation_concurrency == 4


def test_automatic_plan_downgrades_model_before_rejecting_usable_hardware() -> None:
    devices = (
        installer.GpuDevice("0", "Synthetic 48 GiB", 48_000, 30_000, 8.9),
        installer.GpuDevice("1", "Synthetic 24 GiB", 24_576, 20_000, 8.9),
    )
    args = installer._parser().parse_args(["install", "--models", "all"])
    gpu_plan, model_plan = installer._resolve_gpu_plans(args, "all", devices)
    assert gpu_plan.layout == "split"
    assert model_plan.profile == "portable"
    assert model_plan.gpu_utilization == 0.60
    assert model_plan.max_sequences == 4


def test_explicit_gpu_plan_is_never_silently_rewritten() -> None:
    devices = (
        installer.GpuDevice("0", "Synthetic Busy", 48_000, 20_000, 8.9),
        installer.GpuDevice("1", "Synthetic Free", 24_576, 24_000, 8.9),
    )
    args = installer._parser().parse_args(
        [
            "install",
            "--models",
            "all",
            "--gpu-layout",
            "split",
            "--generator-gpus",
            "0",
            "--transcription-gpu",
            "1",
            "--review-model-profile",
            "quality",
        ]
    )
    with pytest.raises(RuntimeError, match="currently has|needs at least"):
        installer._resolve_gpu_plans(args, "all", devices)


def test_review_model_tier_rejects_an_unworkable_explicit_shape() -> None:
    small = (installer.GpuDevice("0", "Synthetic GPU", 12_000, 12_000),)
    shared = installer._gpu_plan("all", small)
    with pytest.raises(RuntimeError, match="needs at least"):
        installer._review_model_plan(
            "all", small, shared, requested_profile="portable"
        )


def test_review_model_plan_rejects_current_gpu_contention() -> None:
    busy = (
        installer.GpuDevice(
            "0", "Synthetic Busy GPU", 24_576, 8_000, 8.0
        ),
    )
    shared = installer._gpu_plan("all", busy)
    with pytest.raises(RuntimeError, match="currently has"):
        installer._review_model_plan("all", busy, shared)


def test_model_catalog_has_one_pinned_generator_per_review_tier() -> None:
    catalog = json.loads((ROOT / "config" / "models.json").read_text())
    generators = {
        item["profile"]: item
        for item in catalog["models"]
        if item["group"] == "review" and item["role"] == "generator"
    }
    assert set(generators) == {"portable", "quality"}
    for item in generators.values():
        assert len(item["revision"]) == 40
        assert item["gated"] is False


def test_transcription_model_modules_are_independently_selected() -> None:
    assert installer._model_stage_groups(
        "transcription",
        transcription_languages=("en",),
        diarization=False,
    ) == ("transcription-asr", "transcription-alignment-en")
    assert installer._model_stage_groups(
        "all",
        transcription_languages=("en", "es"),
        diarization=True,
    ) == (
        "review",
        "transcription-asr",
        "transcription-alignment-en",
        "transcription-alignment-es",
        "transcription-diarization",
    )
    with pytest.raises(RuntimeError, match="requires a transcription"):
        installer._model_stage_groups(
            "review", transcription_languages=("en",), diarization=True
        )


def test_selected_profile_cannot_accept_missing_ai_capabilities() -> None:
    healthy = {
        "capabilities": {
            "answering": "ready",
            "search": "word + meaning",
            "transcription": "local WhisperX v2",
        }
    }
    assert installer._selected_capabilities_ready(healthy, "none")
    assert installer._selected_capabilities_ready(healthy, "review")
    assert installer._selected_capabilities_ready(healthy, "transcription")
    assert installer._selected_capabilities_ready(healthy, "all")

    missing = {
        "capabilities": {
            "answering": "temporarily unavailable",
            "search": "word search only",
            "transcription": "temporarily unavailable",
        }
    }
    assert installer._selected_capabilities_ready(missing, "none")
    assert not installer._selected_capabilities_ready(missing, "review")
    assert not installer._selected_capabilities_ready(missing, "transcription")
    assert not installer._selected_capabilities_ready(missing, "all")


def test_plain_noninteractive_install_defaults_to_cpu_evaluation(tmp_path, ready_host, monkeypatch, capsys) -> None:
    node = tmp_path.resolve() / "node"
    monkeypatch.setattr(sys, "argv", [
            str(ROOT / "install"),
            "install",
            "--root",
            str(node),
            "--auth",
            "local",
            "--non-interactive",
            "--prepare-only",
            "--dry-run",
            "--no-color",
        ])
    assert installer.main() == 0
    output = capsys.readouterr().out
    assert "model payload   :: none" in output
    assert "nvidia-smi" not in output
    assert not node.exists()


def test_federated_identity_inputs_are_declarative_without_cli_secrets() -> None:
    options = {action.dest for action in installer._parser()._actions}
    assert {
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret_file",
        "oidc_allowed_groups",
        "oidc_admin_groups",
        "kerberos_realm",
        "kerberos_allowed_groups",
        "kerberos_admin_groups",
        "kerberos_keytab",
    } <= options
    assert "oidc_client_secret" not in options


def test_release_capsule_is_content_addressed_and_allowlisted(tmp_path) -> None:
    root = tmp_path / "node"
    root.mkdir(mode=0o700)
    console = installer.Console(color=False, quiet=True)
    release_id, release = installer._stage_release(console, root, dry_run=False)
    manifest = json.loads((release / "RELEASE_MANIFEST.json").read_text())
    assert manifest["release_id"] == release_id
    assert len(manifest["source_sha256"]) == 64
    assert (release / "compose.yaml").is_file()
    assert (release / "services/transcription/src").is_dir()
    for lock in ("requirements/application.lock", "requirements/build.lock",
                 "services/transcription/requirements-api.lock",
                 "deploy/model-stager/requirements.lock"):
        assert (release / lock).read_bytes() == (ROOT / lock).read_bytes()
    assert not (release / ".git").exists()
    assert not (release / ".venv").exists()
    assert not (release / "tests").exists()


@pytest.mark.parametrize("cache_path", [
    ".pytest_cache/results", "__pycache__/results", "worker.pyc", "cache.pyc/results",
    "worker.egg-info/PKG-INFO", "build/lib/worker.py",
])
def test_release_identity_matches_staged_bytes_after_tests(tmp_path, monkeypatch, cache_path):
    source = tmp_path / "source"
    component = source / "services/worker/component.py"
    component.parent.mkdir(parents=True)
    component.write_text('VALUE = "synthetic"\n')
    monkeypatch.setattr(installer, "PROJECT", source)
    monkeypatch.setattr(installer, "RELEASE_DIRECTORIES", ("services",))
    monkeypatch.setattr(installer, "RELEASE_FILES", ())
    clean_digest = installer._release_digest()
    cached = source / "services/worker" / cache_path
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text("synthetic cached test result\n")
    assert installer._release_digest() == clean_digest
    node = tmp_path / "node"
    node.mkdir(mode=0o700)
    release_id, release = installer._stage_release(
        installer.Console(color=False, quiet=True), node, dry_run=False)
    cached.write_text("different cached test result\n")
    assert installer._stage_release(
        installer.Console(color=False, quiet=True), node, dry_run=False) == (release_id, release)
    monkeypatch.setattr(installer, "PROJECT", release)
    assert installer._release_digest() == clean_digest
    assert not (release / "services/worker" / cache_path).exists()
    monkeypatch.setattr(installer, "PROJECT", source)
    component.write_text('VALUE = "changed synthetic source"\n')
    assert installer._release_digest() != clean_digest


def test_release_identity_still_rejects_included_symlink(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "services").mkdir(parents=True)
    (source / "services/component.py").symlink_to(tmp_path / "outside.py")
    monkeypatch.setattr(installer, "PROJECT", source)
    monkeypatch.setattr(installer, "RELEASE_DIRECTORIES", ("services",))
    monkeypatch.setattr(installer, "RELEASE_FILES", ())
    with pytest.raises(RuntimeError, match="symbolic link"):
        installer._release_digest()


def test_generated_environment_is_quoted_and_rejects_line_injection() -> None:
    text = installer._env_text(
        {"RECORDBENCH_PATH": "/srv/Record Bench", "RECORDBENCH_VALUE": 7},
        "synthetic",
    )
    assert 'RECORDBENCH_PATH="/srv/Record Bench"' in text
    assert 'RECORDBENCH_VALUE="7"' in text
    with pytest.raises(RuntimeError, match="environment value"):
        installer._env_text({"RECORDBENCH_PATH": "safe\nINJECT=1"}, "synthetic")


def test_generated_environment_round_trips_through_health_parser(tmp_path) -> None:
    path = tmp_path / "compose.env"
    path.write_text(
        installer._env_text(
            {
                "RECORDBENCH_HTTPS_PORT": 18443,
                "RECORDBENCH_SERVER_NAME": "recordbench.example.test",
                "RECORDBENCH_PATH": "/srv/Record Bench",
            },
            "synthetic",
        ),
        encoding="utf-8",
    )
    values = installer._dotenv(path)
    assert int(values["RECORDBENCH_HTTPS_PORT"]) == 18443
    assert values["RECORDBENCH_SERVER_NAME"] == "recordbench.example.test"
    assert values["RECORDBENCH_PATH"] == "/srv/Record Bench"


def test_existing_secret_must_remain_owner_only(tmp_path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("synthetic-secret-value-with-length\n", encoding="utf-8")
    secret.chmod(0o644)
    with pytest.raises(RuntimeError, match="unsafe"):
        installer._secret(secret)
    secret.chmod(0o600)
    assert installer._secret(secret) == "synthetic-secret-value-with-length"


def test_update_uses_versioned_images_backup_and_rollback_contracts() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    source = (ROOT / "scripts/exculpata_install.py").read_text(encoding="utf-8")
    assert "RECORDBENCH_RELEASE_ID" in compose
    assert "SNAPSHOT BEFORE MUTATION" in source
    assert "--no-backup" in source
    assert "Prior release restored and healthy" in source
    assert "docker image prune" not in source
    assert "git pull" not in source


def test_compose_validation_uses_an_isolated_temporary_node() -> None:
    source = (ROOT / "scripts/compose-check.sh").read_text(encoding="utf-8")
    assert "mktemp -d" in source
    assert "trap cleanup EXIT" in source
    assert "config --quiet" in source
    assert "compose.kerberos.yaml" in source


def test_application_health_accepts_a_useful_node_without_optional_models() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "p['status'] in {'ok','degraded'}" in dockerfile
    assert "p['storage']['status']=='ready'" in dockerfile


def test_clamav_retains_only_capabilities_needed_by_upstream_entrypoint() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    clamav = compose.split("\n  clamav:\n", 1)[1].split("\n  retrieval:\n", 1)[0]
    assert "cap_drop: [ALL]" in clamav
    assert "cap_add: [CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID]" in clamav
    assert "read_only: true" not in clamav
    assert "clamav-signatures:/var/lib/clamav" in clamav
    assert 'CLAMAV_NO_FRESHCLAMD: "true"' in clamav
    assert "clamav-updater: {condition: service_healthy}" in clamav
    assert "networks: [services]" in clamav
    updater = clamav.split("\n  clamav-updater:\n", 1)[1]
    assert "networks: [updates]" in updater
    assert 'command: ["freshclam", "--daemon", "--foreground"' in updater
    assert "-mmin -4320" in updater


def test_read_only_gateway_has_a_narrow_runtime_configuration_tmpfs() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    gateway = compose.split("\n  gateway:\n", 1)[1].split("\n  model-stager:\n", 1)[0]
    assert "read_only: true" in gateway
    assert "cap_add: [CHOWN, DAC_OVERRIDE, NET_BIND_SERVICE, SETGID, SETUID]" in gateway
    assert "/etc/nginx/conf.d:rw,noexec,nosuid,nodev,size=1m" in gateway
    assert "./deploy/gateway/default.conf.template:/etc/nginx/templates/default.conf.template:ro" in gateway


def test_provisioning_seal_is_release_specific_and_owner_only(tmp_path) -> None:
    root = tmp_path / "node"
    (root / "state").mkdir(parents=True)
    installation = {
        "release_id": "0.1.0-alpha.1-synthetic",
        "auth": "local",
        "models": "none",
    }
    assert not installer._provisioning_complete(root, installation)
    installer._seal_provisioning(root, installation)
    marker = root / "state" / "provisioned.json"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert installer._provisioning_complete(root, installation)
    assert not installer._provisioning_complete(
        root, {**installation, "release_id": "0.1.0-alpha.1-new"}
    )


def test_partial_local_resume_requires_explicit_bootstrap_identity(tmp_path, monkeypatch) -> None:
    root = tmp_path / "node"
    (root / "secrets").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "matter-storage").mkdir()
    (root / "installation.json").write_text(
        json.dumps(
            {
                "release_id": "synthetic",
                "release_path": str(ROOT),
                "storage_root": str(root / "matter-storage"),
                "auth": "local",
                "models": "none",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0))
    args = installer._parser().parse_args(
        ["install", "--root", str(root), "--resume", "--non-interactive"]
    )
    with pytest.raises(RuntimeError, match="unfinished local installation"):
        installer._provision(
            installer.Console(color=False, quiet=True),
            args,
            root,
            "local",
            "none",
            None,
            None,
        )


@pytest.fixture
def ready_host(monkeypatch, tmp_path):
    home = tmp_path / "compose-client-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    actual_uid = os.geteuid()
    original_stat = Path.stat
    original_fstat = os.fstat
    def synthetic_owner(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if result.st_uid == actual_uid and (actual_uid != 0 or path == tmp_path or tmp_path in path.parents):
            fields = list(result)
            fields[4] = 1000
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(Path, "stat", synthetic_owner)
    # Python 3.14's lstat calls os.lstat directly; keep the same no-follow
    # identity simulation on every supported Python version.
    monkeypatch.setattr(Path, "lstat", lambda path: synthetic_owner(path, follow_symlinks=False))
    def synthetic_descriptor_owner(descriptor):
        result = original_fstat(descriptor)
        selected = actual_uid != 0
        if actual_uid == 0:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            selected = descriptor_path == tmp_path or tmp_path in descriptor_path.parents
        if result.st_uid == actual_uid and selected:
            fields = list(result)
            fields[4] = 1000
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(os, "fstat", synthetic_descriptor_owner)
    import pwd
    from types import SimpleNamespace
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir="/home/synthetic-service"))
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(installer.os, "getegid", lambda: 1000)
    monkeypatch.setattr(installer.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 400 * 1024**3})())
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0, '{"nvidia": {}}' if "{{json .Runtimes}}" in command else "1.0", ""))


def preflight_args(tmp_path, *extra):
    return installer._parser().parse_args(["preflight", "--root", str(tmp_path.resolve() / "new node with spaces"), *extra])


def checks_by_name(result):
    return {check.name: check for check in result.checks}


def test_preflight_cpu_has_optional_gpu_and_never_writes(tmp_path, ready_host, monkeypatch):
    args = preflight_args(tmp_path)
    monkeypatch.setattr(installer.Path, "mkdir", lambda *a, **kw: pytest.fail("preflight attempted a write"))
    result = installer._collect_preflight("none", args)
    assert result.ready
    assert checks_by_name(result)["gpu"].blocking is False
    assert not args.root.exists()
    assert result.payload()["schema_version"] == 1
    assert set(result.payload()["checks"][0]) == {"name", "state", "observed", "required_capability", "blocking", "remedy"}


@pytest.mark.parametrize("system,machine", [("Darwin", "arm64"), ("Linux", "aarch64"), ("Windows", "AMD64")])
def test_preflight_unsupported_host_stops_before_other_probes(tmp_path, ready_host, monkeypatch, system, machine):
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    monkeypatch.setattr(installer, "_probe", lambda command: pytest.fail("unsupported host reached runtime"))
    result = installer._collect_preflight("none", preflight_args(tmp_path))
    assert not result.ready
    assert [check.name for check in result.checks] == ["host"]


@pytest.mark.parametrize("missing", ["docker", "openssl"])
def test_preflight_missing_tools_are_actionable(tmp_path, ready_host, monkeypatch, missing):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None if name == missing else "/usr/bin/" + name)
    result = installer._collect_preflight("none", preflight_args(tmp_path))
    check = checks_by_name(result)[missing]
    assert not result.ready and check.blocking and check.remedy


@pytest.mark.parametrize("failed", ["docker-access", "compose"])
def test_preflight_runtime_failure_never_echoes_output(tmp_path, ready_host, monkeypatch, failed):
    def probe(command):
        failure = ("compose" in command) == (failed == "compose")
        return subprocess.CompletedProcess(command, int(failure), "synthetic-private-output", "synthetic-private-output")
    monkeypatch.setattr(installer, "_probe", probe)
    result = installer._collect_preflight("none", preflight_args(tmp_path))
    assert checks_by_name(result)[failed].state == "fail"
    assert "synthetic-private-output" not in json.dumps(result.payload())


@pytest.mark.parametrize("uid,gid", [(0, 1000), (1000, 0)])
def test_preflight_root_identity_blocks_even_dry_run(tmp_path, ready_host, monkeypatch, uid, gid):
    monkeypatch.setattr(installer.os, "geteuid", lambda: uid)
    monkeypatch.setattr(installer.os, "getegid", lambda: gid)
    args = preflight_args(tmp_path, "--dry-run")
    assert checks_by_name(installer._collect_preflight("none", args))["ownership"].state == "fail"


def test_preflight_capacity_and_write_access_are_separate(tmp_path, ready_host, monkeypatch):
    monkeypatch.setattr(installer.os, "access", lambda *a: False)
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 99 * 1024**3})())
    checks = checks_by_name(installer._collect_preflight("none", preflight_args(tmp_path)))
    assert checks["node-storage"].state == "fail"
    assert checks["node-storage-reserve"].state == "fail"
    assert checks["capacity-target"].blocking is False


def test_preflight_symlink_and_non_directory_storage_block(tmp_path, ready_host):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    args = preflight_args(tmp_path, "--storage-root", str(link / "new"))
    assert checks_by_name(installer._collect_preflight("none", args))["matter-storage"].state == "fail"
    target.rmdir()
    target.write_text("synthetic")
    args.storage_root = target
    assert checks_by_name(installer._collect_preflight("none", args))["matter-storage"].state == "fail"


@pytest.mark.parametrize("options", [["--bind-address", "0.0.0.0"], ["--tls-cert", "/missing/cert"], ["--tls-cert", "/missing/cert", "--tls-key", "/missing/key"]])
def test_preflight_tls_missing_prerequisites_block(tmp_path, ready_host, options):
    result = installer._collect_preflight("none", preflight_args(tmp_path, *options))
    assert checks_by_name(result)["tls"].state == "fail"


def test_preflight_selected_gpu_profile_checks_real_plan(tmp_path, ready_host, monkeypatch):
    def probe(command):
        output = '{"nvidia": {}}' if "{{json .Runtimes}}" in command else "0, Synthetic GPU, 24576, 24000, 8.0" if command[0] == "nvidia-smi" else "1.0"
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(installer, "_probe", probe)
    args = preflight_args(tmp_path, "--models", "review")
    assert installer._collect_preflight("review", args).ready
    args.generator_gpus = "9"
    assert checks_by_name(installer._collect_preflight("review", args))["gpu"].state == "fail"
    monkeypatch.setattr(installer.shutil, "which", lambda name: None if name == "nvidia-smi" else name)
    assert not installer._collect_preflight("review", args).ready
    assert installer._collect_preflight("none", args).ready


def test_preflight_json_is_single_document_and_returns_failure(tmp_path, ready_host, monkeypatch, capsys):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(tmp_path / "new"), "--json"])
    assert installer.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert not (tmp_path / "new").exists()


def test_install_blocking_preflight_does_not_create_state(tmp_path, ready_host, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    node = tmp_path / "new"
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--non-interactive"])
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.parametrize("output,returncode", [('{}', 0), ('[]', 0), ('not-json', 0), ('{"nvidia": {}}', 1)])
def test_preflight_gpu_runtime_requires_successful_registered_engine(tmp_path, ready_host, monkeypatch, output, returncode):
    def probe(command):
        if "{{json .Runtimes}}" in command:
            return subprocess.CompletedProcess(command, returncode, output, "")
        return subprocess.CompletedProcess(command, 0, "0, Synthetic GPU, 24576, 24000, 8.0", "")
    monkeypatch.setattr(installer, "_probe", probe)
    result = installer._collect_preflight("review", preflight_args(tmp_path))
    assert checks_by_name(result)["gpu"].state == "pass"
    assert checks_by_name(result)["gpu-runtime"].state == "fail"


def test_preflight_runtime_timeout_is_actionable(tmp_path, ready_host, monkeypatch):
    def probe(command):
        raise subprocess.TimeoutExpired(command, 20)
    monkeypatch.setattr(installer, "_probe", probe)
    result = installer._collect_preflight("review", preflight_args(tmp_path))
    assert not result.ready
    assert checks_by_name(result)["docker-access"].remedy
    assert checks_by_name(result)["gpu"].remedy


def test_preflight_existing_directory_requires_current_owner(tmp_path, ready_host, monkeypatch):
    args = preflight_args(tmp_path)
    args.root.mkdir()
    monkeypatch.setattr(installer.os, "geteuid", lambda: args.root.stat().st_uid + 1)
    assert checks_by_name(installer._collect_preflight("none", args))["node-storage"].state == "fail"


def test_preflight_supplied_lan_tls_pair_is_read_only(tmp_path, ready_host):
    cert, key = tmp_path / "synthetic.crt", tmp_path / "synthetic.key"
    cert.write_text("synthetic certificate placeholder")
    key.write_text("synthetic key placeholder")
    args = preflight_args(tmp_path, "--bind-address", "0.0.0.0", "--tls-cert", str(cert), "--tls-key", str(key))
    check = checks_by_name(installer._collect_preflight("none", args))["tls"]
    assert check.state == "pass"
    assert "trust must be verified" in check.observed
    assert not args.root.exists()


@pytest.mark.parametrize("options", [
    ["--models", "none", "--enable-diarization"],
    ["--models", "review", "--enable-diarization"],
    ["--models", "transcription", "--transcription-languages", "fr"],
    ["--models", "all", "--transcription-languages", ""],
])
def test_preflight_rejects_invalid_model_option_combinations(tmp_path, ready_host, options):
    args = preflight_args(tmp_path, *options)
    result = installer._collect_preflight(args.models, args)
    assert not result.ready
    assert checks_by_name(result)["model-options"].state == "fail"
    assert not args.root.exists()


def test_preflight_nonempty_root_requires_explicit_resume(tmp_path, ready_host, monkeypatch):
    args = preflight_args(tmp_path)
    args.root.mkdir(mode=0o700)
    (args.root / "existing.txt").write_text("synthetic existing data")
    original_stat = installer.Path.stat
    def synthetic_owned(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == args_root:
            fields = list(result)
            fields[4] = 1000
            return installer.os.stat_result(fields)
        return result
    args_root = args.root
    monkeypatch.setattr(installer.Path, "stat", synthetic_owned)
    blocked = installer._collect_preflight("none", args)
    assert not blocked.ready
    assert checks_by_name(blocked)["node-empty"].state == "fail"
    args.resume = True
    assert not installer._collect_preflight("none", args).ready
    (args.root / "installation.json").write_text(json.dumps({
        "release_path": str(ROOT), "auth": "local", "models": "none", "profiles": ["tools"],
    }))
    # Configuration is persisted only after its private directories exist.
    (args.root / "secrets").mkdir(mode=0o700)
    # This synthetic record precedes local account initialization, so resuming
    # also supplies the explicit bootstrap identity required by first-run checks.
    args.admin_username = "synthetic.admin"
    args.admin_display_name = "Synthetic Administrator"
    assert installer._collect_preflight("none", args).ready
    assert (args.root / "existing.txt").read_text() == "synthetic existing data"


def test_invalid_model_options_stop_install_before_state_creation(tmp_path, ready_host, monkeypatch):
    node = tmp_path.resolve() / "uncreated node"
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "none",
                                     "--enable-diarization", "--non-interactive"])
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.parametrize("options", [
    ["--generator-gpu-utilization", "1"],
    ["--retrieval-device", "cpu", "--retrieval-gpu", "0"],
])
def test_cpu_preflight_validates_gpu_options_without_gpu_probe(tmp_path, ready_host, monkeypatch, options):
    args = preflight_args(tmp_path, "--models", "none", *options)
    result = installer._collect_preflight("none", args)
    assert not result.ready
    assert checks_by_name(result)["gpu-options"].state == "fail"
    assert not args.root.exists()


@pytest.mark.parametrize("field", ["root", "storage_root"])
def test_preflight_rejects_home_directory_dot_dot_alias(tmp_path, ready_host, monkeypatch, field):
    synthetic_home = tmp_path.resolve() / "synthetic-home"
    synthetic_home.mkdir()
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: synthetic_home))
    args = preflight_args(tmp_path, "--resume")
    setattr(args, field, synthetic_home / "missing" / "..")
    result = installer._collect_preflight("none", args)
    assert not result.ready
    check = "node-storage" if field == "root" else "matter-storage"
    assert checks_by_name(result)[check].state == "fail"
    assert list(synthetic_home.iterdir()) == []



def test_preflight_dot_dot_cannot_hide_a_symlink_component(tmp_path, ready_host):
    target = tmp_path.resolve() / "separate-target"
    target.mkdir()
    (tmp_path / "linked").symlink_to(target)
    args = preflight_args(tmp_path)
    args.root = tmp_path.resolve() / "missing" / ".." / "linked" / "node"
    result = installer._collect_preflight("none", args)
    assert not result.ready
    assert checks_by_name(result)["node-storage"].state == "fail"
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("storage", [False, True])
def test_preflight_refuses_effective_service_home_when_home_is_inherited(tmp_path, ready_host, monkeypatch, storage):
    import pwd
    from types import SimpleNamespace
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "inherited-operator-home"))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(service_home)))
    args = preflight_args(tmp_path, "--resume")
    if storage:
        args.storage_root = service_home
    else:
        args.root = service_home
    result = installer._collect_preflight("none", args)
    assert not result.ready
    assert checks_by_name(result)["matter-storage" if storage else "node-storage"].state == "fail"


@pytest.mark.parametrize("storage", [False, True])
@pytest.mark.parametrize("kind", ["shared", "group-writable", "nonowned", "unsafe-grandparent"])
def test_preflight_refuses_replaceable_creation_ancestors(tmp_path, ready_host, monkeypatch, storage, kind):
    parent = tmp_path / "synthetic-parent"
    parent.mkdir(mode=0o700)
    if kind == "shared":
        parent.chmod(0o777)
    elif kind == "group-writable":
        parent.chmod(0o770)
    elif kind == "unsafe-grandparent":
        parent.chmod(0o777)
        parent = parent / "private-child"
        parent.mkdir(mode=0o700)
    else:
        original = Path.stat
        def foreign_owner(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == parent:
                fields = list(result)
                fields[4] = 2000
                return os.stat_result(fields)
            return result
        monkeypatch.setattr(Path, "stat", foreign_owner)
    args = preflight_args(tmp_path)
    target = parent / "new-private-node"
    if storage:
        args.storage_root = target
    else:
        args.root = target
    result = installer._collect_preflight("none", args)
    assert checks_by_name(result)["matter-storage" if storage else "node-storage"].state == "fail"
    assert not result.ready and not target.exists()


def test_storage_chain_accepts_protected_root_parent_and_sticky_system_parent(tmp_path, ready_host, monkeypatch):
    system = tmp_path / "synthetic-system"
    system.mkdir(mode=0o755)
    creation = system / "service-owned"
    creation.mkdir(mode=0o700)
    original = Path.stat
    def root_owner(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == system:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(Path, "stat", root_owner)
    assert installer._storage_ancestors_safe(creation)
    system.chmod(0o1777)
    assert installer._storage_ancestors_safe(creation)
    assert not installer._storage_ancestors_safe(system)
    system.chmod(0o777)
    assert not installer._storage_ancestors_safe(creation)


@pytest.mark.parametrize("command", ["preflight", "install"])
def test_invalid_server_name_blocks_before_state_creation(tmp_path, ready_host, monkeypatch, command):
    node = tmp_path.resolve() / "absent-node"
    monkeypatch.setattr(sys, "argv", ["install", command, "--root", str(node),
                                     "--models", "none", "--server-name", "bad_name",
                                     "--non-interactive", "--json"] if command == "preflight" else
                                    ["install", command, "--root", str(node),
                                     "--models", "none", "--server-name", "bad_name",
                                     "--non-interactive"])
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **kw: pytest.fail("invalid hostname reached release staging"))
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="owner-masking umask requires a real non-root permission check")
@pytest.mark.parametrize("mask", [0o000, 0o700, 0o777])
def test_prepare_protects_all_missing_storage_components_under_any_umask(tmp_path, mask):
    import stat
    base = tmp_path.resolve()
    node = base / "new-node-parent" / "nested" / "node"
    storage = base / "new-storage-parent" / "nested" / "matters"
    old_umask = os.umask(mask)
    try:
        paths = installer._prepare_directories(installer.Console(color=False, quiet=True), node,
                    storage_root=storage, resume=False, dry_run=False)
    finally:
        retained_umask = os.umask(old_umask)
    assert retained_umask == mask
    for leaf in (node, *paths.values()):
        for directory in (leaf, *leaf.parents):
            if directory == base:
                break
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("name", ["recordbench.example.test", "recordbench-2", "LOCALHOST"])
def test_preflight_accepts_configurable_server_name(tmp_path, ready_host, name):
    result = installer._collect_preflight("none", preflight_args(tmp_path, "--server-name", name))
    assert result.ready
    assert checks_by_name(result)["server-name"].state == "pass"
    assert name not in json.dumps(result.payload())


@pytest.mark.parametrize("kind", ["symlink", "writable", "foreign-owner"])
def test_prepare_revalidates_directory_created_during_the_walk(tmp_path, monkeypatch, kind):
    import stat
    base = tmp_path.resolve()
    target = base / "raced" / "node"
    outside = base / "outside"
    outside.mkdir(mode=0o755)
    outside.chmod(0o755)  # Test the declared mode independently of the caller umask.
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("synthetic preserved content")
    original_mkdir, original_fstat = os.mkdir, os.fstat
    replaced_inode = None

    def concurrent_creation(path, *, dir_fd):
        nonlocal replaced_inode
        if path != "raced":
            return original_mkdir(path, 0o700, dir_fd=dir_fd)
        if kind == "symlink":
            os.symlink(outside, path, dir_fd=dir_fd)
        else:
            original_mkdir(path, 0o700, dir_fd=dir_fd)
            if kind == "writable":
                (base / path).chmod(0o777)
            replaced_inode = (base / path).stat().st_ino
        raise FileExistsError("synthetic concurrent creation")

    def foreign_owner(descriptor):
        result = original_fstat(descriptor)
        if kind == "foreign-owner" and result.st_ino == replaced_inode:
            fields = list(result)
            fields[4] = os.geteuid() + 1000
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(installer, "_mkdir_private", concurrent_creation)
    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(RuntimeError, match="storage"):
        installer._create_private_directory(target)
    assert not (outside / "node").exists()
    assert sentinel.read_text() == "synthetic preserved content"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    if kind != "symlink":
        assert not target.exists()


@pytest.mark.parametrize("storage", [False, True])
def test_prepare_refuses_symlink_in_selected_storage_path(tmp_path, storage):
    base = tmp_path.resolve()
    outside = base / "outside"
    outside.mkdir()
    linked = base / "linked"
    linked.symlink_to(outside)
    node = base / "node" if storage else linked / "node"
    with pytest.raises(RuntimeError, match="symbolic link"):
        installer._prepare_directories(installer.Console(color=False, quiet=True), node,
            storage_root=linked / "matters" if storage else None, resume=False, dry_run=False)
    assert list(outside.iterdir()) == []
    assert not (base / "node").exists()


def test_prepare_revalidates_existing_parent_without_changing_it(tmp_path):
    import stat
    parent = tmp_path.resolve() / "parent"
    parent.mkdir(mode=0o755)
    node = parent / "node"
    parent.chmod(0o777)
    with pytest.raises(RuntimeError, match="replaceable"):
        installer._prepare_directories(installer.Console(color=False, quiet=True), node,
            storage_root=None, resume=False, dry_run=False)
    assert not node.exists()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o777
    parent.chmod(0o755)
    installer._prepare_directories(installer.Console(color=False, quiet=True), node,
        storage_root=None, resume=False, dry_run=False)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(node.stat().st_mode) == 0o700


def test_prepare_resume_does_not_follow_replaced_internal_directory(tmp_path):
    base = tmp_path.resolve()
    node = base / "node"
    node.mkdir(mode=0o700)
    outside = base / "outside"
    outside.mkdir()
    (node / "config").symlink_to(outside)
    with pytest.raises(RuntimeError, match="safely"):
        installer._prepare_directories(installer.Console(color=False, quiet=True), node,
            storage_root=None, resume=True, dry_run=False)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("blocker", ["capacity", "creation-parent"])
def test_preflight_json_reports_real_host_blockers_without_creating_state(tmp_path, ready_host, monkeypatch, capsys, blocker):
    parent = tmp_path.resolve() / "creation-parent"
    parent.mkdir(mode=0o700)
    root = parent / "uncreated node"
    if blocker == "capacity":
        monkeypatch.setattr(installer.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 99 * 1024**3})())
        blocked_check = "node-storage-reserve"
    else:
        parent.chmod(0o1777)
        blocked_check = "node-storage"
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--models", "none", "--json"])
    assert installer.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["schema_version"] == 1
    check = next(row for row in payload["checks"] if row["name"] == blocked_check)
    assert check["blocking"] is True and check["state"] == "fail" and check["remedy"]
    assert all(row["remedy"] for row in payload["checks"] if row["blocking"] and row["state"] != "pass")
    assert not root.exists()
    assert list(parent.iterdir()) == []


def test_interactive_invalid_hostname_blocks_before_storage_and_release(tmp_path, ready_host, monkeypatch):
    node = tmp_path.resolve() / "uncreated node"
    prompts = []

    def ask(prompt, default, *, non_interactive):
        prompts.append(prompt)
        return "bad_name" if prompt == "RecordBench hostname" else default

    monkeypatch.setattr(installer, "_ask", ask)
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **kw: pytest.fail("invalid interactive hostname reached storage creation"))
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **kw: pytest.fail("invalid interactive hostname reached release staging"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "none", "--auth", "local"])
    assert installer.main() == 1
    assert prompts.count("RecordBench hostname") == 1
    assert not node.exists()


@pytest.mark.parametrize("storage", [False, True])
def test_preflight_rejects_execute_only_ancestor_before_preparing_storage(tmp_path, ready_host, monkeypatch, storage):
    base = tmp_path.resolve()
    protected = base / "execute-only-parent"
    protected.mkdir(mode=0o711)
    creation = protected / "service-owned"
    creation.mkdir(mode=0o700)
    original_stat, original_access = Path.stat, os.access

    def synthetic_root_owner(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path == protected:
            fields = list(metadata)
            fields[4] = 0
            return os.stat_result(fields)
        return metadata

    def service_access(path, mode, *args, **kwargs):
        if Path(path) == protected:
            return not bool(mode & (os.R_OK | os.W_OK))
        return original_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", synthetic_root_owner)
    monkeypatch.setattr(os, "access", service_access)
    args = preflight_args(tmp_path)
    selected = creation / "uncreated"
    if storage:
        args.storage_root = selected
    else:
        args.root = selected
    result = installer._collect_preflight("none", args)
    assert not result.ready
    check = checks_by_name(result)["matter-storage" if storage else "node-storage"]
    assert check.state == "fail" and check.remedy
    assert not selected.exists()
    assert list(creation.iterdir()) == []
    monkeypatch.setattr(os, "access", original_access)
    assert installer._collect_preflight("none", args).ready
    assert not selected.exists()


def test_interactive_hostname_is_collected_once_and_used_by_configuration(tmp_path, ready_host, monkeypatch):
    node = tmp_path.resolve() / "uncreated node"
    prompts = []
    original_configure = installer._configure

    def ask(prompt, default, *, non_interactive):
        prompts.append(prompt)
        return "synthetic-node.example.test" if prompt == "RecordBench hostname" else default

    def configure(console, args, *positional, **kwargs):
        assert args.server_name == "synthetic-node.example.test"
        return original_configure(console, args, *positional, **kwargs)

    monkeypatch.setattr(installer, "_ask", ask)
    monkeypatch.setattr(installer, "_configure", configure)
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "none", "--auth", "local", "--dry-run"])
    assert installer.main() == 0
    assert prompts.count("RecordBench hostname") == 1
    assert not node.exists()


@pytest.mark.parametrize("prompt,value", [
    ("Initial administrator username", "bad/name"),
    ("Administrator display name", "bad\nname"),
    ("Administrator display name", "   "),
    ("Administrator display name", "x" * 161),
])
def test_interactive_identity_is_validated_before_any_writes(tmp_path, ready_host, monkeypatch, prompt, value):
    node = tmp_path.resolve() / "uncreated-node"
    monkeypatch.setattr(installer, "_ask", lambda label, default, **k: value if label == prompt else default)
    monkeypatch.setattr(installer, "_choose", lambda *a, **k: "local")
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("interactive identity reached writes"))
    monkeypatch.setattr(installer.getpass, "getpass", lambda *a: pytest.fail("identity preflight read a secret"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "none"])
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.parametrize("auth,prompt", [("oidc", "OIDC client ID"), ("kerberos", "Kerberos realm")])
def test_interactive_provider_text_is_checked_before_writes(tmp_path, ready_host, monkeypatch, auth, prompt):
    node = tmp_path.resolve() / "uncreated-node"
    monkeypatch.setattr(installer, "_ask", lambda label, default, **k: "bad\nvalue" if label == prompt else default)
    monkeypatch.setattr(installer, "_choose", lambda *a, **k: auth)
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("invalid provider text reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "none", "--dry-run"])
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.parametrize("missing", ["host", "keytab"])
def test_interactive_kerberos_checks_actual_choice_before_any_writes(tmp_path, ready_host, monkeypatch, missing):
    node = tmp_path.resolve() / "uncreated-node"
    keytab = tmp_path / "synthetic-keytab"
    if missing == "host":
        keytab.write_text("synthetic placeholder")
    prompts = []
    def ask(label, default, **kwargs):
        prompts.append(label)
        return str(keytab) if label == "Path to the exported HTTP service keytab" else default
    original_is_dir, original_is_file = Path.is_dir, Path.is_file
    monkeypatch.setattr(Path, "is_dir", lambda path: missing != "host" if path == Path("/var/lib/sss/pipes") else original_is_dir(path))
    monkeypatch.setattr(Path, "is_file", lambda path: True if path == Path("/etc/krb5.conf") else original_is_file(path))
    monkeypatch.setattr(installer, "_ask", ask)
    monkeypatch.setattr(installer, "_choose", lambda *a, **k: "kerberos")
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("interactive Kerberos choice reached writes"))
    monkeypatch.setattr(installer.getpass, "getpass", lambda *a: pytest.fail("identity preflight read a secret"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "none"])
    assert installer.main() == 1
    assert prompts.count("Path to the exported HTTP service keytab") == 1
    assert not node.exists()


@pytest.mark.parametrize("auth", ["local", "oidc", "kerberos"])
def test_configuration_reuses_all_preflight_identity_answers(tmp_path, ready_host, monkeypatch, auth):
    node = tmp_path.resolve() / "uncreated-node"
    prompts = []
    def ask(label, default, **kwargs):
        prompts.append(label)
        return default
    original_configure = installer._configure
    def configure(console, args, *positional, **kwargs):
        assert args.auth == auth
        monkeypatch.setattr(installer, "_ask", lambda *a, **k: pytest.fail("configuration asked an unchecked identity question"))
        monkeypatch.setattr(installer, "_choose", lambda *a, **k: pytest.fail("configuration changed the checked identity mode"))
        return original_configure(console, args, *positional, **kwargs)
    monkeypatch.setattr(installer, "_ask", ask)
    monkeypatch.setattr(installer, "_choose", lambda *a, **k: auth)
    monkeypatch.setattr(installer, "_configure", configure)
    monkeypatch.setattr(installer.getpass, "getpass", lambda *a: pytest.fail("dry-run read a secret"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "none", "--dry-run"])
    assert installer.main() == 0
    assert len(prompts) == len(set(prompts))
    assert not node.exists()


@pytest.mark.parametrize("models", ["transcription", "all"])
@pytest.mark.parametrize("flags,missing", [
    ([], {"model-terms", "model-token-input"}),
    (["--accept-model-terms"], {"model-token-input"}),
    (["--hf-token-stdin"], {"model-terms"}),
])
def test_unattended_diarization_blocks_missing_staging_options_before_writes(
    tmp_path, ready_host, monkeypatch, models, flags, missing,
):
    node = tmp_path.resolve() / "uncreated node"
    def probe(command):
        output = ("0, Synthetic GPU, 49152, 47000, 8.9" if command[0] == "nvidia-smi"
                  else '{"nvidia": {}}' if "{{json .Runtimes}}" in command else "1.0")
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(installer, "_probe", probe)
    monkeypatch.setattr(installer, "_hf_token", lambda *a: pytest.fail("preflight read a token"))
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("missing staging choices reached writes"))
    args = installer._parser().parse_args(["install", "--root", str(node), "--models", models,
                                          "--enable-diarization", "--diarization-backend", "community-1", "--non-interactive", "--password-stdin", *flags])
    result = installer._collect_preflight(models, args)
    assert {row.name for row in result.checks if row.blocking and row.state == "fail"} == missing
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", models,
                                     "--enable-diarization", "--diarization-backend", "community-1", "--non-interactive", "--password-stdin", *flags])
    assert installer.main() == 1
    assert not node.exists()


def test_diarization_preflight_checks_flags_without_consuming_standard_input(tmp_path, ready_host, monkeypatch):
    class UnreadInput:
        def readline(self, *args):
            pytest.fail("preflight must not consume the staging token")
    monkeypatch.setattr(sys, "stdin", UnreadInput())
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0,
        "0, Synthetic GPU, 49152, 47000, 8.9" if command[0] == "nvidia-smi" else '{"nvidia": {}}', ""))
    args = preflight_args(tmp_path, "--models", "transcription", "--enable-diarization", "--diarization-backend", "community-1", "--non-interactive",
                          "--accept-model-terms", "--hf-token-stdin", "--password-stdin")
    result = installer._collect_preflight("transcription", args)
    assert result.ready
    assert checks_by_name(result)["model-token-input"].state == "pass"


def saved_gpu_node(tmp_path, *, model_profile="quality", models="review"):
    from tests.test_first_run_handoff import configured_node
    root, _, paths = configured_node(tmp_path.resolve())
    from case_intelligence.local_accounts import LocalAccountRepository
    LocalAccountRepository(paths["accounts"] / "local-accounts.json").initialize(
        "alice.admin", "Alice Administrator", "synthetic-saved-node-password", actor="synthetic-operator")
    installation = json.loads((root / "installation.json").read_text())
    installation.update({
        "release_id": "synthetic", "release_path": str(ROOT), "auth": "local", "models": models,
        "profiles": ["ai"] if models == "review" else ["transcription"],
        "review_model_profile": model_profile, "gpu_layout": "shared",
        "diarization_backend": "community-1",
        "gpu_topology": {"generator": ["0"], "transcription": "0", "retrieval_device": "cpu", "retrieval_gpu": None},
    })
    (root / "installation.json").write_text(json.dumps(installation))
    environment = installer._dotenv(root / "compose.env")
    environment.update({"RECORDBENCH_GENERATOR_GPU": "0", "RECORDBENCH_TRANSCRIPTION_GPU": "0",
                   "RECORDBENCH_RETRIEVAL_DEVICE": "cpu", "RECORDBENCH_RETRIEVAL_GPU": "0",
                   "RECORDBENCH_GPU_LAYOUT": "shared", "RECORDBENCH_GENERATOR_GPU_UTILIZATION": "0.72"})
    environment.update({key.replace("RECORDBENCH_", "EXCULPATA_", 1): value
                        for key, value in environment.items() if key.startswith("RECORDBENCH_")})
    (root / "compose.env").write_text(installer._env_text(environment, "synthetic saved GPU plan"))
    (root / "config" / "transcription.env").write_text('TRANSCRIPTION_V2_MIN_FREE_VRAM_MB="16000"\n')
    return root, installation, environment


@pytest.mark.parametrize("auth", [[], ["--auth", "local"]])
@pytest.mark.parametrize("resume", [[], ["--resume"]])
def test_fresh_local_unattended_password_choice_blocks_before_writes(
    tmp_path, ready_host, monkeypatch, capsys, auth, resume,
):
    node = tmp_path.resolve() / "uncreated-node"
    flags = ["--root", str(node), "--models", "none", "--non-interactive", *auth, *resume]
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("password choice reached writes"))
    monkeypatch.setattr(installer, "_password", lambda *a: pytest.fail("preflight read a password"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--json", *flags])
    assert installer.main() == 1
    payload = json.loads(capsys.readouterr().out)
    failed = [row for row in payload["checks"] if row["blocking"] and row["state"] == "fail"]
    assert [row["name"] for row in failed] == ["admin-password-input"]
    assert "--password-stdin" in failed[0]["remedy"]
    monkeypatch.setattr(sys, "argv", ["install", *flags])
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.parametrize("options", [["--password-stdin"], ["--dry-run"], ["--auth", "oidc", "--dry-run"], ["--auth", "kerberos", "--dry-run"]])
def test_unattended_password_preflight_accepts_available_input_without_reading(
    tmp_path, ready_host, monkeypatch, options,
):
    class UnreadInput:
        def readline(self, *args):
            pytest.fail("preflight must not consume the password")
    monkeypatch.setattr(sys, "stdin", UnreadInput())
    args = preflight_args(tmp_path, "--non-interactive", *options)
    assert installer._collect_preflight("none", args).ready
    assert not args.root.exists()


@pytest.mark.parametrize("flags,check", [
    (["--admin-username", "bad/name"], "admin-identity"),
    (["--admin-username", "ab"], "admin-identity"),
    (["--admin-display-name", "   "], "admin-identity"),
    (["--admin-display-name", "synthetic\nname"], "admin-identity"),
    (["--admin-display-name", "x" * 161], "admin-identity"),
    (["--auth", "oidc"], "oidc-secret-input"),
    (["--auth", "oidc", "--oidc-client-secret-file", "/missing/synthetic-secret"], "oidc-secret-input"),
    (["--auth", "kerberos"], "kerberos-keytab-input"),
])
def test_invalid_identity_inputs_block_fresh_install_before_writes(
    tmp_path, ready_host, monkeypatch, flags, check,
):
    args = preflight_args(tmp_path, "--non-interactive", "--password-stdin", *flags)
    assert checks_by_name(installer._collect_preflight("none", args))[check].state == "fail"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("invalid identity reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--models", "none",
                                     "--non-interactive", "--password-stdin", *flags])
    assert installer.main() == 1
    assert not args.root.exists()


@pytest.mark.parametrize("kind", ["regular", "empty", "large", "directory", "symlink"])
def test_oidc_credential_preflight_rejects_unsafe_sources_and_redacts_results(tmp_path, ready_host, monkeypatch, kind):
    source = tmp_path / "synthetic-secret"
    if kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        target = tmp_path / "synthetic-target"
        target.write_text("synthetic placeholder")
        source.symlink_to(target)
    else:
        source.write_text("" if kind == "empty" else "x" * (1024 * 1024 + 1) if kind == "large" else "synthetic placeholder")
    args = preflight_args(tmp_path, "--auth", "oidc", "--non-interactive", "--oidc-client-secret-file", str(source))
    result = installer._collect_preflight("none", args)
    assert checks_by_name(result)["oidc-secret-input"].state == ("pass" if kind == "regular" else "fail")
    assert result.ready is (kind == "regular")
    assert str(source) not in json.dumps(result.payload())
    assert not args.root.exists()


def test_local_identity_preflight_accepts_account_admin_normalization(tmp_path, ready_host):
    args = preflight_args(tmp_path, "--non-interactive", "--password-stdin", "--admin-username", " Synthetic.ADMIN ",
                          "--admin-display-name", " Synthetic Administrator ")
    assert installer._collect_preflight("none", args).ready


@pytest.mark.parametrize("dtype,compute", [("half", 7.5), ("half", 8.9), ("bfloat16", 8.0)])
def test_supported_saved_generator_precision_is_preserved(tmp_path, dtype, compute):
    root, installation, environment = saved_gpu_node(tmp_path)
    environment["EXCULPATA_GENERATOR_DTYPE"] = environment["RECORDBENCH_GENERATOR_DTYPE"] = dtype
    args = installer._parser().parse_args(["install", "--root", str(root)])
    installer._restore_model_options(args, installation, environment, {})
    _, plan = installer._resolve_gpu_plans(args, "review", (installer.GpuDevice("0", "Synthetic GPU", 49152, 47000, compute),))
    assert plan.dtype == dtype


def test_fresh_diarization_dry_run_never_consumes_credentials(tmp_path, ready_host, monkeypatch):
    node = tmp_path.resolve() / "uncreated-node"
    monkeypatch.setattr(installer, "_probe", lambda cmd: subprocess.CompletedProcess(cmd, 0,
        "0, Synthetic GPU, 49152, 47000, 8.9" if cmd[0] == "nvidia-smi" else '{"nvidia": {}}', ""))
    monkeypatch.setattr(installer, "_hf_token", lambda *a: pytest.fail("dry-run consumed a token"))
    monkeypatch.setattr(installer, "_password", lambda *a: pytest.fail("dry-run consumed a password"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--models", "transcription", "--enable-diarization",
                                     "--non-interactive", "--accept-model-terms", "--hf-token-stdin", "--dry-run"])
    assert installer.main() == 0
    assert not node.exists()


def test_relative_node_root_has_same_blocking_result_in_preflight_and_install(
    tmp_path, ready_host, monkeypatch, capsys,
):
    monkeypatch.chdir(tmp_path.resolve())
    flags = ["--root", "relative-node", "--models", "none", "--non-interactive", "--dry-run"]
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("relative root reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--json", *flags])
    assert installer.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert next(row for row in payload["checks"] if row["name"] == "node-storage")["state"] == "fail"
    monkeypatch.setattr(sys, "argv", ["install", *flags])
    assert installer.main() == 1
    assert not (tmp_path / "relative-node").exists()


@pytest.mark.parametrize("command", ["resume", "update"])
@pytest.mark.parametrize("dtype", ["bfloat16", None])
def test_saved_bfloat16_blocks_older_replacement_gpu_before_commands(
    tmp_path, request, monkeypatch, command, dtype,
):
    root, installation, environment = saved_gpu_node(tmp_path)
    request.getfixturevalue("ready_host")
    environment.pop("EXCULPATA_GENERATOR_DTYPE", None)
    environment.pop("RECORDBENCH_GENERATOR_DTYPE", None)
    if dtype is not None:
        environment["EXCULPATA_GENERATOR_DTYPE"] = environment["RECORDBENCH_GENERATOR_DTYPE"] = dtype
    (root / "compose.env").write_text(installer._env_text(environment, "synthetic saved precision"))
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(installer, "_probe", lambda cmd: subprocess.CompletedProcess(cmd, 0,
        "0, Synthetic Replacement GPU, 49152, 47000, 7.5" if cmd[0] == "nvidia-smi" else '{"nvidia": {}}', ""))
    assert installer._collect_preflight("review").ready
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("unsupported saved precision reached a command"))
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **k: pytest.fail("unsupported saved precision staged a release"))
    args = installer._parser().parse_args(["install" if command == "resume" else "update", "--root", str(root),
                                          "--non-interactive", "--no-backup"])
    action = installer._resume_node if command == "resume" else installer._update
    with pytest.raises(RuntimeError, match="prerequisites"):
        action(installer.Console(color=False, quiet=True), args, root)
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("command,shortage", [
    (command, shortage) for command in ("resume", "update")
    for shortage in ("explicit-device", "quality-model", "utilization", "transcription")
    if command != "update" or shortage not in {"explicit-device", "utilization"}
])
def test_saved_gpu_preflight_blocks_resume_update_before_commands(
    tmp_path, request, monkeypatch, command, shortage,
):
    models = "transcription" if shortage == "transcription" else "review"
    root, installation, environment = saved_gpu_node(tmp_path, models=models)
    request.getfixturevalue("ready_host")
    inventories = {
        "explicit-device": "0, Synthetic Busy, 49152, 1000, 8.9\n1, Synthetic Free, 49152, 47000, 8.9",
        "quality-model": "0, Synthetic Small, 24576, 24000, 8.0",
        "utilization": "0, Synthetic Partial, 49152, 35000, 8.9",
        "transcription": "0, Synthetic Transcription, 49152, 44000, 8.9",
    }
    if shortage == "utilization":
        environment["EXCULPATA_GENERATOR_GPU_UTILIZATION"] = environment["RECORDBENCH_GENERATOR_GPU_UTILIZATION"] = "0.90"
        (root / "compose.env").write_text(installer._env_text(environment, "synthetic saved reservation"))
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(installer, "_probe", lambda cmd: subprocess.CompletedProcess(cmd, 0,
        inventories[shortage] if cmd[0] == "nvidia-smi" else '{"nvidia": {}}', ""))
    # Demonstrate why checking an automatic plan is insufficient: it can pass by
    # selecting a different GPU, smaller model, or smaller reservation.
    assert installer._collect_preflight(models).ready
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("blocked saved plan reached a command"))
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **k: pytest.fail("blocked saved plan staged a release"))
    args = installer._parser().parse_args(["install" if command == "resume" else "update", "--root", str(root),
                                          "--non-interactive", "--no-backup", "--generator-gpus", "1",
                                          "--review-model-profile", "portable"])
    action = installer._resume_node if command == "resume" else installer._update
    with pytest.raises(RuntimeError, match="prerequisites"):
        action(installer.Console(color=False, quiet=True), args, root)
    assert args.generator_gpus == "0" and args.review_model_profile == "quality"
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_completed_diarization_resume_uses_saved_plan_without_new_token(tmp_path, request, monkeypatch):
    root, installation, environment = saved_gpu_node(tmp_path, model_profile="portable", models="transcription")
    installation["transcription_diarization"] = True
    installation["transcription_languages"] = ["en", "es"]
    environment["EXCULPATA_TRANSCRIPTION_GPU"] = environment["RECORDBENCH_TRANSCRIPTION_GPU"] = "1"
    (root / "installation.json").write_text(json.dumps(installation))
    (root / "compose.env").write_text(installer._env_text(environment, "synthetic saved transcription"))
    installer._seal_provisioning(root, installation)
    import importlib.util
    spec = importlib.util.spec_from_file_location("synthetic_saved_stager", ROOT / "scripts/stage-models.py")
    stager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stager)
    artifact = root / "models/synthetic-speaker-model.bin"
    artifact.write_bytes(b"synthetic cached speaker model")
    stager._stage_receipt(root / "models", ROOT / "config/models.json",
        frozenset({"transcription-asr", "transcription-alignment-en", "transcription-alignment-es", "transcription-diarization"}),
        "portable", [artifact])
    request.getfixturevalue("ready_host")
    monkeypatch.setattr(installer, "_probe", lambda cmd: subprocess.CompletedProcess(cmd, 0,
        "0, Synthetic Busy, 24576, 1000, 8.0\n1, Synthetic Selected, 24576, 24000, 8.0"
        if cmd[0] == "nvidia-smi" else '{"nvidia": {}}', ""))
    commands = []
    def command(console, cmd, **kw):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(installer, "_run", command)
    monkeypatch.setattr(installer, "_hf_token", lambda *a: pytest.fail("completed resume must remain offline"))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume", "--non-interactive", "--dry-run"])
    installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert "config" in commands[0] and "up" in commands[-1]
    assert not any("stage" in command for command in commands)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert args.transcription_gpu == "1" and args.transcription_languages == "en,es"
    assert args.transcription_min_free_vram_mib == 16000


@pytest.mark.parametrize("account_present", [False, True])
def test_resumed_local_initialization_requires_input_before_commands(tmp_path, request, monkeypatch, account_present):
    from tests.test_first_run_handoff import configured_node
    from case_intelligence.local_accounts import LocalAccountRepository
    root, _, paths = configured_node(tmp_path.resolve())
    if account_present:
        LocalAccountRepository(paths["accounts"] / "local-accounts.json").initialize(
            "alice.admin", "Alice Administrator", "synthetic-resume-password", actor="synthetic-operator")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    request.getfixturevalue("ready_host")
    commands = []
    monkeypatch.setattr(installer, "_run", lambda *a, **kw: commands.append(a[1]))
    monkeypatch.setattr(installer, "_provision", lambda *a, **kw: commands.append("provision"))
    monkeypatch.setattr(installer, "_password", lambda *a: pytest.fail("preflight consumed a password"))
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume", "--non-interactive"])
    if account_present:
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
        assert commands and commands[-1] == "provision"
    else:
        with pytest.raises(RuntimeError, match="prerequisites"):
            installer._resume_node(installer.Console(color=False, quiet=True), args, root)
        assert not commands
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_configure_normalizes_padded_administrator_display_name(tmp_path):
    display = "A" * 160
    args = installer._parser().parse_args(["install", "--auth", "local", "--models", "none", "--non-interactive",
        "--dry-run", "--admin-display-name", "  " + display + "  "])
    installer._collect_identity_choices(args)
    root = tmp_path.resolve() / "node"
    result = installer._configure(installer.Console(color=False, quiet=True), args, root,
        installer._paths(root), "synthetic-release", ROOT, ())
    assert result[-1] == display
    assert not root.exists()


@pytest.mark.parametrize("record", [None, [], {"auth": "unsupported", "models": "none"}, {"auth": []}, {"auth": "local", "models": {}}, {"auth": "local", "profiles": [3]}])
def test_resume_unrelated_root_stops_before_writes(tmp_path, ready_host, monkeypatch, record):
    node = tmp_path.resolve() / "unrelated"
    node.mkdir()
    sentinel = node / "compose.env"
    sentinel.write_text("synthetic existing data")
    if record is not None:
        (node / "installation.json").write_text(json.dumps(record))
    before = {path.name: path.read_bytes() for path in node.iterdir()}
    args = preflight_args(tmp_path, "--root", str(node), "--resume")
    assert not installer._collect_preflight("none", args).ready
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("prepared storage"))
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("ran install command"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--resume", "--models", "none", "--non-interactive", "--password-stdin"])
    assert installer.main() == 1
    assert {path.name: path.read_bytes() for path in node.iterdir()} == before


@pytest.mark.parametrize("reserved", ["matters", ".matter-purging", "ingestion-staging"])
@pytest.mark.parametrize("kind", ["directory", "file", "broken-link"])
def test_preflight_reserved_storage_collision_stops_install(tmp_path, ready_host, monkeypatch, reserved, kind):
    storage = tmp_path.resolve() / "storage"
    storage.mkdir()
    entry = storage / reserved
    if kind == "directory":
        entry.mkdir()
    elif kind == "file":
        entry.write_text("synthetic unrelated data")
    else:
        entry.symlink_to(storage / "missing")
    args = preflight_args(tmp_path, "--storage-root", str(storage))
    result = installer._collect_preflight("none", args)
    assert not result.ready
    assert checks_by_name(result)["matter-storage-layout"].state == "fail"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("prepared storage"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--storage-root", str(storage), "--models", "none", "--non-interactive", "--password-stdin"])
    assert installer.main() == 1
    assert not args.root.exists()
    assert list(storage.iterdir()) == [entry]


@pytest.mark.parametrize("auth,option,value", [
    ("oidc", "--oidc-issuer", "http://identity.example.test"),
    ("kerberos", "--kerberos-realm", "EXAMPLE"),
])
def test_provider_semantics_stop_install_before_writes(tmp_path, ready_host, monkeypatch, auth, option, value):
    args = preflight_args(tmp_path, "--auth", auth, option, value, "--dry-run")
    result = installer._collect_preflight("none", args)
    assert not result.ready
    assert checks_by_name(result)["identity-options"].state == "fail"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("prepared storage"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--models", "none", "--auth", auth, option, value, "--non-interactive", "--dry-run"])
    assert installer.main() == 1
    assert not args.root.exists()


@pytest.mark.parametrize("bind", ["127.0.0.1\nINJECTED=1", "example.test", "127.0.0.1:8443", "[::1]", "", "2001:db8::1%eth0\nINJECTED=1", "2001:db8::1%eth0"])
def test_bind_literal_with_tls_stops_before_writes(tmp_path, ready_host, monkeypatch, bind):
    cert, key = tmp_path / "synthetic.crt", tmp_path / "synthetic.key"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    options = ["--bind-address", bind, "--tls-cert", str(cert), "--tls-key", str(key)]
    args = preflight_args(tmp_path, *options)
    result = installer._collect_preflight("none", args)
    assert not result.ready
    assert checks_by_name(result)["bind-address"].state == "fail"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("prepared storage"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--models", "none", "--non-interactive", "--password-stdin", *options])
    assert installer.main() == 1
    assert not args.root.exists()

@pytest.mark.parametrize("bind", ["127.0.0.1", "0.0.0.0", "192.0.2.10", "::1", "2001:db8::10"])
def test_preflight_accepts_ip_bind_literals(tmp_path, ready_host, bind):
    args = preflight_args(tmp_path, "--bind-address", bind)
    assert checks_by_name(installer._collect_preflight("none", args))["bind-address"].state == "pass"


def test_preflight_preserves_initialized_storage_without_writes(tmp_path, ready_host, monkeypatch):
    storage = tmp_path.resolve() / "storage"
    storage.mkdir()
    for name in ("matters", ".matter-purging", "ingestion-staging"):
        (storage / name).mkdir()
    marker = storage / ".recordbench-managed-storage.json"
    marker.write_text(json.dumps({"format_version": 1, "product": "RecordBench", "storage_id": "recordbench-storage-synthetic"}))
    (storage / "matters" / "synthetic.txt").write_text("synthetic matter fixture")
    args = preflight_args(tmp_path, "--storage-root", str(storage))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("preflight wrote storage"))
    assert installer._collect_preflight("none", args).ready
    marker.write_text("[]")
    assert checks_by_name(installer._collect_preflight("none", args))["matter-storage-layout"].state == "fail"


@pytest.mark.parametrize("auth,options,expected", [
    ("oidc", [], True),
    ("oidc", ["--oidc-issuer", "https://identity.example.test/realm/"], True),
    ("oidc", ["--oidc-issuer", "http://identity.example.test"], False),
    ("oidc", ["--oidc-issuer", "https://identity.example.test?query=yes"], False),
    ("oidc", ["--oidc-issuer", "https://identity.example.test/#fragment"], False),
    ("oidc", ["--oidc-issuer", "https://user:pass@identity.example.test"], False),
    ("oidc", ["--oidc-issuer", "https://localhost"], False),
    ("oidc", ["--oidc-issuer", "https://127.0.0.1"], False),
    ("oidc", ["--server-name", "localhost"], False),
    ("oidc", ["--server-name", "127.0.0.1"], False),
    ("oidc", ["--server-name", " RecordBench.Example.Test "], True),
    ("oidc", ["--oidc-issuer", "https://identity.example.test/" + "a" * 1024], False),
    ("oidc", ["--oidc-client-id", ""], False),
    ("oidc", ["--oidc-client-id", "a\tb"], False),
    ("oidc", ["--oidc-client-id", "a" * 513], False),
    ("oidc", ["--oidc-allowed-groups", "a" * 257], False),
    ("oidc", ["--oidc-admin-groups", ",".join(f"group{i}" for i in range(51))], False),
    ("oidc", ["--oidc-allowed-groups", ",".join(f"group{i}" for i in range(101))], False),
    ("oidc", ["--oidc-allowed-groups", "", "--oidc-admin-groups", ""], True),
    ("oidc", ["--oidc-allowed-groups", "Team,team"], True),
    ("kerberos", [], True),
    ("kerberos", ["--kerberos-realm", " example.test "], True),
    ("kerberos", ["--kerberos-realm", "EXAMPLE"], False),
    ("kerberos", ["--kerberos-realm=-EXAMPLE.TEST"], False),
    ("kerberos", ["--kerberos-realm", "a" * 250 + ".test"], False),
    ("kerberos", ["--kerberos-allowed-groups", "team/invalid"], False),
    ("kerberos", ["--kerberos-allowed-groups", "a" * 256], False),
    ("kerberos", ["--kerberos-allowed-groups", "", "--kerberos-admin-groups", ""], False),
    ("kerberos", ["--kerberos-allowed-groups", "", "--kerberos-admin-groups", "Team Admins"], True),
    ("kerberos", ["--kerberos-admin-groups", ",".join(f"group{i}" for i in range(51))], False),
    ("kerberos", ["--kerberos-allowed-groups", ",".join(f"group{i}" for i in range(101))], False),
    ("kerberos", ["--kerberos-allowed-groups", "Team,team"], True),
])
def test_preflight_nonsecret_settings_match_identity_runtime(tmp_path, ready_host, auth, options, expected):
    from case_intelligence.identity import KerberosSettings, OidcSettings
    args = preflight_args(tmp_path, "--auth", auth, "--dry-run", *options)
    values = {name: getattr(args, name) if getattr(args, name) is not None else default
              for name, _prompt, default in installer.IDENTITY_FIELDS[auth]}
    groups = {suffix: frozenset(value.strip() for value in values[f"{auth}_{suffix}"].split(",") if value.strip())
              for suffix in ("allowed_groups", "admin_groups")}
    try:
        if auth == "oidc":
            OidcSettings(issuer=values["oidc_issuer"],
                         external_origin=f"https://{installer._valid_host(args.server_name or 'recordbench.example.test')}:{args.https_port}",
                         client_id=values["oidc_client_id"], client_secret="synthetic-secret-value",
                         allowed_groups=groups["allowed_groups"], administrator_groups=groups["admin_groups"])
        else:
            KerberosSettings(realm=values["kerberos_realm"], proxy_secret="synthetic-secret-value-0123456789abcdef",
                             allowed_groups=groups["allowed_groups"], administrator_groups=groups["admin_groups"])
        runtime_valid = True
    except (RuntimeError, ValueError):
        runtime_valid = False
    assert runtime_valid is expected
    result = installer._collect_preflight("none", args)
    assert (checks_by_name(result)["identity-options"].state == "pass") is runtime_valid
    assert not args.root.exists()

@pytest.mark.parametrize("relative", ["compose.env", "installation.json", "config/recordbench.env", "releases/capsule", "state"])
def test_matter_storage_control_path_overlap_stops_before_writes(tmp_path, ready_host, monkeypatch, relative):
    node = tmp_path.resolve() / "new-node"
    flags = ["--root", str(node), "--storage-root", str(node / relative), "--models", "none", "--non-interactive", "--password-stdin"]
    args = installer._parser().parse_args(["preflight", *flags])
    assert not installer._collect_preflight("none", args).ready
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("overlap reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", *flags])
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.parametrize("field", ["--root", "--storage-root"])
@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x7f", "\x00"])
def test_storage_control_characters_stop_before_writes(tmp_path, ready_host, monkeypatch, field, control):
    node = tmp_path.resolve() / "new-node"
    malformed = str(tmp_path.resolve() / ("synthetic" + control + "path"))
    flags = ["--root", str(node), field, malformed, "--models", "none", "--non-interactive", "--password-stdin"]
    args = installer._parser().parse_args(["preflight", *flags])
    assert not installer._collect_preflight("none", args).ready
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("invalid path reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", *flags])
    assert installer.main() == 1
    assert not node.exists() and not Path(malformed).exists()


def test_oversized_oidc_secret_stops_before_writes(tmp_path, ready_host, monkeypatch):
    source = tmp_path / "synthetic-secret"
    source.write_text("x" * 4097)
    args = preflight_args(tmp_path, "--auth", "oidc", "--non-interactive", "--oidc-client-secret-file", str(source))
    assert not installer._collect_preflight("none", args).ready
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("oversized secret reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--models", "none", "--auth", "oidc", "--oidc-client-secret-file", str(source), "--non-interactive"])
    assert installer.main() == 1
    assert not args.root.exists()


def synthetic_live_update(tmp_path, monkeypatch, request, *, after_free=47000, fail_command=None, dry_run=False, utilization="0.72"):
    root, installation, environment = saved_gpu_node(tmp_path)
    # Build the saved node using the real owner, then emulate the target Linux
    # host for read-only prerequisites and the mocked update lifecycle.
    request.getfixturevalue("ready_host")
    environment["EXCULPATA_GENERATOR_GPU_UTILIZATION"] = environment["RECORDBENCH_GENERATOR_GPU_UTILIZATION"] = utilization
    environment["EXCULPATA_COMPOSE_PROJECT"] = environment["RECORDBENCH_COMPOSE_PROJECT"] = "recordbench-synthetic-upgrade"
    (root / "compose.env").write_text(installer._env_text(environment, "synthetic current runtime"))
    old_release = Path(installation["release_path"])
    new_release = tmp_path.resolve() / "new-release"
    new_release.mkdir()
    (new_release / "compose.yaml").write_text("services: {}\n")
    events = []
    state = {"stopped": False}
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    def probe(command):
        if command[0] == "nvidia-smi":
            events.append("probe-stopped" if state["stopped"] else "probe-live")
            if state["stopped"] and after_free is None:
                raise OSError("synthetic GPU probe failed")
            free = after_free if state["stopped"] else 8000
            return subprocess.CompletedProcess(command, 0, f"0, Synthetic GPU, 49152, {free}, 8.9", "")
        return subprocess.CompletedProcess(command, 0, '{"nvidia": {}}', "")
    def run(console, command, **kwargs):
        verb = next((value for value in ("build", "config", "stop", "up") if value in command), "other")
        release = Path(command[command.index("-f") + 1]).parent if "-f" in command else None
        events.append(f"{verb}-{'old' if release == old_release else 'new'}")
        assert "down" not in command and "--volumes" not in command
        if verb == "stop":
            assert command[command.index("--env-file") + 1] == str(root / "compose.env")
            assert installer._dotenv(root / "compose.env")["RECORDBENCH_COMPOSE_PROJECT"] == "recordbench-synthetic-upgrade"
            assert release == old_release
            if not kwargs.get("dry_run"):
                state["stopped"] = True
        if verb == fail_command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "", "")
    def stage(*a, **k):
        events.append("stage")
        return "synthetic-new", new_release
    monkeypatch.setattr(installer, "_probe", probe)
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_stage_release", stage)
    monkeypatch.setattr(installer, "_wait_health", lambda *a, **k: events.append("health"))
    monkeypatch.setattr(installer, "_seal_provisioning", lambda *a, **k: events.append("seal"))
    args = installer._parser().parse_args(["update", "--root", str(root), "--non-interactive", "--no-backup", *(["--dry-run"] if dry_run else [])])
    return root, args, events, before


def test_live_gpu_update_checks_freed_capacity_after_own_runtime_stops(tmp_path, request, monkeypatch):
    root, args, events, _before = synthetic_live_update(tmp_path, monkeypatch, request)
    installer._update(installer.Console(color=False, quiet=True), args, root)
    assert events.index("probe-live") < events.index("build-new")
    assert events.index("build-new") < events.index("config-new") < events.index("stop-old")
    assert events.index("stop-old") < events.index("probe-stopped") < events.index("up-new")
    assert json.loads((root / "installation.json").read_text())["release_id"] == "synthetic-new"

@pytest.mark.parametrize("after_free,utilization", [(1000, "0.72"), (35000, "0.72"), (43000, "0.90"), (None, "0.72")])
def test_live_update_preserves_competing_usage_and_rolls_back(tmp_path, request, monkeypatch, after_free, utilization):
    root, args, events, before = synthetic_live_update(tmp_path, monkeypatch, request, after_free=after_free, utilization=utilization)
    args.generator_gpus = "1"
    args.review_model_profile = "portable"
    with pytest.raises(RuntimeError, match="update failed and rollback was attempted"):
        installer._update(installer.Console(color=False, quiet=True), args, root)
    assert events.index("stop-old") < events.index("probe-stopped") < events.index("up-old")
    assert "up-new" not in events and events[-1] == "health"
    assert args.generator_gpus == "0" and args.review_model_profile == "quality"
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("failed", ["build", "config", "stop"])
def test_update_failure_keeps_or_restores_old_node(tmp_path, request, monkeypatch, failed):
    root, args, events, before = synthetic_live_update(tmp_path, monkeypatch, request, fail_command=failed)
    with pytest.raises(RuntimeError, match="update failed and rollback was attempted"):
        installer._update(installer.Console(color=False, quiet=True), args, root)
    assert "up-new" not in events and "up-old" in events
    if failed != "stop":
        assert "stop-old" not in events
    assert "probe-stopped" not in events
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_live_gpu_update_dry_run_defers_post_stop_probe(tmp_path, request, monkeypatch, capsys):
    root, args, events, before = synthetic_live_update(tmp_path, monkeypatch, request, dry_run=True)
    installer._update(installer.Console(color=False), args, root)
    output = capsys.readouterr().out
    assert "actual free-memory admission is deferred" in output
    assert events.count("probe-live") == 1 and "probe-stopped" not in events
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_deferred_update_check_preserves_real_inventory_and_strict_default(tmp_path, request, monkeypatch):
    root, args, events, _before = synthetic_live_update(tmp_path, monkeypatch, request)
    installation, _release = installer._installed_release(root)
    installer._restore_model_options(args, installation, installer._dotenv(root / "compose.env"), {})
    strict = installer._collect_preflight("review", model_args=args, needs_model_staging=False)
    assert not strict.ready and checks_by_name(strict)["gpu"].state == "fail"
    deferred = installer._collect_preflight("review", model_args=args, needs_model_staging=False, defer_gpu_free_check=True)
    assert deferred.ready and deferred.devices[0].free_mib == 8000
    assert "deferred" in checks_by_name(deferred)["gpu"].observed
    assert events == ["probe-live", "probe-live"]


@pytest.mark.parametrize("relative", ["matter-storage", "custom-sources", "custom-sources/nested"])
def test_safe_nested_source_storage_remains_allowed(tmp_path, ready_host, relative):
    node = tmp_path.resolve() / "node"
    args = preflight_args(tmp_path, "--root", str(node), "--storage-root", str(node / relative))
    assert installer._collect_preflight("none", args).ready
    assert not node.exists()


@pytest.mark.parametrize("selection", ["same", "parent"])
def test_source_storage_cannot_equal_or_contain_node(tmp_path, ready_host, monkeypatch, selection):
    node = tmp_path.resolve() / "node"
    storage = node if selection == "same" else node.parent
    args = preflight_args(tmp_path, "--root", str(node), "--storage-root", str(storage))
    result = installer._collect_preflight("none", args)
    assert not result.ready and checks_by_name(result)["storage-separation"].state == "fail"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("overlap reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(node), "--storage-root", str(storage), "--models", "none", "--non-interactive", "--password-stdin"])
    assert installer.main() == 1
    assert not node.exists()


@pytest.mark.parametrize("auth,size,expected", [("oidc", 16, True), ("oidc", 4096, True), ("oidc", 4097, False), ("kerberos", 1, True), ("kerberos", 4097, True), ("kerberos", 1024 * 1024, True), ("kerberos", 1024 * 1024 + 1, False)])
def test_provider_validation_keeps_larger_keytabs(tmp_path, ready_host, monkeypatch, auth, size, expected):
    source = tmp_path / "synthetic-credential"
    source.write_bytes(b"x" * size)
    option = "--oidc-client-secret-file" if auth == "oidc" else "--kerberos-keytab"
    args = preflight_args(tmp_path, "--auth", auth, "--dry-run", option, str(source))
    result = installer._collect_preflight("none", args)
    assert result.ready is expected
    assert not args.root.exists()

@pytest.mark.parametrize("state", ["failed", "deferred"])
def test_gpu_update_requires_successful_backup_before_stopping(tmp_path, request, monkeypatch, state):
    root, args, events, _before = synthetic_live_update(tmp_path, monkeypatch, request)
    (root / "config" / "backup.json").write_text("{}")
    (root / "state").mkdir(exist_ok=True)
    (root / "state" / "backup-status.json").write_text(json.dumps({"state": state}))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError, match="newly succeeded backup"):
        installer._update(installer.Console(color=False, quiet=True), args, root)
    assert not any(event.startswith(("stop-", "up-", "build-")) for event in events)
    assert "stage" not in events
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_failed_post_stop_admission_reports_failed_rollback(tmp_path, request, monkeypatch):
    root, args, events, before = synthetic_live_update(tmp_path, monkeypatch, request, after_free=1000, fail_command="up")
    with pytest.raises(RuntimeError, match="rollback also failed"):
        installer._update(installer.Console(color=False, quiet=True), args, root)
    assert "up-new" not in events and events[-1] == "up-old"
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before

@pytest.mark.parametrize("size", [1, 15])
def test_undersized_oidc_secret_blocks_before_writes(tmp_path, ready_host, monkeypatch, size):
    source = tmp_path / "synthetic-small-secret"
    source.write_bytes(b"x" * size)
    flags = ["--auth", "oidc", "--oidc-client-secret-file", str(source), "--non-interactive"]
    args = preflight_args(tmp_path, *flags)
    result = installer._collect_preflight("none", args)
    assert not result.ready and checks_by_name(result)["oidc-secret-input"].state == "fail"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("small secret reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--models", "none", *flags])
    assert installer.main() == 1
    assert not args.root.exists() and source.stat().st_size == size


@pytest.mark.parametrize("field", ["--tls-cert", "--tls-key"])
@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x7f"])
def test_existing_tls_control_path_blocks_before_writes(tmp_path, ready_host, monkeypatch, field, control):
    cert, key = tmp_path / "synthetic.crt", tmp_path / "synthetic.key"
    malformed = tmp_path / ("synthetic" + control + "tls")
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    malformed.write_text("synthetic supplied TLS material")
    flags = ["--tls-cert", str(cert), "--tls-key", str(key), field, str(malformed), "--non-interactive", "--password-stdin"]
    args = preflight_args(tmp_path, *flags)
    result = installer._collect_preflight("none", args)
    assert not result.ready and checks_by_name(result)["tls"].state == "fail"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("invalid TLS path reached writes"))
    monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--models", "none", *flags])
    assert installer.main() == 1
    assert not args.root.exists()

@pytest.mark.parametrize("field", ["--tls-cert", "--tls-key"])
def test_tls_nul_path_rejected_before_file_metadata(tmp_path, ready_host, monkeypatch, field):
    cert, key = tmp_path / "synthetic.crt", tmp_path / "synthetic.key"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    malformed = tmp_path / "synthetic\x00tls"
    args = preflight_args(tmp_path, "--tls-cert", str(cert), "--tls-key", str(key), field, str(malformed))
    original = Path.is_file
    def checked(path):
        assert "\x00" not in str(path), "invalid TLS path reached a file operation"
        return original(path)
    monkeypatch.setattr(Path, "is_file", checked)
    result = installer._collect_preflight("none", args)
    assert not result.ready and checks_by_name(result)["tls"].state == "fail"
    assert not args.root.exists()


def test_cli_preflight_accepts_new_local_account_management(tmp_path, ready_host, monkeypatch, capsys):
    root = tmp_path.resolve() / "new-managed-node"
    monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("preflight prepared directories"))
    monkeypatch.setattr(installer, "_password", lambda *a: pytest.fail("preflight consumed a password"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--auth", "local", "--enable-account-management", "--models", "none", "--non-interactive", "--password-stdin", "--json"])
    assert installer.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True
    assert not root.exists()


@pytest.mark.parametrize("explicit_flags", [[], ["--auth", "local", "--enable-account-management"]])
@pytest.mark.parametrize("account_present", [False, True])
def test_cli_preflight_resume_uses_canonical_managed_account_storage(tmp_path, request, monkeypatch, capsys, explicit_flags, account_present):
    from tests.test_first_run_handoff import configured_node
    from case_intelligence.local_accounts import LocalAccountRepository
    root, _, paths = configured_node(tmp_path.resolve())
    accounts = tmp_path.resolve() / "relocated-accounts"
    paths["accounts"].rename(accounts)
    installer._replace_env(root / "compose.env", "RECORDBENCH_LOCAL_ACCOUNT_ROOT", str(accounts))
    # An obsolete legacy copy cannot stand in for the canonical managed mount.
    selected = accounts if account_present else paths["secrets"]
    LocalAccountRepository(selected / "local-accounts.json").initialize(
        "alice.admin", "Alice Administrator", "synthetic-preflight-password", actor="synthetic-operator")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    request.getfixturevalue("ready_host")
    capsys.readouterr()
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("preflight issued a runtime command"))
    monkeypatch.setattr(installer, "_password", lambda *a: pytest.fail("preflight consumed a password"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--resume", "--non-interactive", "--json", *explicit_flags])
    assert installer.main() == (0 if account_present else 1)
    result = json.loads(capsys.readouterr().out)
    checks = {row["name"]: row for row in result["checks"]}
    assert result["ready"] is account_present
    assert ("admin-password-input" not in checks) is account_present
    if not account_present:
        assert checks["admin-password-input"]["state"] == "fail"
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_cli_preflight_invalid_saved_state_is_blocking_json(tmp_path, request, monkeypatch, capsys):
    from tests.test_first_run_handoff import configured_node
    root, _, _paths = configured_node(tmp_path.resolve())
    (root / "installation.json").write_text("[]")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    request.getfixturevalue("ready_host")
    capsys.readouterr()
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("preflight issued a runtime command"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--resume", "--json"])
    assert installer.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert not payload["ready"] and payload["checks"][0]["name"] == "saved-node"
    assert payload["checks"][0]["blocking"] is True
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_relative_tls_paths_keep_launch_location_in_staged_configuration(tmp_path, ready_host, monkeypatch):
    launch = tmp_path.resolve() / "launch"
    launch.mkdir()
    cert, key = launch / "synthetic certificate.crt", launch / "synthetic key.pem"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    monkeypatch.chdir(launch)
    root = tmp_path.resolve() / "node"
    args = installer._parser().parse_args(["install", "--root", str(root), "--auth", "local", "--models", "none", "--non-interactive", "--password-stdin", "--tls-cert", cert.name, "--tls-key", key.name])
    result = installer._collect_preflight("none", args)
    assert result.ready
    assert args.tls_cert == cert and args.tls_key == key
    paths = installer._paths(root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    release = tmp_path.resolve() / "staged-release"
    release.mkdir()
    (release / "compose.yaml").write_text("services: {}\n")
    monkeypatch.chdir(release)
    installer._collect_identity_choices(args)
    installer._configure(installer.Console(color=False, quiet=True), args, root, paths, "synthetic-release", release, ())
    values = installer._dotenv(root / "compose.env")
    assert values["EXCULPATA_TLS_CERT"] == str(cert)
    assert values["EXCULPATA_TLS_KEY"] == str(key)


@pytest.mark.parametrize("field", ["--tls-cert", "--tls-key"])
def test_relative_tls_symlink_remains_rejected_after_absolutizing(tmp_path, ready_host, monkeypatch, field):
    launch = tmp_path.resolve()
    cert, key = launch / "synthetic.crt", launch / "synthetic.key"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    link = launch / "synthetic-link"
    link.symlink_to(cert if field == "--tls-cert" else key)
    monkeypatch.chdir(launch)
    args = preflight_args(tmp_path, "--tls-cert", cert.name, "--tls-key", key.name, field, link.name)
    assert checks_by_name(installer._collect_preflight("none", args))["tls"].state == "fail"
    assert not args.root.exists()


def synthetic_late_gpu_resume(tmp_path, monkeypatch, request, *, after_free=47000,
                              running="app\ngateway\ngenerator\npostgres\ntranscription-worker\n",
                              failure=None, dry_run=False, live_free=8000):
    root, installation, environment = saved_gpu_node(tmp_path)
    request.getfixturevalue("ready_host")
    events = []
    commands = []
    state = {"stopped": False, "initial": True}
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume",
        "--non-interactive", *(["--dry-run"] if dry_run else [])])
    installer._saved_node_arguments(args, root)
    def probe(command):
        if command[0] == "nvidia-smi":
            events.append("probe-stopped" if state["stopped"] else "probe-live")
            if state["stopped"] and after_free is None:
                raise OSError("synthetic unavailable GPU probe")
            free = after_free if state["stopped"] else live_free
            return subprocess.CompletedProcess(command, 0, f"0, Synthetic GPU, 49152, {free}, 8.9", "")
        return subprocess.CompletedProcess(command, 0, '{"nvidia": {}}', "")
    def run(console, command, **kwargs):
        commands.append((command, kwargs.get("dry_run", False)))
        assert "down" not in command and "--volumes" not in command
        verb = next((value for value in ("build", "config", "ps", "stop", "start", "up", "run") if value in command), "other")
        events.append(verb)
        if verb == "stop" and not kwargs.get("dry_run"):
            state["stopped"] = True
        if not state["initial"] and (verb == failure or (failure == "stop-and-start" and verb in {"stop", "start"})):
            raise subprocess.CalledProcessError(1, command)
        if verb == "config" and "--services" in command:
            return subprocess.CompletedProcess(command, 0, "app\ngateway\ngenerator\npostgres\nretrieval\n", "")
        if verb == "ps":
            assert "--orphans=false" in command and "--status" in command and "running" in command
            return subprocess.CompletedProcess(command, 0, running, "")
        return subprocess.CompletedProcess(command, 0, "", "")
    def health(*a, **k):
        events.append("health")
        if state["initial"] or failure == "health":
            raise RuntimeError("synthetic health timeout after model startup")
    monkeypatch.setattr(installer, "_probe", probe)
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_wait_health", health)
    monkeypatch.setattr(installer, "_saved_models_verified", lambda *a, **k: True)
    # Reproduce an actual interruption boundary: provisioning seals its receipt,
    # starts the runtime, then fails to observe health consensus.
    args.dry_run = False
    with pytest.raises(RuntimeError, match="health timeout"):
        installer._provision(installer.Console(color=False, quiet=True), args, root,
            args.auth, args.models, None, None)
    assert installer._provisioning_complete(root, installation)
    args.dry_run = dry_run
    state["initial"] = False
    events.clear()
    commands.clear()
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    return root, args, events, commands, before


def test_late_gpu_resume_releases_only_observed_node_services_before_actual_free_admission(tmp_path, monkeypatch, request):
    root, args, events, commands, before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request)
    installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert events.index("probe-live") < events.index("ps") < events.index("stop")
    assert events.index("stop") < events.index("probe-stopped") < events.index("build") < events.index("up") < events.index("health")
    stop = next(command for command, _ in commands if "stop" in command)
    assert stop[stop.index("stop") + 1:] == ["--timeout", "120", "app", "gateway", "generator", "postgres"]
    assert "start" not in events
    assert (root / "compose.env").read_bytes() == before[Path("compose.env")]
    account_file = args.account_root / "local-accounts.json"
    assert account_file.read_bytes() == before[account_file.relative_to(root)]


@pytest.mark.parametrize("after_free", [1000, 35000, None])
def test_late_gpu_resume_restores_previous_services_when_actual_free_admission_fails(tmp_path, monkeypatch, request, after_free):
    root, args, events, commands, before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request, after_free=after_free)
    with pytest.raises(RuntimeError, match="resume failed"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert events.index("stop") < events.index("probe-stopped") < events.index("start")
    assert "build" not in events and "up" not in events
    start = next(command for command, _ in commands if "start" in command)
    assert start[start.index("start") + 1:] == ["app", "gateway", "generator", "postgres"]
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("failure", ["stop", "build", "health", "stop-and-start"])
def test_late_gpu_resume_restores_scoped_services_after_partial_stop_or_provision_failure(tmp_path, monkeypatch, request, failure):
    root, args, events, commands, _before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request, failure=failure)
    with pytest.raises(RuntimeError, match="restoration also failed" if failure == "stop-and-start" else "resume failed"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert events.index("stop") < events.index("start")
    start = next(command for command, _ in commands if "start" in command)
    assert start[start.index("start") + 1:] == ["app", "gateway", "generator", "postgres"]
    if failure.startswith("stop"):
        assert "probe-stopped" not in events and "build" not in events


@pytest.mark.parametrize("running", ["", "\n", "\n\n", "app\ngateway\npostgres\n", "app\ntranscription-worker\n"])
def test_late_gpu_resume_without_running_models_still_requires_actual_free_memory(tmp_path, monkeypatch, request, running):
    root, args, events, _commands, before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request, running=running)
    with pytest.raises(RuntimeError, match="prerequisites"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert events.count("probe-live") == 2 and "ps" in events
    assert all(event not in events for event in ["stop", "start", "build", "up"])
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_late_gpu_resume_with_free_capacity_and_no_running_models_can_continue(tmp_path, monkeypatch, request):
    root, args, events, _commands, _before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request, running="", live_free=47000)
    installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert events.count("probe-live") == 2 and "ps" in events
    assert "stop" not in events and "start" not in events
    assert events.index("build") < events.index("up") < events.index("health")


def test_late_gpu_resume_prepare_only_keeps_strict_admission_and_does_not_stop_services(tmp_path, monkeypatch, request):
    root, args, events, _commands, before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request)
    args.prepare_only = True
    with pytest.raises(RuntimeError, match="prerequisites"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert events == ["probe-live"]
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_late_gpu_resume_rejects_invalid_observed_service_names_before_mutations(tmp_path, monkeypatch, request):
    root, args, events, _commands, before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request, running="generator\n--all\n")
    with pytest.raises(RuntimeError, match="safely identify"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert "ps" in events and all(event not in events for event in ["stop", "start", "build", "up"])
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_late_gpu_resume_service_probe_failure_blocks_before_mutations(tmp_path, monkeypatch, request):
    root, args, events, _commands, before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request, failure="ps")
    with pytest.raises(subprocess.CalledProcessError):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert "ps" in events and all(event not in events for event in ["stop", "start", "build", "up"])
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_late_gpu_resume_dry_run_defers_actual_admission_without_runtime_changes(tmp_path, monkeypatch, request, capsys):
    root, args, events, commands, before = synthetic_late_gpu_resume(tmp_path, monkeypatch, request, dry_run=True)
    installer._resume_node(installer.Console(color=False), args, root)
    assert "actual free-memory admission is deferred" in capsys.readouterr().out
    assert events.count("probe-live") == 1 and "probe-stopped" not in events
    assert "ps" not in events and "stop" not in events and "start" not in events
    assert all(dry_run for _command, dry_run in commands)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("content,expected", [
    (b"x" * 14 + b"\r\n", False), (b"x" * 15 + b"\n", False),
    (b"x" * 16 + b"\r\n", True), (b"x" * 4096 + b"\n", True),
    (("\U0001f511" * 4096 + "\r\n").encode(), True),
    (b"x" * 16 + b"\xff", False), (b"x" * 16 + b"\ny", False),
    (b"x" * 16 + b"\x00", False), (b" " * 16, True),
])
def test_oidc_preflight_matches_runtime_decoded_stripped_secret_semantics(tmp_path, ready_host, monkeypatch, content, expected):
    from case_intelligence.identity import OidcSettings
    source = tmp_path / "synthetic-oidc-boundary"
    source.write_bytes(content)
    source.chmod(0o600)
    monkeypatch.setenv("CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE", str(source))
    monkeypatch.setenv("CASE_INTELLIGENCE_OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_EXTERNAL_ORIGIN", "https://recordbench.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_OIDC_CLIENT_ID", "synthetic-client")
    try:
        OidcSettings.from_env()
        runtime_accepts = True
    except RuntimeError:
        runtime_accepts = False
    assert runtime_accepts is expected
    args = preflight_args(tmp_path, "--auth", "oidc", "--oidc-client-secret-file", str(source), "--non-interactive")
    result = installer._collect_preflight("none", args)
    assert result.ready is expected
    assert checks_by_name(result)["oidc-secret-input"].state == ("pass" if expected else "fail")
    assert str(source) not in json.dumps(result.payload())
    assert not args.root.exists()
    if not expected:
        monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("invalid secret reached writes"))
        monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("invalid secret issued a runtime command"))
        monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--auth", "oidc", "--non-interactive", "--models", "none", "--oidc-client-secret-file", str(source)])
        assert installer.main() == 1
        assert not args.root.exists()


def synthetic_saved_account_preflight(tmp_path, request, monkeypatch, *, managed=True, mutation=None, root_style=False):
    from tests.test_first_run_handoff import configured_node
    from case_intelligence.local_accounts import LocalAccountRepository
    root, _, paths = configured_node(tmp_path.resolve())
    if not managed:
        installation = json.loads((root / "installation.json").read_text())
        installation["local_account_management"] = False
        (root / "installation.json").write_text(json.dumps(installation))
        installer._replace_env(root / "config/recordbench.env", "CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE", "/run/recordbench-secrets/local-accounts.json")
        installer._replace_env(root / "config/recordbench.env", "CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT", "")
    account_file = paths["accounts" if managed else "secrets"] / "local-accounts.json"
    LocalAccountRepository(account_file).initialize("alice.admin", "Alice Administrator", "synthetic-preflight-password", actor="synthetic-operator")
    if mutation == "symlink":
        target = account_file.with_name("synthetic-target")
        account_file.rename(target)
        account_file.symlink_to(target)
    elif mutation == "mode":
        account_file.chmod(0o644)
    elif mutation in {"malformed", "empty", "oversized", "invalid-utf8"}:
        account_file.write_bytes({"malformed": b"{synthetic invalid", "empty": b"", "oversized": b"x" * (1024 * 1024 + 1), "invalid-utf8": b"\xff"}[mutation])
    elif mutation in {"v1", "v3", "hash", "no-admin"}:
        value = json.loads(account_file.read_text())
        if mutation == "v1":
            value["format_version"] = 1
            for account in value["accounts"]:
                del account["session_revision"]
        elif mutation == "v3":
            value["format_version"] = 3
        elif mutation == "hash":
            value["accounts"][0]["password_hash"] = "$argon2id$synthetic-invalid"
        else:
            value["accounts"][0]["roles"] = []
        account_file.write_text(json.dumps(value))
    elif mutation in {"directory", "fifo"}:
        account_file.unlink()
        if mutation == "directory":
            account_file.mkdir()
        else:
            os.mkfifo(account_file, 0o600)
    if root_style:
        # Emulate a root-owned fixture on an unprivileged workstation; the
        # Linux descriptor path lookup is synthetic, with no chmod/chown.
        original_stat, original_fstat, original_readlink = Path.stat, os.fstat, os.readlink
        entries = [tmp_path, *tmp_path.parents, *tmp_path.rglob("*")]
        inode_paths = {(original_stat(path).st_dev, original_stat(path).st_ino): path for path in entries}
        def root_metadata(metadata):
            fields = list(metadata)
            fields[4] = 0
            return os.stat_result(fields)
        def descriptor_path(path, *a, **k):
            if str(path).startswith("/proc/self/fd/"):
                metadata = original_fstat(int(str(path).rsplit("/", 1)[1]))
                return str(inode_paths[(metadata.st_dev, metadata.st_ino)])
            return original_readlink(path, *a, **k)
        monkeypatch.setattr(Path, "stat", lambda path, *a, **k: root_metadata(original_stat(path, *a, **k)))
        monkeypatch.setattr(os, "fstat", lambda descriptor: root_metadata(original_fstat(descriptor)))
        monkeypatch.setattr(os, "readlink", descriptor_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
    request.getfixturevalue("ready_host")
    if mutation == "owner":
        original_fstat = os.fstat
        original_stat = Path.stat
        target = original_stat(account_file).st_ino
        def wrong_owner(metadata):
            if metadata.st_ino == target:
                fields = list(metadata)
                fields[4] = 12345
                return os.stat_result(fields)
            return metadata
        monkeypatch.setattr(os, "fstat", lambda descriptor: wrong_owner(original_fstat(descriptor)))
        monkeypatch.setattr(Path, "stat", lambda path, *a, **k: wrong_owner(original_stat(path, *a, **k)))
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume", "--non-interactive"])
    installer._saved_node_arguments(args, root)
    return root, args, account_file


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("mutation", ["symlink", "mode", "owner", "malformed", "empty", "oversized", "invalid-utf8", "v3", "hash", "no-admin", "directory", "fifo"])
def test_saved_account_preflight_rejects_unsafe_or_invalid_store_before_commands(tmp_path, request, monkeypatch, managed, mutation):
    root, args, account_file = synthetic_saved_account_preflight(tmp_path, request, monkeypatch, managed=managed, mutation=mutation)
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("invalid saved account reached a runtime command"))
    monkeypatch.setattr(installer, "_provision", lambda *a, **k: pytest.fail("invalid saved account reached provisioning"))
    result = installer._collect_preflight("none", args, needs_model_staging=False)
    assert not result.ready
    assert checks_by_name(result)["saved-node"].state == "fail"
    assert str(account_file) not in json.dumps(result.payload())
    with pytest.raises(RuntimeError, match="prerequisites"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)


@pytest.mark.parametrize("root_style", [False, True])
@pytest.mark.parametrize("managed,version,expected", [(False, 1, True), (False, 2, True), (True, 1, False), (True, 2, True)])
def test_saved_account_preflight_requires_v2_only_for_browser_management(tmp_path, request, monkeypatch, capsys, managed, version, expected, root_style):
    root, args, account_file = synthetic_saved_account_preflight(tmp_path, request, monkeypatch, managed=managed, mutation="v1" if version == 1 else None, root_style=root_style)
    before = account_file.read_bytes()
    result = installer._collect_preflight("none", args, needs_model_staging=False)
    assert result.ready is expected
    assert checks_by_name(result)["saved-node"].state == ("pass" if expected else "fail")
    capsys.readouterr()
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("standalone preflight issued a runtime command"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--resume", "--non-interactive", "--json"])
    assert installer.main() == (0 if expected else 1)
    assert json.loads(capsys.readouterr().out)["ready"] is expected
    assert account_file.read_bytes() == before


@pytest.mark.parametrize("display,expected", [("Synthetic\x7fAdmin", False), ("Synthetic\x85Admin", False),
    ("Synthetic\u202eAdmin", False), ("Synthetic\u200dAdmin", False), ("e\u0301" * 160, True)])
def test_initial_administrator_preflight_uses_shared_unicode_display_rules(tmp_path, ready_host, monkeypatch, display, expected):
    args = preflight_args(tmp_path, "--auth", "local", "--non-interactive", "--password-stdin", "--admin-display-name", display)
    result = installer._collect_preflight("none", args)
    assert result.ready is expected
    assert checks_by_name(result)["admin-identity"].state == ("pass" if expected else "fail")
    if not expected:
        monkeypatch.setattr(installer, "_prepare_directories", lambda *a, **k: pytest.fail("invalid display name reached writes"))
        monkeypatch.setattr(sys, "argv", ["install", "--root", str(args.root), "--models", "none", "--auth", "local", "--non-interactive", "--password-stdin", "--admin-display-name", display])
        assert installer.main() == 1
        assert not args.root.exists()


def test_ready_host_keeps_no_follow_path_and_descriptor_identity_consistent(tmp_path, ready_host):
    path = tmp_path / "synthetic-owner"
    path.write_text("synthetic metadata")
    path.chmod(0o600)
    link = tmp_path / "synthetic-link"
    link.symlink_to(path)
    assert path.stat().st_uid == path.lstat().st_uid == os.geteuid()
    assert stat.S_ISLNK(link.lstat().st_mode)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert os.fstat(descriptor).st_uid == path.stat().st_uid
    finally:
        os.close(descriptor)


def test_saved_account_preflight_allows_missing_leaf_with_initial_password_input(tmp_path, request, monkeypatch, capsys):
    root, args, account_file = synthetic_saved_account_preflight(tmp_path, request, monkeypatch)
    account_file.unlink()
    capsys.readouterr()
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("standalone preflight issued a runtime command"))
    monkeypatch.setattr(installer, "_password", lambda *a, **k: pytest.fail("preflight read a password"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--resume", "--non-interactive", "--password-stdin", "--json"])
    assert installer.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready"]
    assert next(row for row in result["checks"] if row["name"] == "admin-password-input")["state"] == "pass"
    assert not account_file.exists()


@pytest.mark.parametrize("bind,target", [("127.0.0.1", "127.0.0.1"), ("::1", "::1"),
    ("192.0.2.44", "192.0.2.44"), ("2001:db8::44", "2001:db8::44"),
    ("0.0.0.0", "127.0.0.1"), ("::", "::1")])
@pytest.mark.parametrize("auth", ["local", "oidc", "kerberos"])
@pytest.mark.parametrize("product_name", ["Exculpata", "RecordBench"])
def test_gateway_acceptance_uses_configured_bind_for_health_and_login(tmp_path, monkeypatch, bind, target, auth, product_name):
    import io
    from tests.test_first_run_handoff import configured_node
    root, _, _ = configured_node(tmp_path.resolve())
    installation = json.loads((root / "installation.json").read_text())
    installation["auth"] = auth
    (root / "installation.json").write_text(json.dumps(installation))
    installer._replace_env(root / "config/recordbench.env", "CASE_INTELLIGENCE_AUTH_MODE", auth)
    installer._replace_env(root / "compose.env", "RECORDBENCH_BIND_ADDRESS", bind)
    payload = {"product": "RecordBench", "status": "ok", "storage": {"status": "ready"},
        "capabilities": {"source_review": "ready", "malware_scan": "ready"}}
    requests = []
    class Connection:
        def __init__(self, address, port, **kwargs):
            assert address == target and port == 8443
            assert kwargs["timeout"] == 5
        def request(self, method, route, *, headers):
            assert method == "GET" and headers == {"Host": "recordbench.example.test"}
            requests.append(route)
        def getresponse(self):
            return self
        @property
        def status(self):
            return 401 if auth == "kerberos" else 200
        def getheader(self, name, default):
            return "Negotiate" if auth == "kerberos" else default
        def read(self, limit):
            assert limit == 262_144
            return f"Synthetic {product_name} sign-in page".encode()
        def close(self):
            pass
    def health_request(request, **kwargs):
        address = f"[{target}]" if ":" in target else target
        assert request.full_url == f"https://{address}:8443/health"
        assert request.get_header("Host") == "recordbench.example.test"
        requests.append("health-transport")
        return io.BytesIO(json.dumps(payload).encode())
    def internal_probe(console, command, **kwargs):
        assert auth == "kerberos" and "exec" in command and "app" in command
        requests.append("internal-kerberos-health")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
    monkeypatch.setattr(installer.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(installer.urllib.request, "urlopen", health_request)
    monkeypatch.setattr(installer, "_run", internal_probe)
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0, json.dumps({"auth_mode": auth}), ""))
    monkeypatch.setattr(installer.time, "sleep", lambda *a: pytest.fail("valid configured bind was not accepted"))
    assert installer._login_reachable(root)
    installer._wait_health(installer.Console(color=False, quiet=True), root)
    assert "/auth/login" in requests
    assert ("internal-kerberos-health" if auth == "kerberos" else "health-transport") in requests
    assert installer._install_progress(root)["phases"]["login_reachable"] == "complete"


@pytest.mark.parametrize("saved", [None, "", "preview", "test", "oidc", "synthetic-invalid-private-value"])
@pytest.mark.parametrize("operation", ["doctor", "health"])
def test_installed_auth_drift_is_rejected_before_runtime_probe(tmp_path, monkeypatch, capsys, saved, operation):
    from tests.test_first_run_handoff import configured_node
    root, args, _ = configured_node(tmp_path.resolve())
    env_path = root / "config/recordbench.env"
    values = installer._dotenv(env_path)
    if saved is None:
        values.pop("CASE_INTELLIGENCE_AUTH_MODE")
    else:
        values["CASE_INTELLIGENCE_AUTH_MODE"] = saved
    env_path.write_text(installer._env_text(values, "synthetic saved environment"))
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("auth drift reached Compose"))
    monkeypatch.setattr(installer, "_probe", lambda *a, **k: pytest.fail("auth drift reached runtime"))
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: pytest.fail("auth drift reached HTTP"))
    with pytest.raises(RuntimeError, match="authentication mode") as error:
        if operation == "doctor":
            installer._doctor(installer.Console(color=False), args, root)
        else:
            installer._wait_health(installer.Console(color=False), root)
    assert "synthetic-invalid-private-value" not in str(error.value) + capsys.readouterr().out


@pytest.mark.parametrize("expected", ["local", "oidc", "kerberos"])
@pytest.mark.parametrize("effective", ["preview", "test", "local", "oidc", "kerberos"])
def test_health_checks_running_auth_mode_before_accepting_healthy_node(tmp_path, monkeypatch, expected, effective):
    import io
    from tests.test_first_run_handoff import configured_node
    root, _, _ = configured_node(tmp_path.resolve())
    installation = json.loads((root / "installation.json").read_text())
    installation["auth"] = expected
    (root / "installation.json").write_text(json.dumps(installation))
    installer._replace_env(root / "config/recordbench.env", "CASE_INTELLIGENCE_AUTH_MODE", expected)
    payload = {"product": "RecordBench", "status": "ok", "storage": {"status": "ready"},
        "capabilities": {"source_review": "ready", "malware_scan": "ready"}}
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(installer, "_run", lambda command, *a, **k: subprocess.CompletedProcess(command, 0, json.dumps(payload), ""))
    probes = []
    def diagnostic(command):
        assert command[-6:-2] == ["exec", "-T", "app", "python"]
        assert "/internal/auth-mode" in command[-1]
        probes.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"auth_mode": effective}), "")
    monkeypatch.setattr(installer, "_probe", diagnostic)
    monkeypatch.setattr(installer, "_login_reachable", lambda root: True)
    monkeypatch.setattr(installer.time, "sleep", lambda *a: pytest.fail("live mode mismatch did not fail promptly"))
    if expected == effective:
        installer._wait_health(installer.Console(color=False, quiet=True), root)
        assert installer._install_progress(root)["phases"]["selected_capabilities"] == "complete"
    else:
        with pytest.raises(RuntimeError, match="running authentication mode"):
            installer._wait_health(installer.Console(color=False, quiet=True), root)
        assert installer._install_progress(root)["phases"]["selected_capabilities"] != "complete"
    assert len(probes) == 1


@pytest.mark.parametrize("result", [
    "", "not-json", "{}", "[]", '{"auth_mode": "unknown"}',
    '{"auth_mode": null}', '{"auth_mode": ["local"]}',
    '{"auth_mode": "local", "private": "synthetic-private-value"}',
    "synthetic-private-value" * 300, "command-error", "timeout", "missing-command",
])
def test_health_never_accepts_unavailable_live_auth_or_reports_probe_output(tmp_path, monkeypatch, capsys, result):
    import io
    from tests.test_first_run_handoff import configured_node
    root, _, _ = configured_node(tmp_path.resolve())
    payload = {"product": "RecordBench", "status": "ok", "storage": {"status": "ready"},
        "capabilities": {"source_review": "ready", "malware_scan": "ready"}}
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    def diagnostic(command):
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, 20, output="synthetic-private-value")
        if result == "missing-command":
            raise OSError("synthetic-private-value")
        return subprocess.CompletedProcess(command, 1 if result == "command-error" else 0,
                                           result, "synthetic-private-value")
    monkeypatch.setattr(installer, "_probe", diagnostic)
    monkeypatch.setattr(installer, "_login_reachable", lambda root: True)
    clock = [0]
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer.time, "sleep", lambda _: clock.__setitem__(0, 901))
    with pytest.raises(RuntimeError, match="running authentication mode is unavailable") as error:
        installer._wait_health(installer.Console(color=False), root)
    assert installer._install_progress(root)["phases"]["login_reachable"] == "incomplete"
    assert "synthetic-private-value" not in str(error.value) + capsys.readouterr().out


@pytest.mark.parametrize("key_length, status, payload, accepted", [
    (32, 200, {"auth_mode": "local"}, True),
    (32, 200, {"auth_mode": "preview"}, True),
    (31, 200, {"auth_mode": "local"}, False),
    (33, 200, {"auth_mode": "local"}, False),
    (32, 302, {"auth_mode": "local"}, False),
    (32, 404, {"auth_mode": "local"}, False),
    (32, 200, {"auth_mode": "synthetic-private-value"}, False),
    (32, 200, {"auth_mode": "local", "private": "synthetic-private-value"}, False),
    (32, 200, {"auth_mode": "local", "padding": "x" * 4096}, False),
])
def test_auth_probe_derives_proof_inside_container_and_only_emits_allowlisted_mode(monkeypatch, capsys, key_length, status, payload, accepted):
    import builtins
    import hashlib
    import hmac
    import io
    key = b"s" * key_length
    proof = hmac.new(key, b"recordbench/auth-mode-diagnostic/v1", hashlib.sha256).hexdigest()
    def key_file(path, mode):
        assert path == "/var/lib/recordbench/runtime/identity-session.key" and mode == "rb"
        return io.BytesIO(key)
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 8786 and timeout == 5
            self.status = status
        def request(self, method, path, headers):
            assert method == "GET" and path == "/internal/auth-mode"
            assert headers == {"X-RecordBench-Auth-Diagnostic": proof}
            calls.append("request")
        def getresponse(self):
            return self
        def read(self, size):
            assert size == 4097
            return json.dumps(payload).encode()[:size]
        def close(self):
            calls.append("close")
    monkeypatch.setattr(builtins, "open", key_file)
    monkeypatch.setattr(installer.http.client, "HTTPConnection", Connection)
    if accepted:
        exec(installer._AUTH_MODE_PROBE, {})
    else:
        with pytest.raises(SystemExit) as error:
            exec(installer._AUTH_MODE_PROBE, {})
        assert error.value.code == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (json.dumps(payload) + "\n" if accepted else "")
    assert proof not in output.out and "synthetic-private-value" not in output.out
    assert calls == (["request", "close"] if key_length == 32 else [])


@pytest.mark.parametrize("rollback_auth", ["local", "preview", None])
def test_update_auth_drift_rolls_back_and_rechecks_previous_live_mode(tmp_path, request, monkeypatch, capsys, rollback_auth):
    import io
    wait_health = installer._wait_health
    root, args, events, before = synthetic_live_update(tmp_path, monkeypatch, request)
    monkeypatch.setattr(installer, "_wait_health", wait_health)
    monkeypatch.setattr(installer, "_preflight", lambda *a, **k: ())
    payload = {"product": "RecordBench", "status": "ok", "storage": {"status": "ready"},
        "capabilities": {"source_review": "ready", "malware_scan": "ready",
                         "answering": "ready", "search": "word + meaning"}}
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(installer, "_login_reachable", lambda root: True)
    modes = []
    def diagnostic(command):
        installed, _ = installer._installed_release(root)
        mode = "preview" if installed["release_id"] == "synthetic-new" else rollback_auth
        modes.append(mode)
        return subprocess.CompletedProcess(command, 0, json.dumps({"auth_mode": mode}), "")
    monkeypatch.setattr(installer, "_probe", diagnostic)
    clock = [0]
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 901))
    message = "update failed and rollback was attempted" if rollback_auth == "local" else "rollback also failed"
    with pytest.raises(RuntimeError, match=message):
        installer._update(installer.Console(color=False), args, root)
    assert modes == ["preview", rollback_auth]
    assert "up-new" in events and "up-old" in events and "seal" not in events
    assert (root / "installation.json").read_bytes() == before[Path("installation.json")]
    assert (root / "compose.env").read_bytes() == before[Path("compose.env")]
    output = capsys.readouterr().out
    assert ("Prior release restored and healthy" in output) is (rollback_auth == "local")


def synthetic_saved_provider(tmp_path, request, monkeypatch, auth, mutation=None):
    from tests.test_first_run_handoff import configured_node
    root, _, paths = configured_node(tmp_path.resolve())
    installation = json.loads((root / "installation.json").read_text())
    installation.update(auth=auth, local_account_management=False)
    (root / "installation.json").write_text(json.dumps(installation))
    environment = installer._dotenv(root / "config/recordbench.env")
    environment["CASE_INTELLIGENCE_AUTH_MODE"] = auth
    if auth == "oidc":
        environment.update({"CASE_INTELLIGENCE_EXTERNAL_ORIGIN": "https://recordbench.example.test:8443",
            "CASE_INTELLIGENCE_OIDC_ISSUER": "https://identity.example.test/realm",
            "CASE_INTELLIGENCE_OIDC_CLIENT_ID": "synthetic-client",
            "CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE": "/run/recordbench-secrets/oidc-client-secret",
            "CASE_INTELLIGENCE_OIDC_ALLOWED_GROUPS": "synthetic-reviewers",
            "CASE_INTELLIGENCE_OIDC_ADMIN_GROUPS": "synthetic-administrators",
            "CASE_INTELLIGENCE_OIDC_SCOPES": "openid profile email"})
        credential = paths["secrets"] / "oidc-client-secret"
        installer._private_write(credential, "synthetic-oidc-secret-value\r\n")
    else:
        environment.update({"CASE_INTELLIGENCE_KERBEROS_REALM": "EXAMPLE.TEST",
            "CASE_INTELLIGENCE_KERBEROS_ALLOWED_GROUPS": "synthetic-users@example.test",
            "CASE_INTELLIGENCE_KERBEROS_ADMIN_GROUPS": "synthetic-admins@example.test",
            "CASE_INTELLIGENCE_KERBEROS_PROXY_SECRET_FILE": "/run/recordbench-secrets/kerberos-proxy-secret"})
        compose = installer._dotenv(root / "compose.env")
        compose["EXCULPATA_KERBEROS_PRINCIPAL"] = "HTTP/recordbench.example.test@EXAMPLE.TEST"
        (root / "compose.env").write_text(installer._env_text(compose, "Synthetic Kerberos node"))
        credential = paths["secrets"] / "kerberos-proxy-secret"
        installer._private_write(credential, "synthetic_proxy_secret_" + "x" * 32 + "\r\n")
        installer._private_write(paths["secrets"] / "recordbench.keytab", "synthetic opaque keytab bytes")
    invalid_options = {
        "issuer": ("CASE_INTELLIGENCE_OIDC_ISSUER", "http://identity.example.test"),
        "client": ("CASE_INTELLIGENCE_OIDC_CLIENT_ID", ""),
        "oidc-groups": ("CASE_INTELLIGENCE_OIDC_ALLOWED_GROUPS", "x" * 257),
        "scopes": ("CASE_INTELLIGENCE_OIDC_SCOPES", "profile email"),
        "claim": ("CASE_INTELLIGENCE_OIDC_GROUPS_CLAIM", "bad claim"),
        "token-auth": ("CASE_INTELLIGENCE_OIDC_TOKEN_AUTH_METHOD", "unsupported"),
        "origin": ("CASE_INTELLIGENCE_EXTERNAL_ORIGIN", "https://wrong.example.test"),
        "oidc-source": ("CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE", "/run/recordbench-secrets/other-secret"),
        "realm": ("CASE_INTELLIGENCE_KERBEROS_REALM", "INVALID"),
        "kerberos-groups": ("CASE_INTELLIGENCE_KERBEROS_ALLOWED_GROUPS", "bad/group"),
        "proxy-source": ("CASE_INTELLIGENCE_KERBEROS_PROXY_SECRET_FILE", "/run/recordbench-secrets/other-secret"),
    }
    if mutation in invalid_options:
        key, value = invalid_options[mutation]
        environment[key] = value
    if mutation == "no-admission":
        environment["CASE_INTELLIGENCE_KERBEROS_ALLOWED_GROUPS"] = ""
        environment["CASE_INTELLIGENCE_KERBEROS_ADMIN_GROUPS"] = ""
    if mutation == "principal":
        installer._replace_env(root / "compose.env", "RECORDBENCH_KERBEROS_PRINCIPAL", "HTTP/other.example.test@EXAMPLE.TEST")
    if mutation and mutation.startswith("keytab-"):
        credential = paths["secrets"] / "recordbench.keytab"
    if mutation and mutation.endswith("-missing") and not mutation.startswith("host-"):
        credential.unlink()
    elif mutation and mutation.endswith("-mode"):
        credential.chmod(0o644)
    elif mutation and mutation.endswith("-empty"):
        credential.write_bytes(b"")
    elif mutation and mutation.endswith("-symlink"):
        target = credential.with_name("synthetic-replacement")
        credential.rename(target)
        credential.symlink_to(target)
    elif mutation == "secret-invalid":
        credential.write_text("synthetic\ninvalid")
    elif mutation == "proxy-invalid":
        credential.write_text("synthetic invalid proxy secret!" * 2)
    (root / "config/recordbench.env").write_text(installer._env_text(environment, "synthetic retained provider configuration"))
    request.getfixturevalue("ready_host")
    original_is_dir, original_is_file = Path.is_dir, Path.is_file
    monkeypatch.setattr(Path, "is_dir", lambda path: mutation != "host-sssd-missing" if path == Path("/var/lib/sss/pipes") else original_is_dir(path))
    monkeypatch.setattr(Path, "is_file", lambda path: mutation != "host-krb-missing" if path == Path("/etc/krb5.conf") else original_is_file(path))
    if mutation and mutation.endswith("-owner"):
        original_lstat = Path.lstat
        def foreign_owner(path):
            metadata = original_lstat(path)
            if path == credential:
                values = list(metadata)
                values[4] = 12345
                return os.stat_result(values)
            return metadata
        monkeypatch.setattr(Path, "lstat", foreign_owner)
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume", "--non-interactive"])
    installer._saved_node_arguments(args, root)
    return root, args


@pytest.mark.parametrize("auth,mutation", [
    *(('oidc', value) for value in ['issuer', 'client', 'oidc-groups', 'scopes', 'claim', 'token-auth', 'origin', 'oidc-source', 'secret-missing', 'secret-empty', 'secret-mode', 'secret-owner', 'secret-symlink', 'secret-invalid']),
    *(('kerberos', value) for value in ['realm', 'kerberos-groups', 'no-admission', 'principal', 'proxy-source', 'proxy-missing', 'proxy-mode', 'proxy-owner', 'proxy-symlink', 'proxy-invalid', 'keytab-missing', 'keytab-empty', 'keytab-mode', 'keytab-owner', 'keytab-symlink', 'host-sssd-missing', 'host-krb-missing']),
])
def test_resume_rejects_invalid_saved_provider_before_provisioning(tmp_path, request, monkeypatch, auth, mutation):
    root, args = synthetic_saved_provider(tmp_path, request, monkeypatch, auth, mutation)
    # New CLI values cannot replace or conceal invalid retained configuration.
    args.oidc_issuer = "https://replacement.example.test"
    args.kerberos_realm = "REPLACEMENT.TEST"
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("invalid saved provider reached an external command"))
    monkeypatch.setattr(installer, "_provision", lambda *a, **k: pytest.fail("invalid saved provider reached provisioning"))
    result = installer._collect_preflight("none", args, needs_model_staging=False)
    assert not result.ready
    with pytest.raises(RuntimeError, match="prerequisites"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert "synthetic-oidc-secret-value" not in json.dumps(result.payload())
    assert "synthetic_proxy_secret_" not in json.dumps(result.payload())


@pytest.mark.parametrize("auth", ["oidc", "kerberos"])
def test_saved_provider_preflight_accepts_valid_canonical_material_without_changes(tmp_path, request, monkeypatch, capsys, auth):
    root, args = synthetic_saved_provider(tmp_path, request, monkeypatch, auth)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    capsys.readouterr()
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("standalone preflight issued a runtime command"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--resume", "--non-interactive", "--json"])
    assert installer.main() == 0
    assert json.loads(capsys.readouterr().out)["ready"]
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_saved_provider_preflight_accepts_explicit_principal_admission(tmp_path, request, monkeypatch):
    root, args = synthetic_saved_provider(tmp_path, request, monkeypatch, "kerberos")
    for key, value in {"CASE_INTELLIGENCE_KERBEROS_ALLOWED_GROUPS": "", "CASE_INTELLIGENCE_KERBEROS_ADMIN_GROUPS": "",
        "CASE_INTELLIGENCE_KERBEROS_ALLOWED_PRINCIPALS": "synthetic.user@EXAMPLE.TEST"}.items():
        installer._replace_env(root / "config/recordbench.env", key, value)
    result = installer._collect_preflight("none", args, needs_model_staging=False)
    assert result.ready
    assert checks_by_name(result)["saved-provider-options"].state == "pass"


@pytest.mark.parametrize("auth", ["oidc", "kerberos"])
def test_saved_provider_malformed_environment_is_a_blocking_json_check(tmp_path, request, monkeypatch, capsys, auth):
    root, _args = synthetic_saved_provider(tmp_path, request, monkeypatch, auth)
    (root / "config/recordbench.env").write_text('INVALID="unterminated\n')
    capsys.readouterr()
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("malformed saved provider reached a runtime command"))
    monkeypatch.setattr(sys, "argv", ["install", "preflight", "--root", str(root), "--resume", "--non-interactive", "--json"])
    assert installer.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert not payload["ready"]
    assert payload["checks"][0]["name"] == "saved-node"


@pytest.mark.parametrize("auth,mutation", [("oidc", "secret-missing"), ("kerberos", "host-sssd-missing")])
def test_saved_provider_dry_run_retains_credential_and_host_checks(tmp_path, request, monkeypatch, auth, mutation):
    _root, args = synthetic_saved_provider(tmp_path, request, monkeypatch, auth, mutation)
    args.dry_run = True
    assert not installer._collect_preflight("none", args, needs_model_staging=False).ready


def test_configuration_uses_the_same_normalized_administrator_as_preflight(tmp_path, ready_host):
    args = preflight_args(tmp_path, "--auth", "local", "--models", "none", "--non-interactive", "--password-stdin",
        "--admin-username", "Synthetic.Admin", "--admin-display-name", "e\u0301" * 160)
    assert installer._collect_preflight("none", args).ready
    paths = installer._paths(args.root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate, key = paths["tls"] / "synthetic.crt", paths["tls"] / "synthetic.key"
    certificate.write_text("synthetic certificate")
    key.write_text("synthetic key")
    args.tls_cert, args.tls_key = certificate, key
    installer._collect_identity_choices(args)
    result = installer._configure(installer.Console(color=False, quiet=True), args, args.root, paths, "synthetic-release", ROOT, ())
    assert result[3:] == ("synthetic.admin", "\u00e9" * 160)
    installation = json.loads((args.root / "installation.json").read_text())
    assert installation["initial_administrator_display_name"] == "\u00e9" * 160


@pytest.mark.parametrize('value', ['', 'short\n', 'x' * 1025 + '\n', 'synthetic-password\x00bad\n'])
@pytest.mark.parametrize('resume', [False, True])
def test_invalid_bootstrap_password_bytes_stop_before_writes(tmp_path, ready_host, monkeypatch, capsys, value, resume):
    import io
    from tests.test_first_run_handoff import configured_node
    root = tmp_path.resolve() / 'uncreated-node'
    flags = ['--models', 'none', '--auth', 'local', '--non-interactive', '--password-stdin']
    if resume:
        root, _, _ = configured_node(tmp_path.resolve())
        flags += ['--resume']
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} if root.exists() else {}
    capsys.readouterr()
    monkeypatch.setattr(sys, 'stdin', io.StringIO(value))
    monkeypatch.setattr(installer, '_prepare_directories', lambda *a, **k: pytest.fail('invalid password reached a write'))
    monkeypatch.setattr(installer, '_run', lambda *a, **k: pytest.fail('invalid password reached provisioning'))
    monkeypatch.setattr(sys, 'argv', ['install', 'install', '--root', str(root), *flags])
    assert installer.main() == 1
    assert 'administrator password' in capsys.readouterr().out
    assert ({p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} if root.exists() else {}) == before


@pytest.mark.parametrize('mutation', ['missing', 'symlink', 'directory', 'invalid', 'outside', 'traversal'])
def test_retained_oidc_ca_is_required_before_resume(tmp_path, request, monkeypatch, mutation):
    root, args = synthetic_saved_provider(tmp_path, request, monkeypatch, 'oidc')
    ca = root / 'secrets/organization-ca.pem'
    if mutation == 'symlink':
        ca.symlink_to(root / 'secrets/oidc-client-secret')
    elif mutation == 'directory':
        ca.mkdir()
    elif mutation == 'invalid':
        ca.write_text('synthetic invalid certificate')
    value = ('/missing/organization-ca.pem' if mutation == 'outside' else
             '/run/recordbench-secrets/../organization-ca.pem' if mutation == 'traversal' else
             '/run/recordbench-secrets/organization-ca.pem')
    installer._replace_env(root / 'config/recordbench.env', 'CASE_INTELLIGENCE_OIDC_CA_FILE', value)
    monkeypatch.setattr(installer, '_run', lambda *a, **k: pytest.fail('invalid CA reached provisioning'))
    with pytest.raises(RuntimeError, match='prerequisites'):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)


def test_retained_oidc_ca_accepts_a_readable_certificate(tmp_path, request, monkeypatch):
    root, args = synthetic_saved_provider(tmp_path, request, monkeypatch, 'oidc')
    ca = root / 'secrets/organization-ca.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=Synthetic CA', '-keyout', str(tmp_path / 'synthetic.key'), '-out', str(ca)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    installer._replace_env(root / 'config/recordbench.env', 'CASE_INTELLIGENCE_OIDC_CA_FILE', '/run/recordbench-secrets/organization-ca.pem')
    assert installer._collect_preflight('none', args, needs_model_staging=False).ready


@pytest.mark.parametrize('management', [False, True])
@pytest.mark.parametrize('seal', ['current', 'older-release', 'invalid'])
def test_resume_refuses_missing_prepared_account_store_before_password_or_commands(tmp_path, request, monkeypatch, management, seal):
    from tests.test_first_run_handoff import configured_node
    root, _, paths = configured_node(tmp_path.resolve())
    installation = json.loads((root / 'installation.json').read_text())
    installation['local_account_management'] = management
    (root / 'installation.json').write_text(json.dumps(installation))
    if not management:
        installer._replace_env(paths['config'] / 'recordbench.env', 'CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE', '/run/recordbench-secrets/local-accounts.json')
        installer._replace_env(paths['config'] / 'recordbench.env', 'CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT', '')
    installer._seal_provisioning(root, installation)
    marker = installer._provision_marker(root)
    if seal == 'older-release':
        value = json.loads(marker.read_text()); value['release_id'] = 'synthetic-prior-release'; marker.write_text(json.dumps(value))
    elif seal == 'invalid':
        marker.write_text('synthetic damaged provisioning seal')
    before = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    request.getfixturevalue('ready_host')
    monkeypatch.setattr(installer, '_password', lambda *a: pytest.fail('missing prepared account store consumed a password'))
    monkeypatch.setattr(installer, '_run', lambda *a, **k: pytest.fail('missing prepared account store issued a command'))
    args = installer._parser().parse_args(['install', '--root', str(root), '--resume', '--non-interactive', '--password-stdin', '--admin-username', 'alice.admin', '--admin-display-name', 'Alice Administrator'])
    with pytest.raises(RuntimeError, match='account store.*missing.*restore'):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('management', [False, True])
@pytest.mark.parametrize('field', ['CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE', 'CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT'])
def test_resume_refuses_inconsistent_application_account_paths_before_commands(tmp_path, request, monkeypatch, management, field):
    from tests.test_first_run_handoff import configured_node
    from case_intelligence.local_accounts import LocalAccountRepository
    root, _, paths = configured_node(tmp_path.resolve())
    installation = json.loads((root / 'installation.json').read_text())
    installation['local_account_management'] = management
    (root / 'installation.json').write_text(json.dumps(installation))
    app = paths['config'] / 'recordbench.env'
    installer._replace_env(app, 'CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE', '/var/lib/recordbench-accounts/local-accounts.json' if management else '/run/recordbench-secrets/local-accounts.json')
    installer._replace_env(app, 'CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT', '/var/lib/recordbench-accounts' if management else '')
    account_root = paths['accounts'] if management else paths['secrets']
    LocalAccountRepository(account_root / 'local-accounts.json').initialize('alice.admin', 'Alice Administrator', 'synthetic-retained-password', actor='synthetic-operator')
    installer._replace_env(app, field, '/synthetic-wrong-account-path')
    before = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    request.getfixturevalue('ready_host')
    monkeypatch.setattr(installer, '_password', lambda *a: pytest.fail('retained account mismatch consumed a password'))
    monkeypatch.setattr(installer, '_run', lambda *a, **k: pytest.fail('retained account mismatch issued a command'))
    args = installer._parser().parse_args(['install', '--root', str(root), '--resume', '--non-interactive'])
    with pytest.raises(RuntimeError, match='saved local.account.*configuration'):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize("entry", [".local-accounts.json.lock", ".local-accounts.json-" + "a" * 32 + ".tmp"])
@pytest.mark.parametrize("fault", ["mode", "owner"])
def test_retained_account_auxiliary_metadata_rejected(tmp_path, request, monkeypatch, entry, fault):
    root, args, account_file = synthetic_saved_account_preflight(tmp_path, request, monkeypatch)
    auxiliary = account_file.with_name(entry)
    auxiliary.touch(mode=0o600)
    if fault == "mode":
        auxiliary.chmod(0o644)
    else:
        original = os.fstat
        target = auxiliary.stat().st_ino
        def metadata(*a, **k):
            value = original(*a, **k)
            if value.st_ino == target:
                fields = list(value)
                fields[4] = 12345
                return os.stat_result(fields)
            return value
        monkeypatch.setattr(os, "fstat", metadata)
    assert installer._saved_account_status(account_file, managed=True) == "invalid"


@pytest.mark.parametrize("problem", ["missing", "unset", "relative", "file", "symlink", "shared", "foreign", "inaccessible"])
@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_preflight_blocks_unusable_compose_home_without_writes(tmp_path, ready_host, monkeypatch, problem, names):
    home = Path(os.environ["HOME"])
    if problem == "missing":
        home.rmdir()
    elif problem == "unset":
        monkeypatch.delenv("HOME")
    elif problem == "relative":
        monkeypatch.setenv("HOME", "relative-home")
    elif problem == "file":
        home.rmdir()
        home.write_text("synthetic")
    elif problem == "symlink":
        link = tmp_path / "home-link"
        link.symlink_to(home, target_is_directory=True)
        monkeypatch.setenv("HOME", str(link))
    elif problem == "shared":
        home.chmod(0o750)
    elif problem == "foreign":
        original = Path.lstat
        def foreign(path):
            result = original(path)
            if path == home:
                fields = list(result)
                fields[4] = 2000
                return os.stat_result(fields)
            return result
        monkeypatch.setattr(Path, "lstat", foreign)
    elif problem == "inaccessible":
        monkeypatch.setattr(os, "access", lambda *a: False)
    args = preflight_args(tmp_path, "--names", names)
    check = next(row for row in installer._collect_preflight("none", args).checks if row.name == "service-home")
    assert check.state == "fail" and check.blocking
    assert "0700" in check.remedy and f"/var/lib/{names}-home" in check.remedy
    assert not args.root.exists()
    assert not (home / ".docker").exists()


def test_preflight_accepts_owner_only_home_without_creating_client_state(tmp_path, ready_host):
    result = installer._collect_preflight("none", preflight_args(tmp_path))
    assert next(row for row in result.checks if row.name == "service-home").state == "pass"
    assert list(Path(os.environ["HOME"]).iterdir()) == []


@pytest.mark.parametrize("driver,exit_code,state", [("vfs", 0, "fail"), ("fuse-overlayfs", 0, "pass"), ("overlay2", 0, "pass"), ("", 1, "fail")])
def test_preflight_storage_driver_note_does_not_block_cpu(tmp_path, ready_host, monkeypatch, driver, exit_code, state):
    original = installer._probe
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, exit_code, driver, "") if "{{.Driver}}" in command else original(command))
    result = installer._collect_preflight("none", preflight_args(tmp_path))
    check = next(row for row in result.checks if row.name == "docker-storage-driver")
    assert check.state == state and not check.blocking
    assert result.ready


@pytest.mark.parametrize("cause", ["reserve", "clamav", "unreachable"])
def test_health_timeout_has_actionable_host_remedy(tmp_path, monkeypatch, cause):
    import io
    from tests.test_first_run_handoff import configured_node
    root, _, _ = configured_node(tmp_path.resolve())
    payload = {"product": "RecordBench", "status": "degraded",
        "storage": {"status": "ready"},
        "capabilities": {"source_review": "ready", "malware_scan": "ready"}}
    if cause == "reserve":
        payload["storage"] = {"status": "blocked", "reserve_satisfied": False}
    elif cause == "clamav":
        payload["capabilities"]["malware_scan"] = "unavailable"
    def probe(*a, **kw):
        if cause == "unreachable":
            raise OSError("synthetic transport failure")
        return io.BytesIO(json.dumps(payload).encode())
    clock = [0]
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer.time, "sleep", lambda seconds: clock.__setitem__(0, 901))
    monkeypatch.setattr(installer.urllib.request, "urlopen", probe)
    monkeypatch.setattr(installer, "_login_reachable", lambda root: True)
    monkeypatch.setattr(installer, "_probe", lambda command: subprocess.CompletedProcess(command, 0, '{"auth_mode": "local"}', ""))
    with pytest.raises(RuntimeError) as error:
        installer._wait_health(installer.Console(color=False, quiet=True), root)
    message = str(error.value)
    assert "startup-troubleshooting" in message and "Docker bridge" in message
    assert "clamav-updater" in message and "100 GiB" in message
    if cause == "reserve":
        assert "storage reserve is unsatisfied" in message
    elif cause == "clamav":
        assert "malware scanning is unavailable" in message
    assert installer._install_progress(root)["phases"]["selected_capabilities"] != "complete"


def test_compose_startup_failure_reports_remedy_before_http_health(tmp_path, monkeypatch):
    from tests.test_first_run_handoff import configured_node
    root, args, paths = configured_node(tmp_path.resolve())
    args.prepare_only = False
    # Provisioning commands are simulated; the account initializer is not exercised.
    (paths["accounts"] / "local-accounts.json").write_text("{}")
    def run(console, command, **kwargs):
        if "up" in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_wait_health", lambda *a: pytest.fail("startup failure reached HTTP health"))
    with pytest.raises(RuntimeError, match="Service startup failed.*clamav-updater.*startup-troubleshooting"):
        installer._provision(installer.Console(color=False, quiet=True), args, root, "local", "none", None, None, password_input=[])
    assert installer._install_progress(root)["phases"]["running"] == "checking"


@pytest.mark.parametrize("problem", ["symlink", "shared", "foreign"])
def test_preflight_blocks_replaceable_home_ancestry(tmp_path, ready_host, monkeypatch, problem):
    parent = tmp_path / "client-parent"
    parent.mkdir(mode=0o700)
    home = parent / "home"
    home.mkdir(mode=0o700)
    if problem == "symlink":
        link = tmp_path / "parent-link"
        link.symlink_to(parent, target_is_directory=True)
        home = link / "home"
    elif problem == "shared":
        parent.chmod(0o777)
    else:
        original = Path.stat
        def foreign(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == parent:
                fields = list(result)
                fields[4] = 2000
                return os.stat_result(fields)
            return result
        monkeypatch.setattr(Path, "stat", foreign)
    monkeypatch.setenv("HOME", str(home))
    result = installer._collect_preflight("none", preflight_args(tmp_path))
    assert not result.ready
    assert checks_by_name(result)["service-home"].state == "fail"
    assert not (home / ".docker").exists()


@pytest.mark.parametrize("running", ["\n", "\n\n"])
def test_prepared_resume_accepts_compose_blank_running_service_output(tmp_path, monkeypatch, request, running):
    root, args, events, commands, _before = synthetic_late_gpu_resume(
        tmp_path, monkeypatch, request, running=running, live_free=47000)
    installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert "stop" not in events and "start" not in events
    assert events.count("probe-live") == 2
    assert events.index("ps") < events.index("build") < events.index("up") < events.index("health")


@pytest.mark.parametrize("running", ["\ninvalid service\n", "\n--all\n"])
def test_prepared_resume_still_rejects_malformed_service_names(tmp_path, monkeypatch, request, running):
    root, args, events, _commands, _before = synthetic_late_gpu_resume(
        tmp_path, monkeypatch, request, running=running, live_free=47000)
    with pytest.raises(RuntimeError, match="could not safely identify"):
        installer._resume_node(installer.Console(color=False, quiet=True), args, root)
    assert all(event not in events for event in ("stop", "start", "build", "up"))


@pytest.mark.parametrize("models", ["none", "review", "transcription", "all"])
def test_initial_build_includes_managed_companion_security_images(tmp_path, monkeypatch, models):
    from tests.test_first_run_handoff import configured_node
    root, args, _ = configured_node(tmp_path)
    args.dry_run = True
    commands = []
    def capture(console, command, **kwargs):
        commands.append(command)
        if "build" in command:
            raise RuntimeError("synthetic stop before image build")
    monkeypatch.setattr(installer, "_run", capture)
    with pytest.raises(RuntimeError, match="synthetic stop"):
        installer._provision(installer.Console(color=False, quiet=True), args, root,
            "local", models, "alice.admin", "Alice Administrator",
            password_input=["synthetic-unused-password"])
    build = next(command for command in commands if "build" in command)
    targets = set(build[build.index("build") + 1:])
    assert {"gateway", "clamav", "postgres", "app", "account-admin"} <= targets
    assert ("retrieval" in targets) is (models in {"review", "all"})
    assert ("transcription-worker" in targets) is (models in {"transcription", "all"})


def test_update_builds_companion_images_before_node_swap(tmp_path, monkeypatch, request):
    root, args, events, _ = synthetic_live_update(tmp_path, monkeypatch, request)
    original_run = installer._run
    builds = []
    def capture(console, command, **kwargs):
        if "build" in command:
            builds.append(command[command.index("build") + 1:])
        return original_run(console, command, **kwargs)
    monkeypatch.setattr(installer, "_run", capture)
    installer._update(installer.Console(color=False, quiet=True), args, root)
    assert len(builds) == 1
    assert {"gateway", "clamav", "postgres"} <= set(builds[0])
    assert events.index("build-new") < events.index("stop-old") < events.index("up-new")


@pytest.mark.parametrize("prefixes", [("RECORDBENCH",), ("EXCULPATA",), ("RECORDBENCH", "EXCULPATA")])
@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_saved_alias_updates_preserve_keys_and_unchanged_bytes(tmp_path, prefixes, ending):
    path = tmp_path / "compose.env"
    original = "# Synthetic operator formatting\r\n\r\n" + "\r\n".join(
        f"  {prefix}_HTTPS_PORT= 8443  " for prefix in prefixes) + ending
    # Keys are strict dotenv identifiers, while indentation and value spacing
    # are accepted operator formatting and should survive a no-op update.
    path.write_bytes(original.encode())
    path.chmod(0o600)
    for name in ("RECORDBENCH_HTTPS_PORT", "EXCULPATA_HTTPS_PORT"):
        installer._replace_env(path, name, "8443")
        assert path.read_bytes() == original.encode()
    installer._replace_env(path, "RECORDBENCH_ABSENT", "synthetic")
    assert path.read_bytes() == original.encode()
    installer._replace_env(path, "RECORDBENCH_HTTPS_PORT", "9443")
    expected = original
    for prefix in prefixes:
        expected = expected.replace(f"  {prefix}_HTTPS_PORT= 8443  ", f'{prefix}_HTTPS_PORT="9443"')
    assert path.read_bytes() == expected.encode()
    assert installer._dotenv(path) == {f"{prefix}_HTTPS_PORT": "9443" for prefix in prefixes}


def test_configuration_writes_new_names_only_and_keeps_existing_old_file_byte_identical(tmp_path):
    from tests.test_first_run_handoff import configured_node
    root, args, paths = configured_node(tmp_path, names="exculpata")
    path = root / "compose.env"
    fresh = path.read_bytes()
    assert b"EXCULPATA_" in fresh
    assert b"RECORDBENCH_" not in fresh
    old = b"# Synthetic operator note\r\n" + fresh.replace(b"EXCULPATA_", b"RECORDBENCH_").replace(b"\n", b"\r\n")
    path.write_bytes(old)
    installer._configure(installer.Console(color=False, quiet=True), args, root, paths,
                         "synthetic-release", ROOT, ())
    assert path.read_bytes() == old


@pytest.mark.parametrize("same_release", [True, False])
def test_update_preserves_old_only_compose_settings(tmp_path, monkeypatch, request, same_release):
    root, args, events, _ = synthetic_live_update(tmp_path, monkeypatch, request)
    path = root / "compose.env"
    settings = installer._dotenv(path)
    old = {key.replace("EXCULPATA_", "RECORDBENCH_", 1): value
           for key, value in settings.items()}
    original = installer._env_text(old, "Synthetic old-only node").encode()
    path.write_bytes(original)
    if same_release:
        installation = json.loads((root / "installation.json").read_text())
        monkeypatch.setattr(installer, "_stage_release", lambda *a, **k:
                            (installation["release_id"], Path(installation["release_path"])))
        monkeypatch.setattr(installer, "_doctor", lambda *a, **k: events.append("doctor"))
    installer._update(installer.Console(color=False, quiet=True), args, root)
    if same_release:
        assert path.read_bytes() == original
        assert "doctor" in events
    else:
        # A new capsule changes the existing release coordinate, never the
        # spelling, order, or value of any other saved setting.
        assert path.read_bytes() == original.replace(
            b'RECORDBENCH_RELEASE_ID="synthetic-release"',
            b'RECORDBENCH_RELEASE_ID="synthetic-new"')
        assert "up-new" in events
    assert b"EXCULPATA_" not in path.read_bytes()


@pytest.mark.parametrize("prefix", ["RECORDBENCH", "EXCULPATA"])
@pytest.mark.parametrize("final_newline", [True, False])
def test_reconfigure_adds_kerberos_setting_using_saved_naming_style(tmp_path, monkeypatch, prefix, final_newline):
    from tests.test_first_run_handoff import configured_node
    root, args, paths = configured_node(tmp_path, names="exculpata")
    path = root / "compose.env"
    before = path.read_bytes().replace(b"EXCULPATA_", prefix.encode() + b"_")
    if not final_newline:
        before = before.removesuffix(b"\n")
    path.write_bytes(before)
    assert b"KERBEROS_PRINCIPAL" not in before
    assert json.loads((root / "installation.json").read_text())["auth"] == "local"
    args.auth = "kerberos"
    args.kerberos_realm = "EXAMPLE.TEST"
    args.kerberos_allowed_groups = "synthetic-reviewers@example.test"
    args.kerberos_admin_groups = "synthetic-administrators@example.test"
    installer._private_write(paths["secrets"] / "recordbench.keytab", "synthetic opaque keytab bytes")
    original_is_dir, original_is_file = Path.is_dir, Path.is_file
    monkeypatch.setattr(Path, "is_dir", lambda value: True if value == Path("/var/lib/sss/pipes") else original_is_dir(value))
    monkeypatch.setattr(Path, "is_file", lambda value: True if value == Path("/etc/krb5.conf") else original_is_file(value))

    installer._configure(installer.Console(color=False, quiet=True), args, root, paths,
                         "synthetic-release", ROOT, ())

    principal = "HTTP/recordbench.example.test@EXAMPLE.TEST"
    added = f"{prefix}_KERBEROS_PRINCIPAL={json.dumps(principal)}\n".encode()
    expected = before + (b"" if final_newline else b"\n") + added
    assert path.read_bytes() == expected
    other = "EXCULPATA" if prefix == "RECORDBENCH" else "RECORDBENCH"
    assert not any(key.startswith(other + "_") for key in installer._dotenv(path))
    assert installer._dotenv(path)[f"{prefix}_KERBEROS_PRINCIPAL"] == principal
    assert json.loads((root / "installation.json").read_text())["auth"] == "kerberos"
    # Repeating the same reconfiguration must not duplicate or reformat keys.
    installer._configure(installer.Console(color=False, quiet=True), args, root, paths,
                         "synthetic-release", ROOT, ())
    assert path.read_bytes() == expected
