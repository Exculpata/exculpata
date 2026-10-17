"""Synthetic naming-generation compatibility, without installing a host node."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    CURRENT_ENV_PREFIX,
    CURRENT_PRODUCT,
    CURRENT_STEM,
    LEGACY_ENV_PREFIX,
    LEGACY_PRODUCT,
    LEGACY_STEM,
)

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_oss_installer import installer, ROOT
from tests.test_first_run_handoff import configured_node


@pytest.fixture(autouse=True)
def no_host_account(monkeypatch):
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)


@pytest.mark.parametrize("auth", ["local", "oidc", "kerberos"])
@pytest.mark.parametrize("models", ["none", "review", "transcription", "all"])
def test_install_and_update_build_proxy_without_changing_model_selection(auth, models):
    expected = {"app", "account-admin", "gateway", "clamav", "postgres", "egress-proxy"}
    if auth == "kerberos":
        expected.add("kerberos-proxy")
    if models in {"review", "all"}:
        expected.add("retrieval")
    if models in {"transcription", "all"}:
        expected.update({"transcription-api", "transcription-worker"})
    assert set(installer._build_targets(auth, models)) == expected


@pytest.mark.parametrize("names", [LEGACY_STEM, CURRENT_STEM])
@pytest.mark.parametrize("auth", ["oidc", "local", "kerberos"])
def test_egress_issuer_uses_selected_app_file_only(tmp_path, names, auth):
    root, _, paths = configured_node(tmp_path, names=names)
    selected = paths["config"] / "custom.env"
    selected.write_text(
        f"CASE_INTELLIGENCE_AUTH_MODE={auth}\n"
        "CASE_INTELLIGENCE_OIDC_ISSUER=https://issuer.example.test:8443/realm\n"
        "CASE_INTELLIGENCE_OIDC_CA_FILE=/run/example/ca.pem\n"
        "CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE=/run/example/secret\n"
    )
    installer._replace_env(root / "compose.env", "EXCULPATA_APP_ENV_FILE", "custom.env", add_missing_aliases=True)
    command = installer._compose(root, (), release=ROOT, auth=auth)
    environment = installer._compose_environment(command, {
        "PATH": os.environ["PATH"], "OIDC_EGRESS_ISSUER": "https://unrelated.example.test",
    })
    assert environment["OIDC_EGRESS_ISSUER"] == (
        "https://issuer.example.test:8443/realm" if auth == "oidc" else ""
    )
    result = subprocess.run([*command, "config", "--format", "json"],
                            env=environment, capture_output=True, text=True, check=True)
    graph = json.loads(result.stdout)
    proxy = graph["services"]["egress-proxy"]
    assert proxy["environment"] == {"OIDC_ISSUER": environment["OIDC_EGRESS_ISSUER"]}
    assert not any(key in proxy for key in ("volumes", "secrets", "ports"))
    assert graph["services"]["app"]["environment"]["CASE_INTELLIGENCE_OIDC_CA_FILE"] == "/run/example/ca.pem"


def test_fresh_defaults_and_generated_coordinates(tmp_path):
    args = installer._parser().parse_args([])
    assert installer._select_names(args, tmp_path / "fresh") == "exculpata"
    assert installer._default_root() == Path("/srv/exculpata")
    root, _, paths = configured_node(tmp_path, names="exculpata")
    env = installer._dotenv(root / "compose.env")
    expected = f"exculpata-{installer.hashlib_short(root)}"
    assert env["EXCULPATA_COMPOSE_PROJECT"] == env["EXCULPATA_IMAGE_NAMESPACE"] == expected
    assert env["EXCULPATA_APP_ENV_FILE"] == "exculpata.env"
    assert env["EXCULPATA_STORAGE_NAMES"] == "exculpata"
    assert not any(key.startswith(f"{LEGACY_ENV_PREFIX}") for key in env)
    assert (paths["config"] / "exculpata.env").is_file()
    assert not (paths["config"] / f"{LEGACY_STEM}.env").exists()
    assert f"dbname={LEGACY_STEM} user={LEGACY_STEM}" in (paths["secrets"] / "postgres-dsn").read_text()
    assert installer._node_names(root) == "exculpata"


@pytest.mark.parametrize("signal", ["config", "marker", "external-marker", "account"])
def test_legacy_signals_keep_names_and_refuse_migration(tmp_path, monkeypatch, signal):
    root = tmp_path / "legacy"
    storage = tmp_path / "storage"
    args = installer._parser().parse_args(["--root", str(root), "--storage-root", str(storage)])
    if signal == "config":
        (root / "config").mkdir(parents=True)
        (root / f"config/{LEGACY_STEM}.env").write_text("# Synthetic retained configuration\n")
    elif signal in {"marker", "external-marker"}:
        target = root if signal == "marker" else storage
        target.mkdir()
        (target / f".{LEGACY_STEM}-managed-storage.json").write_text("{}")
    else:
        monkeypatch.setattr(installer, "_legacy_account", lambda: True)
        assert installer._default_root() == Path(f"/srv/{LEGACY_STEM}")
    assert installer._select_names(args, root) == f"{LEGACY_STEM}"
    args.names = "exculpata"
    with pytest.raises(installer.NamingError, match="migration.*not implemented"):
        installer._select_names(args, root)


@pytest.mark.parametrize("command", ["install", "preflight", "doctor", "update", "backup", "restore"])
def test_explicit_rename_is_refused_before_any_write(tmp_path, monkeypatch, capsys, command):
    root = tmp_path / "legacy"
    (root / "config").mkdir(parents=True)
    retained = root / f"config/{LEGACY_STEM}.env"
    retained.write_bytes(b"# Synthetic operator formatting\r\n")
    monkeypatch.setattr(sys, "argv", ["install", command, "--root", str(root), "--names", "exculpata", "--non-interactive"])
    monkeypatch.setattr(installer, "_run", lambda *a, **k: pytest.fail("external command before rename refusal"))
    assert installer.main() == 1
    assert "migration, which is not implemented" in capsys.readouterr().out
    assert retained.read_bytes() == b"# Synthetic operator formatting\r\n"
    assert [p.relative_to(root) for p in root.rglob("*") if p.is_file()] == [Path(f"config/{LEGACY_STEM}.env")]


def test_legacy_reconfiguration_preserves_all_config_bytes_and_namespace(tmp_path):
    root, args, paths = configured_node(tmp_path)
    compose = root / "compose.env"
    installer._replace_env(compose, f"{LEGACY_ENV_PREFIX}COMPOSE_PROJECT", "synthetic-original-project")
    installer._replace_env(compose, f"{LEGACY_ENV_PREFIX}IMAGE_NAMESPACE", "synthetic-original-images")
    controls = [compose, paths["config"] / f"{LEGACY_STEM}.env", paths["config"] / "transcription.env"]
    for path in controls:
        path.write_bytes(b"# Synthetic operator note\r\n" + path.read_bytes().replace(b"\n", b"\r\n"))
    before = {path: path.read_bytes() for path in controls}
    installer._configure(installer.Console(color=False, quiet=True), args, root, paths, "synthetic-release", ROOT, ())
    assert {path: path.read_bytes() for path in controls} == before
    assert all(b"EXCULPATA_" not in content for content in before.values())
    assert not (paths["config"] / "exculpata.env").exists()


@pytest.mark.parametrize("names", [f"{LEGACY_STEM}", "exculpata"])
def test_doctor_and_scheduled_units_follow_installed_generation(tmp_path, monkeypatch, capsys, names):
    root, args, _ = configured_node(tmp_path, names=names)
    args.dry_run = True
    monkeypatch.setattr(installer, "_run", lambda *a, **k: None)
    installer._doctor(installer.Console(color=False), args, root)
    assert f"naming generation :: {names}" in capsys.readouterr().out
    home = tmp_path / "synthetic-home"
    monkeypatch.setattr(Path, "home", lambda: home)
    installer._schedule_backup(installer.Console(color=False), root, dry_run=False)
    record = json.loads((root / "state/backup-unit.json").read_text())
    assert record["service"] == f"{names}-backup-{installer.hashlib_short(root)}.service"
    units = home / ".config/systemd/user"
    assert (units / record["service"]).is_file()
    assert f"Unit={record['service']}" in (units / record["timer"]).read_text()
    other = "exculpata" if names == f"{LEGACY_STEM}" else f"{LEGACY_STEM}"
    assert not list(units.glob(f"{other}-*"))


@pytest.mark.parametrize("names", [f"{LEGACY_STEM}", "exculpata"])
def test_shell_cannot_override_saved_naming_coordinates(tmp_path, names):
    root, _, _ = configured_node(tmp_path, names=names)
    command = installer._compose(root, (), release=ROOT, auth="local")
    env = installer._compose_environment(command, {
        "EXCULPATA_IMAGE_NAMESPACE": "synthetic-unrelated-images",
        f"{LEGACY_ENV_PREFIX}IMAGE_NAMESPACE": "synthetic-other-images",
        "EXCULPATA_APP_ENV_FILE": "unrelated.env",
        "EXCULPATA_STORAGE_NAMES": "unrelated",
        "COMPOSE_PROJECT_NAME": "synthetic-unrelated-project",
    })
    assert env["EXCULPATA_IMAGE_NAMESPACE"] == f"{names}-{installer.hashlib_short(root)}"
    assert env["EXCULPATA_COMPOSE_PROJECT"] == f"{names}-{installer.hashlib_short(root)}"
    assert "COMPOSE_PROJECT_NAME" not in env
    assert env["EXCULPATA_APP_ENV_FILE"] == f"{names}.env"


@pytest.mark.parametrize("names", [f"{LEGACY_STEM}", "exculpata"])
def test_compose_renders_saved_names_database_and_marker_mode(tmp_path, names):
    root, _, _ = configured_node(tmp_path, names=names)
    command = installer._compose(root, ("tools",), release=ROOT, auth="local")
    result = subprocess.run([*command, "config", "--format", "json"], env=installer._compose_environment(command), capture_output=True, text=True, check=True)
    graph = json.loads(result.stdout)
    expected = f"{names}-{installer.hashlib_short(root)}"
    assert graph["name"] == expected
    assert graph["services"]["app"]["image"].startswith(expected + "/")
    assert graph["services"]["postgres"]["environment"]["POSTGRES_DB"] == f"{LEGACY_STEM}"
    assert graph["services"]["postgres"]["environment"]["POSTGRES_USER"] == f"{LEGACY_STEM}"
    assert graph["services"]["account-admin"]["environment"]["EXCULPATA_STORAGE_NAMES"] == names


def test_legacy_installer_import_and_both_entrypoints():
    import importlib
    import exculpata_install
    legacy = importlib.import_module(f"{LEGACY_STEM}_install")
    assert exculpata_install is legacy
    for script in ("exculpata_install.py", f"{LEGACY_STEM}_install.py"):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"], capture_output=True, text=True)
        assert result.returncode == 0 and "--names" in result.stdout


def test_old_missing_project_defaults_remain_old_without_editing_env(tmp_path):
    root, _, _ = configured_node(tmp_path)
    path = root / "compose.env"
    original = b"".join(line for line in path.read_bytes().splitlines(keepends=True)
                        if not line.startswith(((f"{LEGACY_ENV_PREFIX}COMPOSE_PROJECT=").encode(), (f"{LEGACY_ENV_PREFIX}IMAGE_NAMESPACE=").encode())))
    path.write_bytes(original)
    env = installer._compose_environment(installer._compose(root, ()), {
        "EXCULPATA_STORAGE_ROOT": "synthetic-unrelated-storage",
        f"{LEGACY_ENV_PREFIX}STORAGE_ROOT": "synthetic-other-storage",
    })
    assert env["EXCULPATA_IMAGE_NAMESPACE"] == f"{LEGACY_STEM}"
    assert env["EXCULPATA_COMPOSE_PROJECT"] == f"{LEGACY_STEM}"
    assert "EXCULPATA_STORAGE_ROOT" not in env and f"{LEGACY_ENV_PREFIX}STORAGE_ROOT" not in env
    assert path.read_bytes() == original


@pytest.mark.parametrize("names", [f"{LEGACY_STEM}", "exculpata"])
def test_update_retains_generation_and_config_except_release(tmp_path, monkeypatch, names):
    root, _, paths = configured_node(tmp_path, names=names)
    controls = [root / "compose.env", paths["config"] / f"{names}.env", paths["config"] / "transcription.env"]
    before = {path: path.read_bytes() for path in controls}
    calls = []
    monkeypatch.setattr(installer, "_preflight", lambda *a, **k: ())
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **k: ("synthetic-next", ROOT))
    monkeypatch.setattr(installer, "_run", lambda console, command, **k: calls.append(command))
    monkeypatch.setattr(installer, "_wait_health", lambda *a: None)
    monkeypatch.setattr(installer, "_seal_provisioning", lambda *a: None)
    args = installer._parser().parse_args(["update", "--root", str(root), "--no-backup", "--non-interactive"])
    installer._update(installer.Console(color=False, quiet=True), args, root)
    for path in controls:
        expected = before[path]
        if path.name == "compose.env":
            expected = expected.replace(b'RELEASE_ID="synthetic-release"', b'RELEASE_ID="synthetic-next"')
        assert path.read_bytes() == expected
    assert installer._node_names(root) == names
    assert any("up" in call for call in calls)
    assert not (paths["config"] / ("exculpata.env" if names == f"{LEGACY_STEM}" else f"{LEGACY_STEM}.env")).exists()


@pytest.mark.parametrize("names", [f"{LEGACY_STEM}", "exculpata"])
def test_installation_metadata_detects_generation_without_other_markers(tmp_path, monkeypatch, names):
    record = tmp_path / "installation.json"
    original = json.dumps({"names": names}).encode()
    record.write_bytes(original)
    # A host account is only a fallback, not a conflicting node marker.
    monkeypatch.setattr(installer, "_legacy_account", lambda: True)
    assert installer._detected_names(tmp_path) == names
    assert record.read_bytes() == original


@pytest.mark.parametrize("names", [f"{LEGACY_STEM}", "exculpata"])
@pytest.mark.parametrize("signal", ["config", "marker", "external-marker"])
@pytest.mark.parametrize("matches", [True, False])
def test_installation_metadata_must_agree_with_markers(tmp_path, names, signal, matches):
    root = tmp_path / "node"
    root.mkdir()
    record = root / "installation.json"
    record.write_text(json.dumps({"names": names}))
    marker_names = names if matches else (f"{LEGACY_STEM}" if names == "exculpata" else "exculpata")
    storage = tmp_path / "storage"
    if signal == "config":
        (root / "config").mkdir()
        marker = root / "config" / f"{marker_names}.env"
    else:
        directory = root if signal == "marker" else storage
        directory.mkdir(exist_ok=True)
        marker = directory / f".{marker_names}-managed-storage.json"
    marker.write_text("{}")
    before = {p: p.read_bytes() for p in (record, marker)}
    if matches or signal == "config":
        # Application filenames select configuration, not a storage migration.
        assert installer._detected_names(root, storage) == names
    else:
        with pytest.raises(installer.NamingError, match="Conflicting naming"):
            installer._detected_names(root, storage)
    assert {p: p.read_bytes() for p in before} == before


def test_installation_without_names_keeps_legacy_fallback(tmp_path):
    (tmp_path / "installation.json").write_text("{}")
    assert installer._detected_names(tmp_path) == f"{LEGACY_STEM}"


@pytest.mark.parametrize("content", ['{"names": "unknown"}', '{"names": null}',
                                     '{"names": []}', '[]', 'invalid JSON'])
def test_invalid_installation_naming_record_is_refused(tmp_path, content):
    (tmp_path / "installation.json").write_text(content)
    with pytest.raises(installer.NamingError, match="invalid"):
        installer._detected_names(tmp_path)


def test_installation_naming_record_symlink_is_refused(tmp_path):
    target = tmp_path / "synthetic-record.json"
    target.write_text('{"names": "exculpata"}')
    (tmp_path / "installation.json").symlink_to(target)
    with pytest.raises(installer.NamingError, match="symbolic link"):
        installer._detected_names(tmp_path)


@pytest.mark.parametrize("overlay", [None, "compose.kerberos.yaml"])
def test_compose_without_naming_environment_uses_current_defaults(tmp_path, overlay):
    # The full graph requires mount paths and application env files. Supply only
    # those synthetic prerequisites; neither naming family nor a .env file may
    # supply a project, image namespace, or release default. An unset image
    # namespace is the current name. An unset project name stays the previous
    # stem so a compose.env without a project variable keeps existing volumes.
    # A saved node still injects its stored project.
    root, _, _ = configured_node(tmp_path)
    saved = installer._dotenv(root / "compose.env")
    paths = {key: value for key, value in saved.items()
             if key.endswith("_ROOT") or key in {f"{LEGACY_ENV_PREFIX}TLS_CERT", f"{LEGACY_ENV_PREFIX}TLS_KEY"}}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("EXCULPATA_", f"{LEGACY_ENV_PREFIX}", "COMPOSE_"))}
    env.update(paths)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    command = ["docker", "compose", "--env-file", str(empty_env), "--profile", "*",
               "-f", str(ROOT / "compose.yaml")]
    if overlay:
        command += ["-f", str(ROOT / overlay)]
    result = subprocess.run([*command, "config", "--format", "json"], env=env,
                            capture_output=True, text=True, check=True)
    graph = json.loads(result.stdout)
    assert graph["name"] == f"{LEGACY_STEM}"
    for service in graph["services"].values():
        if "build" in service:
            assert service["image"].startswith("exculpata/")


@pytest.mark.parametrize("overlay", [None, "compose.kerberos.yaml"])
def test_compose_naming_fields_render_with_no_environment(tmp_path, overlay):
    # Render the naming expressions with literally no environment.
    # Mounts/env files are exercised by the full-graph regression above.
    sources = [(ROOT / "compose.yaml").read_text()]
    if overlay:
        sources.append((ROOT / overlay).read_text())
    project = sources[0].splitlines()[0]
    images = [image for source in sources
              for image in re.findall(r"^    image: (.+)$", source, re.MULTILINE)
              if "IMAGE_NAMESPACE" in image]
    assert images
    config = project + "\nservices:\n" + "".join(
        f"  synthetic-{index}:\n    image: {image}\n" for index, image in enumerate(images)
    )
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    result = subprocess.run(
        [shutil.which("docker"), "compose", "--env-file", str(empty_env), "--project-directory", str(tmp_path),
         "-f", "-", "config", "--format", "json"],
        input=config, env={}, capture_output=True, text=True, check=True,
    )
    graph = json.loads(result.stdout)
    assert graph["name"] == f"{LEGACY_STEM}"
    assert len(graph["services"]) == len(images)
    assert all(service["image"].startswith("exculpata/") for service in graph["services"].values())


def test_compose_project_expression_defaults_to_previous_stem():
    expected = (
        "name: ${" + CURRENT_ENV_PREFIX + "COMPOSE_PROJECT:-${"
        + LEGACY_ENV_PREFIX + "COMPOSE_PROJECT:-" + f"{LEGACY_STEM}" + "}}"
    )
    assert (ROOT / "compose.yaml").read_text().splitlines()[0] == expected


@pytest.mark.parametrize("names,host", [
    ("exculpata", f"{CURRENT_STEM}.example.test"),
    (f"{LEGACY_STEM}", f"{LEGACY_STEM}.example.test"),
    (None, f"{CURRENT_STEM}.example.test"),
])
def test_hostname_default_follows_selected_generation(names, host):
    assert installer._default_server_name(names) == host
    product = CURRENT_PRODUCT if host.startswith(f"{CURRENT_STEM}.") else LEGACY_PRODUCT
    assert installer._server_name_prompt(names) == f"{product} hostname"


@pytest.mark.parametrize("names,host", [
    ("exculpata", f"{CURRENT_STEM}.example.test"),
    (f"{LEGACY_STEM}", f"{LEGACY_STEM}.example.test"),
])
def test_prepared_node_hostname_follows_generation(tmp_path, names, host):
    root, _, _ = configured_node(tmp_path, names=names)
    record = json.loads((root / "installation.json").read_text())
    assert record["server_name"] == host
    assert installer._dotenv(root / "compose.env")[f"{CURRENT_ENV_PREFIX}SERVER_NAME" if names == "exculpata" else f"{LEGACY_ENV_PREFIX}SERVER_NAME"] == host


def test_resume_keeps_saved_hostname_when_server_name_is_omitted(tmp_path):
    root, _, _ = configured_node(tmp_path, names="exculpata")
    saved = json.loads((root / "installation.json").read_text())["server_name"]
    assert saved == f"{CURRENT_STEM}.example.test"
    args = installer._parser().parse_args(["install", "--root", str(root), "--resume", "--non-interactive"])
    installation, _release = installer._saved_node_arguments(args, root)
    assert args.server_name == saved == installation["server_name"]


@pytest.mark.parametrize("files,explicit,legacy_explicit,expected", [
    (["legacy"], None, None, "legacy"),
    (["current"], None, None, "current"),
    (["legacy", "current"], None, None, "current"),
    (["legacy", "current", "custom"], "custom", None, "custom"),
    (["legacy", "current", "custom"], "custom", "legacy", "custom"),
    (["legacy", "current"], None, "legacy", "legacy"),
    (["legacy"], "current", None, "current"),
    ([], None, None, "legacy"),
])
def test_compose_selects_one_saved_application_file(tmp_path, files, explicit, legacy_explicit, expected):
    root, _, paths = configured_node(tmp_path)
    filenames = {"legacy": f"{LEGACY_STEM}.env", "current": "exculpata.env", "custom": "custom.env"}
    for path in paths["config"].glob("*.env"):
        if path.name != "transcription.env":
            path.unlink()
    for name in files:
        (paths["config"] / filenames[name]).write_text(f"SYNTHETIC_SELECTED={name}\nSYNTHETIC_{name.upper()}_ONLY=1\n")
    saved = root / "compose.env"
    if explicit:
        saved.write_text(saved.read_text() + f"EXCULPATA_APP_ENV_FILE={filenames[explicit]}\n")
    if legacy_explicit:
        saved.write_text(saved.read_text() + f"{LEGACY_ENV_PREFIX}APP_ENV_FILE={filenames[legacy_explicit]}\n")
    before = saved.read_bytes()
    command = installer._compose(root, (), release=ROOT, auth="local")
    environment = installer._compose_environment(command, {"PATH": os.environ["PATH"], "EXCULPATA_APP_ENV_FILE": "unrelated.env"})
    assert environment["EXCULPATA_APP_ENV_FILE"] == filenames[expected]
    result = subprocess.run([*command, "config", "--format", "json"], env=environment, capture_output=True, text=True)
    assert saved.read_bytes() == before
    if expected not in files:
        assert result.returncode != 0  # A missing explicit file must not silently fall back.
        return
    assert result.returncode == 0, result.stderr
    app = json.loads(result.stdout)["services"]["app"]["environment"]
    assert app["SYNTHETIC_SELECTED"] == expected
    for other in set(files) - {expected}:
        assert f"SYNTHETIC_{other.upper()}_ONLY" not in app


@pytest.mark.parametrize("names", [LEGACY_STEM, CURRENT_STEM])
def test_custom_app_file_is_shared_by_validation_doctor_and_rollback(tmp_path, monkeypatch, names):
    root, _, paths = configured_node(tmp_path, names=names)
    default = paths["config"] / f"{names}.env"
    selected = paths["config"] / "custom.env"
    selected.write_bytes(default.read_bytes())
    installer._replace_env(root / "compose.env", "EXCULPATA_APP_ENV_FILE", "custom.env", add_missing_aliases=True)
    assert installer._app_env(root) == selected
    default_before = default.read_bytes()
    selected_before = selected.read_bytes()
    args = installer._parser().parse_args(["update", "--root", str(root), "--no-backup", "--non-interactive"])
    installer._replace_env(selected, "CASE_INTELLIGENCE_AUTH_MODE", "preview")
    with pytest.raises(RuntimeError, match="authentication mode disagrees"):
        installer._saved_node_arguments(args, root)
    with pytest.raises(RuntimeError, match="authentication mode disagrees"):
        installer._doctor(installer.Console(color=False, quiet=True), args, root)
    selected.write_bytes(selected_before)
    monkeypatch.setattr(installer, "_preflight", lambda *a, **k: ())
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **k: ("synthetic-next", ROOT))
    launches = []
    def run(console, command, **kwargs):
        if "up" in command:
            assert installer._compose_environment(command)["EXCULPATA_APP_ENV_FILE"] == selected.name
            launches.append(selected.read_bytes())
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(installer, "_run", run)
    health_calls = []
    def health(*args):
        health_calls.append(True)
        if len(health_calls) == 1:
            selected.write_text("# Synthetic failed startup mutation\n")
            raise RuntimeError("synthetic startup failure")
        assert selected.read_bytes() == selected_before
    monkeypatch.setattr(installer, "_wait_health", health)
    with pytest.raises(RuntimeError, match="rollback was attempted"):
        installer._update(installer.Console(color=False, quiet=True), args, root)
    assert len(health_calls) == len(launches) == 2
    assert selected.read_bytes() == selected_before
    assert default.read_bytes() == default_before


@pytest.mark.parametrize("names", [LEGACY_STEM, CURRENT_STEM])
def test_reconfigure_preserves_custom_application_selection(tmp_path, names):
    root, args, paths = configured_node(tmp_path, names=names)
    default = paths["config"] / f"{names}.env"
    selected = paths["config"] / "custom.env"
    default.rename(selected)
    installer._replace_env(root / "compose.env", "EXCULPATA_APP_ENV_FILE", selected.name, add_missing_aliases=True)
    installer._configure(installer.Console(color=False, quiet=True), args, root, paths, "synthetic-release", ROOT, ())
    assert installer._app_env(root) == selected
    assert not default.exists()
    assert installer._compose_environment(installer._compose(root, ()))["EXCULPATA_APP_ENV_FILE"] == selected.name


@pytest.mark.parametrize("filename", ["../outside.env", "/outside.env", ".", "..", "subdir/custom.env", "subdir\\custom.env"])
def test_app_selection_cannot_escape_config_root(tmp_path, filename):
    root, _, _ = configured_node(tmp_path)
    installer._replace_env(root / "compose.env", "EXCULPATA_APP_ENV_FILE", filename, add_missing_aliases=True)
    with pytest.raises(RuntimeError, match="filename inside the config root"):
        installer._app_env(root)
    with pytest.raises(RuntimeError, match="filename inside the config root"):
        installer._compose_environment(installer._compose(root, ()))


@pytest.mark.parametrize("names", [LEGACY_STEM, CURRENT_STEM])
@pytest.mark.parametrize("selection", [None, "legacy", "current", "custom"])
@pytest.mark.parametrize("command", ["doctor", "update"])
def test_cli_with_both_app_files_uses_selection_without_renaming_node(tmp_path, monkeypatch, names, selection, command):
    root, _, paths = configured_node(tmp_path, names=names)
    filenames = {"legacy": f"{LEGACY_STEM}.env", "current": f"{CURRENT_STEM}.env", "custom": "custom.env"}
    original = (paths["config"] / f"{names}.env").read_bytes()
    selected = filenames[selection or "current"]
    for filename in filenames.values():
        path = paths["config"] / filename
        path.write_bytes(original)
        if filename != selected:
            installer._replace_env(path, "CASE_INTELLIGENCE_AUTH_MODE", "preview")
    compose = root / "compose.env"
    # Omit both spelling families for the existence-fallback case, including
    # the current filename normally written by a fresh node.
    compose.write_text("\n".join(line for line in compose.read_text().splitlines()
                                 if "APP_ENV_FILE=" not in line) + "\n")
    if selection:
        installer._replace_env(compose, "EXCULPATA_APP_ENV_FILE", selected, add_missing_aliases=True)
    before = {path: path.read_bytes() for path in (compose, root / "installation.json", *(paths["config"] / filename for filename in filenames.values()))}
    calls = []
    def run(console, argv, **kwargs):
        if argv[:2] == ["docker", "compose"]:
            environment = installer._compose_environment(argv)
            assert environment["EXCULPATA_APP_ENV_FILE"] == selected
            assert environment["EXCULPATA_COMPOSE_PROJECT"].startswith(names + "-")
            calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(installer, "_preflight", lambda *a, **k: ())
    monkeypatch.setattr(installer, "_stage_release", lambda *a, **k: ("synthetic-next", ROOT))
    monkeypatch.setattr(sys, "argv", ["install", command, "--root", str(root), "--dry-run", "--non-interactive", "--no-backup"])
    assert installer.main() == 0
    assert calls
    assert installer._node_names(root) == names
    assert installer._app_env(root) == paths["config"] / selected
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("marker_names", [LEGACY_STEM, CURRENT_STEM])
def test_cli_with_both_app_files_still_rejects_storage_generation_conflict(tmp_path, monkeypatch, marker_names):
    names = CURRENT_STEM if marker_names == LEGACY_STEM else LEGACY_STEM
    root, _, paths = configured_node(tmp_path, names=names)
    # Older legacy records without a naming field are durable evidence too.
    other = paths["config"] / f"{marker_names}.env"
    other.write_bytes((paths["config"] / f"{names}.env").read_bytes())
    (paths["storage"] / f".{marker_names}-managed-storage.json").write_text("{}")
    monkeypatch.setattr(installer, "_doctor", lambda *a: pytest.fail("conflict reached doctor"))
    monkeypatch.setattr(sys, "argv", ["install", "doctor", "--root", str(root), "--dry-run", "--non-interactive"])
    assert installer.main() == 1


@pytest.mark.parametrize("names", [LEGACY_STEM, CURRENT_STEM])
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_single_app_filename_cannot_override_saved_generation(tmp_path, monkeypatch, names, explicit):
    root, _, paths = configured_node(tmp_path, names=names)
    filename = f"{CURRENT_STEM if names == LEGACY_STEM else LEGACY_STEM}.env"
    (paths["config"] / f"{names}.env").rename(paths["config"] / filename)
    compose = root / "compose.env"
    compose.write_text("\n".join(line for line in compose.read_text().splitlines()
                                 if "APP_ENV_FILE=" not in line) + "\n")
    if explicit:
        installer._replace_env(compose, "EXCULPATA_APP_ENV_FILE", filename, add_missing_aliases=True)
    before = {path: path.read_bytes() for path in (compose, root / "installation.json", paths["config"] / filename)}
    args = installer._parser().parse_args(["update", "--root", str(root)])
    installer._saved_node_arguments(args, root)
    calls = []
    def run(console, command, **kwargs):
        env = installer._compose_environment(command)
        assert env["EXCULPATA_APP_ENV_FILE"] == filename
        assert env["EXCULPATA_COMPOSE_PROJECT"].startswith(names + "-")
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(installer, "_run", run)
    monkeypatch.setattr(sys, "argv", ["install", "doctor", "--root", str(root), "--dry-run", "--non-interactive"])
    assert installer.main() == 0
    assert calls
    assert installer._node_names(root) == names
    assert {path: path.read_bytes() for path in before} == before
