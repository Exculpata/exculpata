"""Persistent browser throttling signals, separate from authentication sessions."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from threading import RLock
from typing import Callable, Iterator

from .local_login_limiter import LoginThrottled, _Window

DEVICE_COOKIE = "__Host-exculpata_device"
DEVICE_MAX_AGE = 90 * 24 * 60 * 60
_TOKEN = re.compile(r"v1\.([0-9a-f]{32})\.([0-9a-f]{64})\Z")


def _private_file(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("identity device path is unsafe")
    metadata = path.stat()
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()):
        raise RuntimeError("identity device file must be owner-only with mode 0600")


def _create_private_file(path: Path, contents: bytes = b"") -> None:
    if path.is_symlink():
        raise RuntimeError("identity device path is unsafe")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _private_file(path)
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    _private_file(path)


@dataclass
class DeviceAttempt:
    device_digest: str | None = None
    chain_id: str | None = None
    predecessor: str | None = None
    replayed: bool = False
    outcome: str | None = None
    revision: tuple[str, str] | None = None
    cookie: str | None = field(default=None, repr=False)


class LocalLoginResult(tuple):
    """Keep the existing context/token pair API with request-local device metadata."""

    def __new__(cls, session, device: DeviceAttempt):
        result = super().__new__(cls, session)
        result.device_cookie = device.cookie
        result.device_replayed = device.replayed
        return result


def set_device_cookie(response, value: str | None, *, secure: bool) -> None:
    if secure and value is not None:
        response.set_cookie(DEVICE_COOKIE, value, secure=True, httponly=True,
                            samesite="strict", path="/", max_age=DEVICE_MAX_AGE)


class LocalLoginDevices:
    capacity = 10_000
    failure_limit = 10
    lifetime_failure_limit = 30

    def __init__(self, runtime_dir: Path):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if runtime_dir.is_symlink():
            raise RuntimeError("identity device directory is unsafe")
        key_path = runtime_dir / "identity-device.key"
        _create_private_file(key_path, secrets.token_bytes(32))
        self._key = key_path.read_bytes()
        if len(self._key) != 32:
            raise RuntimeError("identity device key is invalid")
        self.path = runtime_dir / "login-devices.sqlite3"
        _create_private_file(self.path)
        self._lock = RLock()
        self.windows: dict[str, _Window] = {}
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS devices (
                device_digest TEXT PRIMARY KEY, chain_id TEXT NOT NULL,
                username_digest TEXT NOT NULL, session_revision TEXT NOT NULL,
                account_binding TEXT NOT NULL, created REAL NOT NULL, last_used REAL NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL CHECK(state IN ('active', 'rotated', 'revoked'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS devices_chain ON devices(chain_id)")

    @contextmanager
    def _transaction(self):
        _private_file(self.path)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.is_symlink():
                raise RuntimeError("identity device path is unsafe")
        db = None
        try:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error:
            if db is not None:
                db.rollback()
            raise RuntimeError("identity device registry is unavailable") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _digest(self, purpose: str, value: str) -> str:
        return hmac.new(self._key, f"{purpose}|{value}".encode(), hashlib.sha256).hexdigest()

    def username_digest(self, username: str) -> str:
        return self._digest("device-username", username.strip().casefold())

    @staticmethod
    def _entries(cookie: str | None) -> list[str]:
        if not isinstance(cookie, str):
            return []
        return [value for value in cookie[:505].split("~", 5)[:5] if _TOKEN.fullmatch(value)]

    def _row(self, db, token: str):
        match = _TOKEN.fullmatch(token)
        if match is None:
            return None
        device_id, signature = match.groups()
        row = db.execute("SELECT * FROM devices WHERE device_digest = ?",
                         (self._digest("device-id", device_id),)).fetchone()
        if row is None:
            return None
        expected = self._digest("device-cookie", device_id + "|" + row["username_digest"])
        return row if hmac.compare_digest(signature, expected) else None

    def _select(self, db, cookie, username, revision, now) -> DeviceAttempt:
        result = DeviceAttempt()
        for token in self._entries(cookie):
            row = self._row(db, token)
            if row is None:
                continue
            if row["state"] == "rotated":
                db.execute("UPDATE devices SET state = 'revoked' WHERE chain_id = ?",
                           (row["chain_id"],))
                result.replayed = True
            elif (row["state"] == "active" and row["username_digest"] == username
                  and result.predecessor is None and row["last_used"] > now.timestamp() - DEVICE_MAX_AGE):
                result.predecessor = row["device_digest"]
                result.chain_id = row["chain_id"]
                if revision is not None and (row["session_revision"], row["account_binding"]) == revision:
                    result.device_digest = row["device_digest"]
        if result.replayed:
            result.device_digest = None
            if result.predecessor is not None:
                state = db.execute("SELECT state FROM devices WHERE device_digest = ?",
                                   (result.predecessor,)).fetchone()["state"]
                if state != "active":
                    result.predecessor = result.chain_id = None
        return result

    def _prune(self, db, now: datetime, *, make_room: bool = False) -> None:
        rows = db.execute("SELECT device_digest, last_used, state FROM devices "
                          "ORDER BY (state = 'active'), last_used, created").fetchall()
        remaining = len(rows)
        present = {row["device_digest"] for row in rows}
        for key in list(self.windows):
            if key not in present and not self.windows[key].pending:
                del self.windows[key]
        for row in rows:
            key = row["device_digest"]
            if self.windows.get(key, _Window()).pending:
                continue
            if (row["last_used"] <= now.timestamp() - DEVICE_MAX_AGE
                    or remaining > self.capacity - int(make_room)):
                db.execute("DELETE FROM devices WHERE device_digest = ?", (key,))
                self.windows.pop(key, None)
                remaining -= 1
        if make_room and remaining >= self.capacity:
            raise RuntimeError("identity device registry is busy")

    def _issue(self, db, attempt, cookie, username, now):
        retained = []
        seen = {username}
        for token in self._entries(cookie):
            row = self._row(db, token)
            if (row is not None and row["username_digest"] not in seen and row["state"] == "active"
                    and row["last_used"] > now.timestamp() - DEVICE_MAX_AGE):
                retained.append(token)
                seen.add(row["username_digest"])
        if attempt.predecessor is not None:
            row = db.execute("SELECT state FROM devices WHERE device_digest = ?",
                             (attempt.predecessor,)).fetchone()
            if row is None or row["state"] != "active":
                if row is not None and row["state"] == "rotated":
                    attempt.replayed = True
                    db.execute("UPDATE devices SET state = 'revoked' WHERE chain_id = ?",
                               (attempt.chain_id,))
                return
            db.execute("UPDATE devices SET state = 'rotated', failures = 0 WHERE device_digest = ?",
                       (attempt.predecessor,))
        self._prune(db, now, make_room=True)
        device_id = secrets.token_hex(16)
        chain = attempt.chain_id or secrets.token_hex(16)
        db.execute("INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'active')",
                   (self._digest("device-id", device_id), chain, username,
                    *attempt.revision, now.timestamp(), now.timestamp()))
        signature = self._digest("device-cookie", device_id + "|" + username)
        attempt.cookie = "~".join([f"v1.{device_id}.{signature}", *retained[:4]])

    @contextmanager
    def attempt(self, cookie: str | None, username: str, revision: tuple[str, str] | None,
                clock: Callable[[], datetime]) -> Iterator[DeviceAttempt]:
        result = DeviceAttempt()
        window = None
        if self._entries(cookie):
            with self._lock:
                with self._transaction() as db:
                    now = clock()
                    result = self._select(db, cookie, username, revision, now)
                    if result.device_digest is not None:
                        if result.device_digest not in self.windows and len(self.windows) >= self.capacity:
                            self._prune(db, now)
                            if len(self.windows) >= self.capacity:
                                raise LoginThrottled(900, now)
                        window = self.windows.setdefault(result.device_digest, _Window())
                        window.prune(now)
                        row = db.execute("SELECT failures FROM devices WHERE device_digest = ?",
                                         (result.device_digest,)).fetchone()
                        limit = min(self.failure_limit, self.lifetime_failure_limit - row["failures"])
                        if window.count >= self.failure_limit or window.pending >= limit:
                            delay = (window.retry_seconds(now, self.failure_limit)
                                     if window.count >= self.failure_limit else 900)
                            raise LoginThrottled(delay, now)
                        db.execute("UPDATE devices SET last_used = ? WHERE device_digest = ?",
                                   (now.timestamp(), result.device_digest))
                if window is not None:
                    window.pending += 1
        try:
            yield result
        finally:
            with self._lock:
                now = clock()
                if window is not None:
                    window.pending -= 1
                    window.prune(now)
                    if result.outcome == "failure":
                        window.failures.append(now)
                    elif result.outcome == "success":
                        window.failures.clear()
                if result.device_digest is not None or result.outcome == "success":
                    with self._transaction() as db:
                        if result.device_digest is not None and result.outcome == "failure":
                            db.execute("UPDATE devices SET failures = failures + 1, "
                                       "state = CASE WHEN failures + 1 >= ? THEN 'revoked' ELSE state END "
                                       "WHERE device_digest = ?",
                                       (self.lifetime_failure_limit, result.device_digest))
                        if result.outcome == "success":
                            self._issue(db, result, cookie, username, now)
                    if result.outcome == "success" and result.cookie is None:
                        raise RuntimeError("identity device changed during sign-in")
