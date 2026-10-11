"""Owner-authorized local account storage shared by operator and future UI code.

Readers need only read access. Writers require the existing private directory;
this module does not grant filesystem access or authorize browser requests.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping

from argon2 import PasswordHasher, extract_parameters

from .local_account_format import LocalAccount, normalized_account, parse_accounts

_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LocalAccountChange:
    action: str
    username: str
    actor: str


def hash_password(value: str) -> str:
    if not isinstance(value, str) or not 14 <= len(value) <= 1024 or "\x00" in value:
        raise RuntimeError("Local account passwords must contain 14 to 1,024 characters without NUL")
    return PasswordHasher().hash(value)


def _parse(payload: object) -> tuple[int, dict[str, LocalAccount]]:
    version, accounts = parse_accounts(payload)
    for account in accounts.values():
        try:
            parameters = extract_parameters(account.password_hash)
        except Exception as exc:
            raise RuntimeError("Local account file contains an invalid password hash") from exc
        if parameters.type.name.casefold() != "id":
            raise RuntimeError("Local account passwords must use Argon2id")
    return version, accounts


def _payload(accounts: Mapping[str, LocalAccount]) -> dict[str, object]:
    return {"format_version": 2, "accounts": [
        {"username": account.username, "display_name": account.display_name,
         "password_hash": account.password_hash, "roles": sorted(account.roles),
         "enabled": account.enabled, "session_revision": account.session_revision}
        for account in accounts.values()
    ]}


def _validate_directory(descriptor: int, *, final: bool = False) -> None:
    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    # Root-controlled sticky temporary directories protect entries owned by the
    # service account. No other writable ancestor is a trusted traversal boundary.
    trusted_sticky = metadata.st_uid == 0 and mode == 0o1777 and not final
    if (metadata.st_uid not in ({os.geteuid()} if final else {0, os.geteuid()})
            or (mode & 0o022 and not trusted_sticky)):
        raise RuntimeError("Account directory ancestors must be root- or service-owned and not replaceable by other users")


@contextmanager
def _parent(path: Path, *, create: bool = False) -> Iterator[int]:
    if not path.is_absolute() or path.name in {"", ".", ".."} or ".." in path.parts:
        raise RuntimeError("Account file must be an exact absolute path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        _validate_directory(descriptor)
        for component in path.parent.parts[1:]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                # Persist the new child entry before descending or removing
                # an older canonical store after migration or relocation.
                os.fsync(descriptor)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _validate_directory(descriptor)
        _validate_directory(descriptor, final=True)
        yield descriptor
    finally:
        os.close(descriptor)


def _private_metadata(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise RuntimeError("Local account file is unavailable or unsafe")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("Local account file must have mode 0600")
    if metadata.st_size > _MAX_BYTES:
        raise RuntimeError("Local account file exceeds its size limit")
    return metadata


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            metadata.st_ctime_ns, metadata.st_uid, metadata.st_mode, metadata.st_nlink)


def _read_descriptor(descriptor: int) -> tuple[bytes, int, dict[str, LocalAccount]]:
    before = _file_identity(_private_metadata(descriptor))
    with os.fdopen(descriptor, "rb", closefd=False) as stream:
        data = stream.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise RuntimeError("Local account file exceeds its size limit")
    try:
        version, accounts = _parse(json.loads(data.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Local account file is unreadable or invalid") from exc
    if _file_identity(_private_metadata(descriptor)) != before:
        raise RuntimeError("Local account file changed while reading")
    return data, version, accounts


def _read(directory: int, name: str) -> tuple[bytes, int, dict[str, LocalAccount]]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        return _read_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _write(directory: int, name: str, data: bytes, *, create: bool = False) -> None:
    temporary = f".{name}-{secrets.token_hex(16)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            # A concurrent initializer/backup cannot replace an existing file.
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _invalidate_local_sessions(path: Path) -> None:
    """Invalidate local cookies in an existing stopped workspace, without migrations."""
    with _parent(path) as directory:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            def safe_file(item):
                return (stat.S_ISREG(item.st_mode) and item.st_uid == os.geteuid()
                        and not stat.S_IMODE(item.st_mode) & 0o022 and item.st_nlink == 1)
            if not safe_file(metadata):
                raise RuntimeError("Rollback workspace file is unavailable or unsafe")
            for suffix in ("-wal", "-shm", "-journal"):
                try:
                    companion = os.stat(path.name + suffix, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not safe_file(companion):
                    raise RuntimeError("Rollback workspace journal is unavailable or unsafe")
            # mode=rw refuses a missing database. Never construct WorkspaceStore:
            # its initialization would migrate the operator's recovery database.
            db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if not safe_file(current) or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                    raise RuntimeError("Rollback workspace file changed while opening")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    tables = {row[0] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('workbench_session','workbench_principal')")}
                    required = {"session_id", "token_digest", "principal_id", "auth_method", "revoked_at"}
                    columns = {row[1] for row in db.execute("PRAGMA table_info(workbench_session)")}
                    if tables != {"workbench_session", "workbench_principal"} or not required.issubset(columns):
                        raise RuntimeError("Rollback requires a compatible existing Exculpata workspace database")
                    # Keep append-only audit references intact. Replace the cookie
                    # lookup digest as well as revoking the row so older readers
                    # cannot recover either generation of local credentials.
                    db.execute("UPDATE workbench_session SET token_digest=lower(hex(randomblob(32))), "
                               "revoked_at=COALESCE(revoked_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                               "WHERE auth_method='local'")
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise RuntimeError("Local sessions could not be invalidated; account rollback was not performed") from exc
        finally:
            os.close(descriptor)


class LocalAccountRepository:
    """Validated live snapshots and process-serialized account mutations."""

    def __init__(self, path: Path, *,
                 guard: Callable[[Mapping[str, LocalAccount]], None] | None = None,
                 before_write: Callable[[LocalAccountChange], None] | None = None,
                 after_write: Callable[[LocalAccountChange, Mapping[str, LocalAccount]], None] | None = None) -> None:
        self.path = Path(path)
        self.guard = guard
        self.before_write = before_write
        self.after_write = after_write
        self._snapshot_lock = threading.Lock()
        self._snapshot_key: tuple[int, ...] | None = None
        self._snapshot: Mapping[str, LocalAccount] | None = None
        self._snapshot_data: bytes | None = None

    def _invalidate_snapshot(self) -> None:
        with self._snapshot_lock:
            self._snapshot_key = None
            self._snapshot = None
            self._snapshot_data = None

    def read(self) -> Mapping[str, LocalAccount]:
        # Keep only one immutable snapshot per repository. Every access still
        # traverses the safe path and validates the opened file's ownership/mode.
        # Stat timestamps can collide even at nanosecond resolution. Reuse a
        # parsed snapshot only after checking the bounded bytes as well.
        with self._snapshot_lock:
            try:
                with _parent(self.path) as directory:
                    parent = os.fstat(directory)
                    descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                         dir_fd=directory)
                    try:
                        key = (parent.st_dev, parent.st_ino, *_file_identity(_private_metadata(descriptor)))
                        if self._snapshot is not None and key == self._snapshot_key:
                            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                                data = stream.read(_MAX_BYTES + 1)
                            if key != (parent.st_dev, parent.st_ino, *_file_identity(_private_metadata(descriptor))):
                                raise RuntimeError("Local account file changed while reading")
                            if data == self._snapshot_data:
                                return self._snapshot
                            os.lseek(descriptor, 0, os.SEEK_SET)
                        data, _, accounts = _read_descriptor(descriptor)
                        if key != (parent.st_dev, parent.st_ino, *_file_identity(_private_metadata(descriptor))):
                            raise RuntimeError("Local account file changed while reading")
                        self._snapshot = MappingProxyType(accounts)
                        self._snapshot_key = key
                        self._snapshot_data = data
                        return self._snapshot
                    finally:
                        os.close(descriptor)
            except Exception:
                # A previous good snapshot never masks a missing/unsafe/bad file.
                self._snapshot_key = None
                self._snapshot = None
                self._snapshot_data = None
                raise

    @contextmanager
    def _writer(self, *, create: bool = False) -> Iterator[int]:
        with _parent(self.path, create=create) as directory:
            lock = os.open(f".{self.path.name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=directory)
            try:
                _private_metadata(lock)
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._invalidate_snapshot()
                yield directory
            finally:
                self._invalidate_snapshot()
                os.close(lock)

    @staticmethod
    def _receipt(action: str, username: str, actor: str) -> LocalAccountChange:
        if not isinstance(actor, str) or not actor or len(actor) > 200 or any(ord(char) < 32 for char in actor):
            raise RuntimeError("Account change attribution is required")
        return LocalAccountChange(action, username, actor)

    def _change(self, action: str, username: str, actor: str,
                update: Callable[[dict[str, LocalAccount]], None]) -> LocalAccountChange:
        receipt = self._receipt(action, username, actor)
        with self._writer() as directory:
            _, version, accounts = _read(directory, self.path.name)
            if version != 2:
                raise RuntimeError("Migrate local accounts with an explicit backup before changing them: run recordbench accounts migrate with --file and a new --backup-file")
            if self.guard is not None:
                self.guard(accounts)
            update(accounts)
            payload = _payload(accounts)
            _parse(payload)  # Last-admin and size/shape checks share the writer lock.
            data = (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
            if len(data) > _MAX_BYTES:
                raise RuntimeError("Local account file exceeds its size limit")
            if self.before_write is not None:
                self.before_write(receipt)
            _write(directory, self.path.name, data)
            if self.after_write is not None:
                self.after_write(receipt, accounts)
        return receipt

    def initialize(self, username: str, display_name: str, password: str, *, actor: str) -> LocalAccountChange:
        if self.guard is not None:
            raise RuntimeError("Initial account setup is an operator-only action")
        username, display_name = normalized_account(username, display_name)
        receipt = self._receipt("initialize", username, actor)
        account = LocalAccount(username, display_name, hash_password(password), frozenset({"administrator"}), True, secrets.token_hex(32))
        data = (json.dumps(_payload({username: account}), indent=2, sort_keys=True) + "\n").encode()
        with self._writer(create=True) as directory:
            _write(directory, self.path.name, data, create=True)
        return receipt

    def migrate(self, backup_file: Path, *, actor: str) -> LocalAccountChange:
        if self.guard is not None:
            raise RuntimeError("Account migration is an operator-only action")
        receipt = self._receipt("migrate", "*", actor)
        backup_file = Path(backup_file)
        if backup_file == self.path:
            raise RuntimeError("Migration backup must be separate from the account file")
        with self._writer() as directory:
            data, version, accounts = _read(directory, self.path.name)
            if version != 1:
                raise RuntimeError("Local accounts are already in the current format")
            with _parent(backup_file, create=True) as backup_directory:
                _write(backup_directory, backup_file.name, data, create=True)
                restored_data, restored_version, restored_accounts = _read(backup_directory, backup_file.name)
                if restored_data != data or restored_version != version or restored_accounts != accounts:
                    raise RuntimeError("Migration backup verification failed")
            updated = {name: replace(account, session_revision=secrets.token_hex(32)) for name, account in accounts.items()}
            payload = _payload(updated)
            _parse(payload)
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
            if len(encoded) > _MAX_BYTES:
                raise RuntimeError("Migrated local account file exceeds its size limit")
            _write(directory, self.path.name, encoded)
        return receipt

    def rollback(self, backup_file: Path, workspace_file: Path, *, actor: str,
                 writers_stopped: bool = False) -> LocalAccountChange:
        """Restore exact version 1 bytes only after durably invalidating local sessions."""
        if self.guard is not None or not writers_stopped:
            raise RuntimeError("Stop the application and all account writers, then confirm the operator rollback")
        backup_file, workspace_file = Path(backup_file), Path(workspace_file)
        if backup_file == self.path or workspace_file in {self.path, backup_file}:
            raise RuntimeError("Use separate account, recovery and workspace files for rollback")
        receipt = self._receipt("rollback", "*", actor)
        with self._writer() as directory:
            _read(directory, self.path.name)
            with _parent(backup_file) as backup_directory:
                data, version, _ = _read(backup_directory, backup_file.name)
            if version != 1:
                raise RuntimeError("Migration rollback requires the exact version 1 recovery copy")
            # Two resources cannot commit atomically. Invalidate sessions first:
            # a later account write failure leaves safe revocation, never revival.
            _invalidate_local_sessions(workspace_file)
            _write(directory, self.path.name, data)
        return receipt

    def relocate(self, destination: Path, backup_file: Path, *, actor: str,
                 writers_stopped: bool = False) -> LocalAccountChange:
        """Move the sole canonical file during an explicit stopped-node change."""
        if self.guard is not None or not writers_stopped:
            raise RuntimeError("Stop the application and account writers, then confirm the operator relocation")
        destination, backup_file = Path(destination), Path(backup_file)
        if destination.name != "local-accounts.json" or destination.parent == self.path.parent:
            raise RuntimeError("Use local-accounts.json in a separate dedicated account directory")
        if backup_file in {self.path, destination} or backup_file.resolve(strict=False).is_relative_to(destination.parent.resolve(strict=False)):
            raise RuntimeError("Keep the recovery copy outside the dedicated account directory")
        receipt = self._receipt("relocate", "*", actor)
        with self._writer() as source_directory:
            _, version, accounts = _read(source_directory, self.path.name)
            if version != 2:
                raise RuntimeError("Migrate local accounts to version 2 before enabling browser changes")
            with _parent(destination, create=True) as target_directory:
                if stat.S_IMODE(os.fstat(target_directory).st_mode) != 0o700 or os.listdir(target_directory):
                    raise RuntimeError("The destination account directory must be empty and have mode 0700")
                # A rollback must not revive an unresolved pre-move session or
                # one created at the destination with the same workspace/key.
                recovery = {name: replace(account, session_revision=secrets.token_hex(32)) for name, account in accounts.items()}
                recovery_data = (json.dumps(_payload(recovery), indent=2, sort_keys=True) + "\n").encode()
                updated = {name: replace(account, session_revision=secrets.token_hex(32)) for name, account in accounts.items()}
                encoded = (json.dumps(_payload(updated), indent=2, sort_keys=True) + "\n").encode()
                if max(len(encoded), len(recovery_data)) > _MAX_BYTES:
                    raise RuntimeError("Relocated account file exceeds its size limit")
                with _parent(backup_file, create=True) as backup_directory:
                    _write(backup_directory, backup_file.name, recovery_data, create=True)
                    restored, _, _ = _read(backup_directory, backup_file.name)
                    if restored != recovery_data:
                        raise RuntimeError("Account relocation recovery copy could not be verified")
                _write(target_directory, destination.name, encoded, create=True)
                # Both the destination and recovery copy are durable before the
                # old canonical location is removed. The operator switches the
                # configuration before restarting any reader or writer.
                os.unlink(self.path.name, dir_fd=source_directory)
                os.fsync(source_directory)
        return receipt

    def create(self, username: str, display_name: str, password: str, *, administrator: bool = False,
               actor: str) -> LocalAccountChange:
        username, display_name = normalized_account(username, display_name)
        if not isinstance(administrator, bool):
            raise RuntimeError("Administrator role must be enabled or disabled")
        account = LocalAccount(username, display_name, hash_password(password),
                               frozenset({"administrator"}) if administrator else frozenset(), True, secrets.token_hex(32))
        def update(accounts: dict[str, LocalAccount]) -> None:
            if username in accounts:
                raise RuntimeError("That local username already exists")
            accounts[username] = account
        return self._change("create", username, actor, update)

    def _update(self, action: str, username: str, actor: str, **changes: object) -> LocalAccountChange:
        username = (username or "").strip().casefold()
        def update(accounts: dict[str, LocalAccount]) -> None:
            if username not in accounts:
                raise RuntimeError("Local account was not found")
            old = accounts[username]
            if action != "display-name":
                changes["session_revision"] = secrets.token_hex(32)
            accounts[username] = replace(old, **changes)
        return self._change(action, username, actor, update)

    def change_display_name(self, username: str, display_name: str, *, actor: str) -> LocalAccountChange:
        username, display_name = normalized_account(username, display_name)
        return self._update("display-name", username, actor, display_name=display_name)

    def change_password(self, username: str, password: str, *, actor: str) -> LocalAccountChange:
        return self._update("password", username, actor, password_hash=hash_password(password))

    def set_enabled(self, username: str, enabled: bool, *, actor: str) -> LocalAccountChange:
        if not isinstance(enabled, bool):
            raise RuntimeError("Account state must be enabled or disabled")
        return self._update("enable" if enabled else "disable", username, actor, enabled=enabled)

    def set_administrator(self, username: str, administrator: bool, *, actor: str) -> LocalAccountChange:
        if not isinstance(administrator, bool):
            raise RuntimeError("Administrator role must be enabled or disabled")
        return self._update("administrator" if administrator else "reviewer", username, actor,
                            roles=frozenset({"administrator"}) if administrator else frozenset())
