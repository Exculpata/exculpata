"""A retained synthetic node resolves without migrating its naming coordinates."""

from __future__ import annotations

import json
import sys

import pytest
from starlette.datastructures import Headers

from case_intelligence import compat_env
from case_intelligence.legacy_names import (
    CURRENT_HEADER_PREFIX,
    CURRENT_STEM,
    LEGACY_ENV_PREFIX,
    LEGACY_HEADER_PREFIX,
    LEGACY_PRODUCT,
    LEGACY_STEM,
)
from tests.test_backup_contract import backup
from tests.test_first_run_handoff import configured_node
from tests.test_oss_installer import installer


@pytest.mark.parametrize("selected_file", [False, True], ids=["legacy-file", "saved-file"])
def test_legacy_upgrade_loads_coordinates_and_dry_run_preserves_node(
    tmp_path, monkeypatch, capsys, selected_file,
):
    # Freeze the synthetic input names independently of the contract table.
    assert (LEGACY_STEM, CURRENT_STEM) == ("recordbench", "exculpata")
    assert LEGACY_ENV_PREFIX == "RECORDBENCH_"
    assert LEGACY_HEADER_PREFIX == "X-RecordBench-"
    assert CURRENT_HEADER_PREFIX == "X-Exculpata-"
    monkeypatch.setattr(installer, "_legacy_account", lambda: False)
    root, _, paths = configured_node(tmp_path, names=LEGACY_STEM)
    application = paths["config"] / f"{LEGACY_STEM}.env"
    if selected_file:
        selected = paths["config"] / "synthetic-upgrade.env"
        application.rename(selected)
        application = selected
        with (root / "compose.env").open("a") as stream:
            stream.write(f'{LEGACY_ENV_PREFIX}APP_ENV_FILE="{selected.name}"\n')

    storage_id = f"{LEGACY_STEM}-storage-synthetic-upgrade"
    marker = paths["storage"] / f".{LEGACY_STEM}-managed-storage.json"
    marker.write_text(json.dumps({
        "format_version": 1, "product": LEGACY_PRODUCT, "storage_id": storage_id,
    }))
    for name in ("matters", ".matter-purging", "ingestion-staging"):
        (paths["storage"] / name).mkdir()
    repository = tmp_path / f"{LEGACY_STEM}-backup-synthetic"
    repository.mkdir()
    recovery = tmp_path / "synthetic-recovery-key"
    backup_coordinates = {
        "format_version": 1, "repository": str(repository),
        "recovery_key_output": str(recovery), "retention": "14d",
    }
    backup_config = paths["config"] / "backup.json"
    backup_config.write_text(json.dumps(backup_coordinates))
    backup_config.chmod(0o600)

    def snapshot():
        return {
            path.relative_to(tmp_path): (
                None if path.is_dir() else path.read_bytes(),
                path.stat().st_mode, path.stat().st_mtime_ns,
            )
            for path in tmp_path.rglob("*")
        }

    before = snapshot()
    saved = installer._dotenv(root / "compose.env")
    assert any(key.startswith(LEGACY_ENV_PREFIX) for key in saved)
    assert not any(key.startswith("EXCULPATA_") for key in saved)
    assert backup._dotenv(root / "compose.env") == saved
    assert backup._backup_config(root) == backup_coordinates
    assert installer._node_names(root) == LEGACY_STEM
    assert installer._matter_storage_layout_valid(paths["storage"])
    assert installer._backup_tool(root).name == f"{LEGACY_STEM}_backup.py"

    headers = Headers({
        f"{LEGACY_HEADER_PREFIX}Authenticated-User": "synthetic.reviewer@EXAMPLE.TEST",
        f"{LEGACY_HEADER_PREFIX}Proxy-Secret": "synthetic-upgrade-proof",
    })
    assert compat_env.kerberos_headers(headers) == (
        "synthetic.reviewer@EXAMPLE.TEST", "synthetic-upgrade-proof",
    )
    assert compat_env.response_headers("Theme", "dark") == {
        f"{LEGACY_HEADER_PREFIX}Theme": "dark", f"{CURRENT_HEADER_PREFIX}Theme": "dark",
    }

    # These existing APIs also run on the pre-review rename commit. Its saved
    # filename is ignored, making config loading fail before the dry run.
    args = installer._parser().parse_args([
        "install", "--root", str(root), "--resume", "--dry-run", "--prepare-only",
        "--non-interactive", "--plain",
    ])
    installer._saved_node_arguments(args, root)
    assert installer._app_env(root) == application
    assert args.storage_root == paths["storage"]
    assert args.account_root == paths["accounts"]
    assert args.server_name == f"{LEGACY_STEM}.example.test"
    app_values = installer._dotenv(application)
    assert app_values["CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE"] == (
        f"/var/lib/{LEGACY_STEM}-accounts/local-accounts.json"
    )
    assert f"dbname={LEGACY_STEM} user={LEGACY_STEM}" in (
        paths["secrets"] / "postgres-dsn"
    ).read_text()
    compose = installer._compose(root, ())
    environment = installer._compose_environment(compose, {})
    assert environment["EXCULPATA_COMPOSE_PROJECT"] == saved[f"{LEGACY_ENV_PREFIX}COMPOSE_PROJECT"]
    assert environment["EXCULPATA_IMAGE_NAMESPACE"] == saved[f"{LEGACY_ENV_PREFIX}IMAGE_NAMESPACE"]
    # An omitted legacy APP_ENV_FILE uses Compose's legacy default, whereas an
    # explicit saved filename must be passed through by either configuration.
    assert environment.get("EXCULPATA_APP_ENV_FILE", f"{LEGACY_STEM}.env") == application.name

    # Host prerequisite availability is independent of retained coordinates;
    # keep the real dry-run dispatcher, provisioning and command handling.
    monkeypatch.setattr(installer, "_preflight", lambda *a, **kw: ())
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **kw: pytest.fail(
        "installer dry run attempted an external command"
    ))
    monkeypatch.setattr(sys, "argv", ["install",
        "install", "--root", str(root), "--resume", "--dry-run", "--prepare-only",
        "--non-interactive", "--plain",
    ])
    assert installer.main() == 0
    installer._schedule_backup(installer.Console(color=False), root, dry_run=True)
    output = capsys.readouterr().out
    assert "REJOIN NODE" in output and "PREPARE RUNTIME" in output
    assert f"{LEGACY_STEM}-backup-{installer.hashlib_short(root)}.timer" in output
    assert json.loads(marker.read_text())["storage_id"] == storage_id
    assert snapshot() == before
