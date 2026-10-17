from __future__ import annotations


from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import json
import multiprocessing
import os
import shutil
import threading
from pathlib import Path

import pytest

from case_intelligence import local_accounts
from case_intelligence.identity import IdentityService, LocalAccountSettings, LocalAuthenticationError
from case_intelligence.local_accounts import LocalAccountRepository
from case_intelligence.workspace_store import WorkspaceStore
from tests.test_local_authentication import _accounts_file

ACTOR = "synthetic-operator"
PASSWORD = "synthetic-local-password"


def repository(tmp_path):
    result = LocalAccountRepository(tmp_path / "private accounts/accounts.json")
    result.initialize("alice.admin", "Alice Administrator", PASSWORD, actor=ACTOR)
    return result


def identity(tmp_path, repo):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    return IdentityService(WorkspaceStore(runtime / "workbench.sqlite"), runtime,
                           auth_mode="local", secure_cookie=True,
                           local_settings=LocalAccountSettings(repo.path))


def _concurrent_change(path, operation, username, barrier, queue):
    repo = LocalAccountRepository(Path(path))
    barrier.wait(timeout=10)
    try:
        if operation == "create":
            repo.create(username, "Generated Reviewer", PASSWORD, actor=ACTOR)
        else:
            repo.set_administrator(username, False, actor=ACTOR)
        queue.put("ok")
    except RuntimeError:
        queue.put("denied")


def test_independent_writers_retain_both_account_changes(tmp_path):
    repo = repository(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_concurrent_change,
                               args=(str(repo.path), "create", username, barrier, queue))
               for username in ("first.reviewer", "second.reviewer")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in workers) == ["ok", "ok"]
    assert set(repo.read()) == {"alice.admin", "first.reviewer", "second.reviewer"}


def test_last_administrator_race_preserves_one_enabled_administrator(tmp_path):
    repo = repository(tmp_path)
    repo.create("second.admin", "Second Administrator", PASSWORD, administrator=True, actor=ACTOR)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_concurrent_change,
                               args=(str(repo.path), "demote", username, barrier, queue))
               for username in ("alice.admin", "second.admin")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in workers) == ["denied", "ok"]
    assert sum(account.enabled and "administrator" in account.roles for account in repo.read().values()) == 1


@pytest.mark.parametrize("operation", ["disable", "demote"])
def test_last_administrator_rejection_does_not_change_file(tmp_path, operation):
    repo = repository(tmp_path)
    before = repo.path.read_bytes()
    with pytest.raises(RuntimeError, match="At least one enabled"):
        if operation == "disable":
            repo.set_enabled("alice.admin", False, actor=ACTOR)
        else:
            repo.set_administrator("alice.admin", False, actor=ACTOR)
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failed_write_preserves_original_and_releases_writer_lock(tmp_path, monkeypatch, failure):
    repo = repository(tmp_path)
    before = repo.path.read_bytes()
    original = getattr(local_accounts.os, failure)
    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(local_accounts.os, failure, fail)
    with pytest.raises(OSError, match="synthetic write failure"):
        repo.change_display_name("alice.admin", "Changed Name", actor=ACTOR)
    assert repo.path.read_bytes() == before
    assert not list(repo.path.parent.glob("*.tmp"))
    monkeypatch.setattr(local_accounts.os, failure, original)
    repo.change_display_name("alice.admin", "Changed Name", actor=ACTOR)
    assert repo.read()["alice.admin"].display_name == "Changed Name"


def test_readers_see_complete_snapshots_without_writer_lock(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    ready, release = threading.Event(), threading.Event()
    original = local_accounts.os.replace
    failures = []
    def paused_replace(*args, **kwargs):
        ready.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(local_accounts.os, "replace", paused_replace)
    def change():
        try:
            repo.change_display_name("alice.admin", "After Replacement", actor=ACTOR)
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=change)
    worker.start()
    try:
        assert ready.wait(10)
        assert repo.read()["alice.admin"].display_name == "Alice Administrator"
    finally:
        release.set()
        worker.join(timeout=10)
    assert not failures and not worker.is_alive()
    assert repo.read()["alice.admin"].display_name == "After Replacement"


def test_migration_backup_clean_restore_and_rollback_preserve_accounts(tmp_path):
    source = _accounts_file(tmp_path)
    original = source.read_bytes()
    repo = LocalAccountRepository(source)
    # Existing v1 sign-in is readable; mutation requires deliberate migration.
    settings = LocalAccountSettings(source)
    assert settings.authenticate("alice.reviewer", PASSWORD)
    with pytest.raises(RuntimeError, match="Migrate local accounts"):
        repo.change_password("alice.reviewer", "synthetic-new-password", actor=ACTOR)
    backup = tmp_path / "recovery/accounts-v1.json"
    repo.migrate(backup, actor=ACTOR)
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    assert json.loads(source.read_bytes())["format_version"] == 2
    assert settings.authenticate("alice.reviewer", PASSWORD)
    # New-format backup restored into a clean private directory survives restart.
    restored = tmp_path / "restored/accounts.json"
    restored.parent.mkdir(mode=0o700)
    shutil.copyfile(source, restored)
    restored.chmod(0o600)
    assert LocalAccountSettings(restored).authenticate("alice.reviewer", PASSWORD)
    # A rollback restores exactly the v1 bytes expected by the prior release.
    rollback = tmp_path / "rollback/accounts.json"
    rollback.parent.mkdir(mode=0o700)
    shutil.copyfile(backup, rollback)
    rollback.chmod(0o600)
    assert rollback.read_bytes() == original
    assert json.loads(rollback.read_bytes())["format_version"] == 1
    assert LocalAccountSettings(rollback).authenticate("alice.reviewer", PASSWORD)


def test_failed_migration_backup_does_not_upgrade_or_overwrite_backup(tmp_path):
    source = _accounts_file(tmp_path)
    original = source.read_bytes()
    backup = tmp_path / "existing-backup.json"
    backup.write_text("synthetic existing recovery copy")
    backup.chmod(0o600)
    with pytest.raises(FileExistsError):
        LocalAccountRepository(source).migrate(backup, actor=ACTOR)
    assert source.read_bytes() == original
    assert backup.read_text() == "synthetic existing recovery copy"


@pytest.mark.parametrize("operation", ["password", "disable-enable", "demote-promote"])
def test_changes_revoke_sessions_without_restart_and_never_revive(tmp_path, operation):
    repo = repository(tmp_path)
    repo.create("backup.admin", "Backup Administrator", PASSWORD, administrator=True, actor=ACTOR)
    first, second = identity(tmp_path, repo), identity(tmp_path, repo)
    context, token = first.login_local("alice.admin", PASSWORD)
    assert second.resolve(token).is_administrator
    if operation == "password":
        repo.change_password("alice.admin", "synthetic-updated-password", actor=ACTOR)
    elif operation == "disable-enable":
        repo.set_enabled("alice.admin", False, actor=ACTOR)
        repo.set_enabled("alice.admin", True, actor=ACTOR)
    else:
        repo.set_administrator("alice.admin", False, actor=ACTOR)
        repo.set_administrator("alice.admin", True, actor=ACTOR)
    # No request occurred between removal and restoration: the revision still
    # makes the old token invalid in both already-running settings instances.
    assert second.resolve(token) is None
    assert first.resolve(token) is None
    assert first.store.resolve_session(first.token_digest(token), now="2026-01-01T00:00:00Z", next_idle_expires_at="2026-01-01T01:00:00Z") is None
    if operation == "password":
        with pytest.raises(LocalAuthenticationError):
            first.login_local("alice.admin", PASSWORD)
        new_password = "synthetic-updated-password"
    else:
        new_password = PASSWORD
    _, fresh = second.login_local("alice.admin", new_password)
    assert first.resolve(fresh)
    assert context.session.token_digest != token


def test_display_name_refresh_preserves_session_and_new_accounts_are_live(tmp_path):
    repo = repository(tmp_path)
    service = identity(tmp_path, repo)
    _, token = service.login_local("alice.admin", PASSWORD)
    revision = repo.read()["alice.admin"].session_revision
    repo.change_display_name("alice.admin", "Updated Administrator", actor=ACTOR)
    assert service.resolve(token).display_name == "Updated Administrator"
    assert repo.read()["alice.admin"].session_revision == revision
    repo.create("new.reviewer", "New Reviewer", PASSWORD, actor=ACTOR)
    context, _ = service.login_local("new.reviewer", PASSWORD)
    assert not context.is_administrator


def test_relocation_rollback_denies_pre_move_and_moved_sessions_without_intervening_requests(tmp_path):
    repo = repository(tmp_path)
    original = identity(tmp_path, repo)
    _, before_cookie = original.login_local("alice.admin", PASSWORD)
    before = repo.read()["alice.admin"]
    destination = tmp_path / "dedicated/local-accounts.json"
    backup = tmp_path / "recovery/accounts.json"
    repo.relocate(destination, backup, actor=ACTOR, writers_stopped=True)
    moved = identity(tmp_path, LocalAccountRepository(destination))
    _, moved_cookie = moved.login_local("alice.admin", PASSWORD)
    # Restore the old path with the original database and signing key. Neither
    # cookie has been resolved since its account snapshot was replaced.
    shutil.copy2(backup, repo.path)
    restored = identity(tmp_path, repo)
    assert restored.resolve(before_cookie) is None
    assert restored.resolve(moved_cookie) is None
    context, fresh = restored.login_local("alice.admin", PASSWORD)
    assert context.is_administrator and restored.resolve(fresh)
    after = repo.read()["alice.admin"]
    assert (after.username, after.display_name, after.password_hash, after.roles, after.enabled) == (
        before.username, before.display_name, before.password_hash, before.roles, before.enabled)
    assert len({before.session_revision, moved.local_settings.accounts["alice.admin"].session_revision,
                after.session_revision}) == 3
    assert backup.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("display_name", ["Synthetic\u202eReviewer", "Synthetic\x7fReviewer", "Synthetic\x85Reviewer", "Synthetic\u200dReviewer"])
@pytest.mark.parametrize("operation", ["create", "rename"])
def test_invalid_display_names_do_not_change_account_snapshot(tmp_path, display_name, operation):
    repo = repository(tmp_path)
    before = repo.path.read_bytes()
    with pytest.raises(RuntimeError, match="Display name"):
        if operation == "create":
            repo.create("new.reviewer", display_name, PASSWORD, actor=ACTOR)
        else:
            repo.change_display_name("alice.admin", display_name, actor=ACTOR)
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize("display_name, normalized", [("  Jose\u0301 審査 🙂  ", "José 審査 🙂"), ("e\u0301" * 160, "é" * 160)], ids=["multilingual", "normalized-limit"])
def test_unicode_display_names_normalize_before_create_and_rename(tmp_path, display_name, normalized):
    repo = repository(tmp_path)
    repo.create("new.reviewer", display_name, PASSWORD, actor=ACTOR)
    service = identity(tmp_path, repo)
    context, cookie = service.login_local("new.reviewer", PASSWORD)
    assert context.display_name == normalized
    assert repo.read()["new.reviewer"].display_name == normalized
    _, admin_cookie = service.login_local("alice.admin", PASSWORD)
    repo.change_display_name("alice.admin", display_name, actor=ACTOR)
    assert repo.read()["alice.admin"].display_name == normalized
    assert service.resolve(admin_cookie).display_name == normalized
    assert service.resolve(cookie).display_name == normalized


def test_invalid_live_file_denies_existing_session_and_sign_in_safely(tmp_path):
    repo = repository(tmp_path)
    service = identity(tmp_path, repo)
    _, token = service.login_local("alice.admin", PASSWORD)
    repo.path.chmod(0o644)
    assert service.resolve(token) is None
    with pytest.raises(LocalAuthenticationError, match="temporarily unavailable"):
        service.login_local("alice.admin", PASSWORD)


def test_safe_file_boundary_rejects_symlink_and_non_regular_inputs(tmp_path):
    repo = repository(tmp_path)
    linked = repo.path.parent / "linked.json"
    linked.symlink_to(repo.path)
    with pytest.raises(OSError):
        LocalAccountRepository(linked).read()
    fifo = repo.path.parent / "fifo.json"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(RuntimeError, match="unsafe"):
        LocalAccountRepository(fifo).read()
    directory_link = tmp_path / "linked-directory"
    directory_link.symlink_to(repo.path.parent)
    with pytest.raises(OSError):
        LocalAccountRepository(directory_link / "accounts.json").read()


def test_cli_uses_shared_changes_and_never_logs_passwords_or_hashes(tmp_path, monkeypatch, capsys):
    from case_intelligence import admin_cli
    repo = repository(tmp_path)
    monkeypatch.setattr("sys.argv", [f"{LEGACY_STEM}", "accounts", "display-name", "--file", str(repo.path),
                                   "--username", "alice.admin", "--display-name", "CLI Updated"])
    admin_cli.main()
    output = capsys.readouterr().out
    assert "actor=uid:" in output and "display-name" in output
    assert PASSWORD not in output and "$argon2" not in output
    assert repo.read()["alice.admin"].display_name == "CLI Updated"


def _long_lived_reader(path, ready, changed, queue):
    settings = LocalAccountSettings(Path(path))
    ready.set()
    if not changed.wait(10):
        raise RuntimeError("synthetic reader timed out")
    queue.put(settings.account("alice.admin").display_name)


def test_running_process_refreshes_account_after_independent_change(tmp_path):
    repo = repository(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, changed, queue = context.Event(), context.Event(), context.Queue()
    worker = context.Process(target=_long_lived_reader, args=(str(repo.path), ready, changed, queue))
    worker.start()
    try:
        assert ready.wait(10)
        repo.change_display_name("alice.admin", "Fresh Across Processes", actor=ACTOR)
    finally:
        changed.set()
        worker.join(timeout=15)
    assert worker.exitcode == 0
    assert queue.get(timeout=2) == "Fresh Across Processes"


def test_credential_revision_survives_restart_and_does_not_revoke_other_accounts(tmp_path):
    repo = repository(tmp_path)
    repo.create("other.reviewer", "Other Reviewer", PASSWORD, actor=ACTOR)
    service = identity(tmp_path, repo)
    _, old = service.login_local("alice.admin", PASSWORD)
    _, unrelated = service.login_local("other.reviewer", PASSWORD)
    repo.change_password("alice.admin", "synthetic-updated-password", actor=ACTOR)
    restarted = identity(tmp_path, repo)
    assert restarted.resolve(old) is None
    assert restarted.resolve(unrelated).principal.provider_subject == "other.reviewer"
    _, fresh = restarted.login_local("alice.admin", "synthetic-updated-password")
    assert identity(tmp_path, repo).resolve(fresh)


def test_reader_does_not_create_lock_or_require_directory_write(tmp_path, monkeypatch):
    source = _accounts_file(tmp_path)
    assert not (source.parent / f".{source.name}.lock").exists()
    original = local_accounts.os.open
    def read_only(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(local_accounts.os, "open", read_only)
    assert LocalAccountSettings(source).authenticate("alice.reviewer", PASSWORD)
    assert not (source.parent / f".{source.name}.lock").exists()


def test_near_limit_account_snapshot_is_parsed_once_across_session_requests(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    payload = json.loads(repo.path.read_bytes())
    template = payload["accounts"][0]
    payload["accounts"].extend({**template, "username": f"reviewer.{index:03}", "roles": []}
                               for index in range(499))
    repo.path.write_text(json.dumps(payload))
    original = local_accounts._parse
    parses = []
    def count_parse(value):
        parses.append(len(value["accounts"]))
        return original(value)
    monkeypatch.setattr(local_accounts, "_parse", count_parse)
    service = identity(tmp_path, repo)
    _, token = service.login_local("alice.admin", PASSWORD)
    for _ in range(100):
        assert service.resolve(token).is_administrator
    assert parses == [500]
    snapshot = service.local_settings.accounts
    with pytest.raises(TypeError):
        snapshot["alice.admin"] = snapshot["reviewer.000"]
    assert service.local_settings.accounts is snapshot


@pytest.mark.parametrize("fault", ["missing", "corrupt", "mode", "parent-mode", "symlink", "fifo", "oversized"])
def test_cached_snapshot_never_masks_failed_read_and_sessions_stay_revoked(tmp_path, fault):
    repo = repository(tmp_path)
    service = identity(tmp_path, repo)
    _, token = service.login_local("alice.admin", PASSWORD)
    reader = service.local_settings.repository
    original = repo.path.read_bytes()
    assert reader.read()
    if fault == "missing":
        repo.path.unlink()
    elif fault == "corrupt":
        repo.path.write_bytes(b"invalid synthetic JSON")
    elif fault == "mode":
        repo.path.chmod(0o644)
    elif fault == "parent-mode":
        repo.path.parent.chmod(0o777)
    elif fault == "symlink":
        other = repo.path.with_name("other.json")
        other.write_bytes(original)
        other.chmod(0o600)
        repo.path.unlink()
        repo.path.symlink_to(other)
    elif fault == "fifo":
        repo.path.unlink()
        os.mkfifo(repo.path, 0o600)
    else:
        repo.path.write_bytes(b" " * (local_accounts._MAX_BYTES + 1))
    with pytest.raises((OSError, RuntimeError)):
        reader.read()
    assert reader._snapshot is None and reader._snapshot_key is None
    assert service.resolve(token) is None
    with pytest.raises(LocalAuthenticationError, match="temporarily unavailable"):
        service.login_local("alice.admin", PASSWORD)
    repo.path.unlink(missing_ok=True)
    repo.path.parent.chmod(0o700)
    repo.path.write_bytes(original)
    repo.path.chmod(0o600)
    assert service.resolve(token) is None
    _, fresh = service.login_local("alice.admin", PASSWORD)
    assert service.resolve(fresh).is_administrator


@pytest.mark.parametrize("colliding_metadata", [False, True])
def test_same_size_edit_with_restored_mtime_reloads_cached_snapshot(tmp_path, monkeypatch, colliding_metadata):
    repo = repository(tmp_path)
    if colliding_metadata:
        # Deterministically model a filesystem timestamp collision; do not wait
        # for a kernel clock tick or depend on a particular filesystem.
        key = local_accounts._file_identity(repo.path.stat())
        monkeypatch.setattr(local_accounts, "_file_identity", lambda metadata: key)
    before = repo.read()
    metadata = repo.path.stat()
    original = repo.path.read_bytes()
    updated = original.replace(b"Alice Administrator", b"Other Administrator")
    assert len(updated) == len(original)
    repo.path.write_bytes(updated)
    os.utime(repo.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert repo.read()["alice.admin"].display_name == "Other Administrator"
    assert before["alice.admin"].display_name == "Alice Administrator"


def test_colliding_metadata_cannot_hide_invalid_account_bytes(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    key = local_accounts._file_identity(repo.path.stat())
    monkeypatch.setattr(local_accounts, "_file_identity", lambda metadata: key)
    repo.read()
    data = repo.path.read_bytes()
    repo.path.write_bytes(b"!" + data[1:])
    with pytest.raises(RuntimeError, match="unreadable or invalid"):
        repo.read()
    assert repo._snapshot is None and repo._snapshot_data is None


def test_snapshot_read_rejects_in_place_changes_during_validation(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    original = local_accounts._parse
    def modify_during_parse(payload):
        repo.path.write_bytes(b"synthetic concurrent edit")
        return original(payload)
    monkeypatch.setattr(local_accounts, "_parse", modify_during_parse)
    with pytest.raises(RuntimeError, match="changed while reading"):
        repo.read()
    assert repo._snapshot is None


def test_atomic_replacement_during_read_is_seen_on_next_request(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    original = local_accounts._read_descriptor
    replacement = repo.path.with_name("replacement.json")
    replacement.write_bytes(repo.path.read_bytes().replace(b"Alice Administrator", b"Fresh Administrator"))
    replacement.chmod(0o600)
    replaced = False
    def replace_after_read(descriptor):
        nonlocal replaced
        result = original(descriptor)
        if not replaced:
            os.replace(replacement, repo.path)
            replaced = True
        return result
    monkeypatch.setattr(local_accounts, "_read_descriptor", replace_after_read)
    # Unlinking the opened inode may advance ctime: rejecting that first
    # in-flight request is also safe. The next read must resolve the new file.
    try:
        assert repo.read()["alice.admin"].display_name == "Alice Administrator"
    except RuntimeError as exc:
        assert "changed while reading" in str(exc)
    assert repo.read()["alice.admin"].display_name == "Fresh Administrator"


def test_cache_hit_still_checks_file_owner(tmp_path, monkeypatch):
    from types import SimpleNamespace
    repo = repository(tmp_path)
    assert repo.read()
    inode = repo.path.stat().st_ino
    original = local_accounts.os.fstat
    def unsafe_owner(descriptor):
        metadata = original(descriptor)
        if metadata.st_ino == inode:
            return SimpleNamespace(st_mode=metadata.st_mode, st_uid=os.geteuid() + 1)
        return metadata
    monkeypatch.setattr(local_accounts.os, "fstat", unsafe_owner)
    with pytest.raises(RuntimeError, match="unsafe"):
        repo.read()
    assert repo._snapshot is None


@pytest.mark.parametrize("change", ["disable", "demote"])
def test_live_account_restriction_revokes_prior_session_and_applies_to_new_login(tmp_path, change):
    repo = repository(tmp_path)
    repo.create("backup.admin", "Backup Administrator", PASSWORD, administrator=True, actor=ACTOR)
    service = identity(tmp_path, repo)
    _, token = service.login_local("alice.admin", PASSWORD)
    assert service.resolve(token).is_administrator
    if change == "disable":
        repo.set_enabled("alice.admin", False, actor=ACTOR)
        with pytest.raises(LocalAuthenticationError, match="incorrect"):
            service.login_local("alice.admin", PASSWORD)
    else:
        repo.set_administrator("alice.admin", False, actor=ACTOR)
        _, fresh = service.login_local("alice.admin", PASSWORD)
        assert not service.resolve(fresh).is_administrator
    assert service.resolve(token) is None


@pytest.mark.parametrize("operation", ["migrate", "relocate"])
@pytest.mark.parametrize("fail_sync", [False, True])
def test_account_move_syncs_each_new_parent_before_canonical_change(tmp_path, monkeypatch, operation, fail_sync):
    repo = LocalAccountRepository(_accounts_file(tmp_path)) if operation == "migrate" else repository(tmp_path)
    before = repo.path.read_bytes()
    destination = tmp_path / "new-target" / "managed" / "local-accounts.json"
    backup = tmp_path / "new-recovery" / "snapshot" / "accounts.json"
    pending = set()
    synced = []
    real_mkdir, real_fsync = os.mkdir, os.fsync
    real_replace, real_unlink = os.replace, os.unlink

    def make_directory(path, mode=0o777, *, dir_fd=None):
        assert not pending, "A new parent was used before its directory entry was synced"
        result = real_mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is not None:
            metadata = os.fstat(dir_fd)
            pending.add((metadata.st_dev, metadata.st_ino))
        return result

    def sync_directory(descriptor):
        metadata = os.fstat(descriptor)
        inode = (metadata.st_dev, metadata.st_ino)
        if inode in pending:
            if fail_sync:
                raise OSError("Synthetic directory persistence failure")
            synced.append(inode)
            pending.remove(inode)
        return real_fsync(descriptor)

    def replace(*args, **kwargs):
        assert not pending, "Canonical replacement preceded parent persistence"
        return real_replace(*args, **kwargs)

    def unlink(*args, **kwargs):
        assert not pending, "Canonical removal preceded parent persistence"
        return real_unlink(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "mkdir", make_directory)
        patch.setattr(os, "fsync", sync_directory)
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "unlink", unlink)
        def change():
            if operation == "migrate":
                repo.migrate(backup, actor=ACTOR)
            else:
                repo.relocate(destination, backup, actor=ACTOR, writers_stopped=True)
        if fail_sync:
            with pytest.raises(OSError, match="persistence failure"):
                change()
            assert repo.path.read_bytes() == before
            assert not destination.exists() and not backup.exists()
        else:
            change()
            assert not pending and len(synced) == (2 if operation == "migrate" else 4)
            if operation == "relocate":
                assert backup.read_bytes() != before
                assert not repo.path.exists() and destination.exists()
            else:
                assert backup.read_bytes() == before
                assert json.loads(repo.path.read_bytes())["format_version"] == 2


@pytest.mark.parametrize("mode", [0o777, 0o775])
def test_replaceable_ancestor_denies_cached_reads_and_writes(tmp_path, mode):
    boundary = tmp_path / "shared"
    repo = repository(boundary)
    assert repo.read()
    original = repo.path.read_bytes()
    boundary.chmod(mode)
    with pytest.raises(RuntimeError, match="ancestors"):
        repo.read()
    assert repo._snapshot is None
    with pytest.raises(RuntimeError, match="ancestors"):
        repo.change_display_name("alice.admin", "Must Not Save", actor=ACTOR)
    assert repo.path.read_bytes() == original
    boundary.chmod(0o700)
    assert repo.read()["alice.admin"].display_name == "Alice Administrator"


@pytest.mark.parametrize("operation", ["initialize", "migrate", "relocate-destination", "relocate-backup"])
def test_replaceable_ancestor_blocks_creation_and_recovery_before_canonical_change(tmp_path, operation):
    unsafe = tmp_path / "shared"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    source = _accounts_file(tmp_path) if operation == "migrate" else repository(tmp_path).path
    repo = LocalAccountRepository(source)
    original = source.read_bytes()
    with pytest.raises(RuntimeError, match="ancestors"):
        if operation == "initialize":
            LocalAccountRepository(unsafe / "new/accounts.json").initialize("new.admin", "New Admin", PASSWORD, actor=ACTOR)
        elif operation == "migrate":
            repo.migrate(unsafe / "new/recovery.json", actor=ACTOR)
        elif operation == "relocate-destination":
            repo.relocate(unsafe / "new/local-accounts.json", tmp_path / "recovery/accounts.json", actor=ACTOR, writers_stopped=True)
        else:
            repo.relocate(tmp_path / "dedicated/local-accounts.json", unsafe / "new/recovery.json", actor=ACTOR, writers_stopped=True)
    assert source.read_bytes() == original
    assert not (unsafe / "new").exists()


def test_foreign_owned_ancestor_is_refused_even_when_not_writable(tmp_path, monkeypatch):
    from types import SimpleNamespace
    repo = repository(tmp_path / "foreign")
    assert repo.read()
    ancestor = (tmp_path / "foreign").stat().st_ino
    native = local_accounts.os.fstat
    def foreign_owner(descriptor):
        metadata = native(descriptor)
        if metadata.st_ino == ancestor:
            return SimpleNamespace(st_uid=os.geteuid() + 1000, st_mode=metadata.st_mode)
        return metadata
    monkeypatch.setattr(local_accounts.os, "fstat", foreign_owner)
    with pytest.raises(RuntimeError, match="ancestors"):
        repo.read()
    assert repo._snapshot is None


def test_only_root_owned_sticky_ancestors_are_trusted(tmp_path, monkeypatch):
    from types import SimpleNamespace
    repo = repository(tmp_path / "sticky")
    ancestor = (tmp_path / "sticky").stat().st_ino
    native = local_accounts.os.fstat
    owner = 0
    def sticky_owner(descriptor):
        metadata = native(descriptor)
        if metadata.st_ino == ancestor:
            return SimpleNamespace(st_uid=owner, st_mode=0o41777)
        return metadata
    monkeypatch.setattr(local_accounts.os, "fstat", sticky_owner)
    assert repo.read()
    owner = os.geteuid() if os.geteuid() else 1000
    with pytest.raises(RuntimeError, match="ancestors"):
        repo.read()


def test_session_resolution_cannot_overwrite_newer_persisted_account_name(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    service = identity(tmp_path, repo)
    context, token = service.login_local("alice.admin", PASSWORD)
    original = service.store.refresh_principal_display_name
    repo.change_display_name("alice.admin", "Intermediate Name", actor=ACTOR)
    def rename_before_projection(provider, username, display_name, *, expected_display_name=None):
        assert display_name == "Intermediate Name" and expected_display_name == "Alice Administrator"
        repo.change_display_name("alice.admin", "Newest Name", actor=ACTOR)
        original(provider, username, "Newest Name")
        original(provider, username, display_name, expected_display_name=expected_display_name)
    monkeypatch.setattr(service.store, "refresh_principal_display_name", rename_before_projection)
    resolved = service.resolve(token)
    assert resolved.display_name == "Newest Name"
    saved = service.store.get_principal(context.principal_id)
    assert saved.display_name == "Newest Name"
    assert saved.active == context.principal.active
    assert saved.login_name == context.principal.login_name
    assert saved.last_seen_at == context.principal.last_seen_at
