from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import recordbench_backup as backup  # noqa: E402


def test_browser_account_profile_is_preserved_and_hashed_in_backup(tmp_path):
    compose = backup._compose(tmp_path, {"release_path": str(ROOT), "local_account_management": True})
    assert str(ROOT / "compose.local-accounts.yaml") in compose
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    account = accounts / "local-accounts.json"
    account.write_text("synthetic account snapshot")
    backup._hash_controls(tmp_path)
    assert f"{hashlib.sha256(account.read_bytes()).hexdigest()}  accounts/local-accounts.json" in (tmp_path / "CONTROL_SHA256SUMS").read_text()


@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_backup_entrypoints_are_executable_and_parse(names) -> None:
    scripts = [
        ROOT / f"scripts/{names}-backup.sh",
        ROOT / f"scripts/{names}-backup-status.sh",
        ROOT / f"scripts/{names}-restore-drill.sh",
        ROOT / f"scripts/{names}_backup.py",
    ]
    for script in scripts:
        assert script.is_file()
        assert os.access(script, os.X_OK)
    subprocess.run(["bash", "-n", *map(str, scripts[:3])], check=True)
    subprocess.run(
        [sys.executable, str(scripts[-1]), "backup", "--help"],
        check=True,
        capture_output=True,
    )


def test_backup_contract_defers_all_background_work_and_restarts_early() -> None:
    source = (ROOT / "scripts/recordbench_backup.py").read_text(encoding="utf-8")
    for marker in (
        '"ingest_jobs"',
        '"media_jobs"',
        '"media_summaries"',
        '"answer_jobs"',
        '"research_jobs"',
        '"review_runs"',
        '"report_compilation_jobs"',
        'state="deferred"',
        '"cp", "-al"',
        '"stop", "--timeout", "120", "gateway", "app"',
        '"up", "-d", "--no-deps", "app", "gateway"',
        '"--keep-within"',
        '"--read-data-subset=1/14"',
    ):
        assert marker in source
    assert source.index('"up", "-d", "--no-deps", "app", "gateway"') < source.index(
        "result = _restic("
    )


def test_restore_is_new_target_only_and_validates_every_store() -> None:
    source = (ROOT / "scripts/recordbench_backup.py").read_text(encoding="utf-8")
    for marker in (
        "target.exists()",
        "CONTROL_SHA256SUMS",
        "PRAGMA quick_check;",
        "PRAGMA foreign_key_check;",
        "pg_restore",
        "RESTORE_DRILL_VERIFIED.json",
        "separate from the live node",
    ):
        assert marker in source


def test_generated_dotenv_parser_handles_quoted_paths_without_shell_evaluation(tmp_path) -> None:
    path = tmp_path / "compose.env"
    path.write_text(
        '# generated\nRECORDBENCH_STORAGE_ROOT="/srv/Case Files/recordbench"\n'
        'RECORDBENCH_RELEASE_ID="alpha-1"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)
    assert backup._dotenv(path) == {
        "RECORDBENCH_STORAGE_ROOT": "/srv/Case Files/recordbench",
        "RECORDBENCH_RELEASE_ID": "alpha-1",
    }


def test_restore_checksum_rejects_escape_and_accepts_a_control_file(tmp_path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    control = payload / "control"
    control.mkdir()
    item = control / "installation.json"
    item.write_bytes(b"synthetic-control\n")
    digest = hashlib.sha256(item.read_bytes()).hexdigest()
    backup._verify_checksum_line(payload, f"{digest}  control/installation.json")
    (tmp_path / "outside").write_bytes(item.read_bytes())
    with pytest.raises(backup.BackupError, match="escapes"):
        backup._verify_checksum_line(payload, f"{digest}  ../outside")


@pytest.mark.parametrize("names", ["recordbench", "exculpata"])
def test_quiet_hour_timer_has_retry_and_is_not_host_wide(names) -> None:
    service = (ROOT / f"deploy/systemd/{names}-backup.service.in").read_text()
    timer = (ROOT / f"deploy/systemd/{names}-backup.timer.in").read_text()
    assert "recordbench_backup.py" not in service
    assert "@TOOL@ backup --node-root @NODE_ROOT@" in service
    assert "UMask=0077" in service
    assert "OnCalendar=*-*-* 03:15:00" in timer
    assert "OnCalendar=*-*-* 05:15:00" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
