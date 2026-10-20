"""Persistent browser throttling signals, separate from authentication sessions."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from threading import RLock
import time
from typing import Callable, Iterator

from .local_login_limiter import LoginThrottled, _Window

DEVICE_COOKIE = "__Host-exculpata_device"
DEVICE_MAX_AGE = 90 * 24 * 60 * 60
_DEVICE_KEY_FILENAME = ".".join(("identity-device", "key"))
_TOKEN = re.compile(r"v1\.([0-9a-f]{32})\.([0-9a-f]{64})\Z")
LOGGER = logging.getLogger(__name__)


def _boot_clock() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError, ValueError):
        return time.monotonic()


class DeviceRegistryUnavailable(RuntimeError):
    pass


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
    per_user_active_limit = 20
    per_parent_unadmitted_limit = 5
    rotation_grace_seconds = 60
    pending_timeout_seconds = 900

    def __init__(self, runtime_dir: Path):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if runtime_dir.is_symlink():
            raise RuntimeError("identity device directory is unsafe")
        key_path = runtime_dir / _DEVICE_KEY_FILENAME
        _create_private_file(key_path, secrets.token_bytes(32))
        self._key = key_path.read_bytes()
        if len(self._key) != 32:
            raise RuntimeError("identity device key is invalid")
        self.path = runtime_dir / "login-devices.sqlite3"
        _create_private_file(self.path)
        self._lock = RLock()
        self._successful_releases: list[tuple[str, int]] = []
        self._clock_origin: tuple[float, float] | None = None
        try:
            self._clock_epoch = Path("/proc/sys/kernel/random/boot_id").read_text().strip() or secrets.token_hex(32)
        except OSError:
            self._clock_epoch = secrets.token_hex(32)
        self.windows: dict[str, _Window] = {}
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS devices (
                device_digest TEXT PRIMARY KEY, chain_id TEXT NOT NULL,
                username_digest TEXT NOT NULL, session_revision TEXT NOT NULL,
                account_binding TEXT NOT NULL, created REAL NOT NULL, last_used REAL NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL CHECK(state IN ('active', 'rotated', 'revoked')),
                admitted INTEGER NOT NULL DEFAULT 1,
                successor_used INTEGER NOT NULL DEFAULT 1,
                parent_digest TEXT
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(devices)")}
            for name in ("admitted", "successor_used"):
                if name not in columns:
                    db.execute(f"ALTER TABLE devices ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1")
            if "parent_digest" not in columns:
                db.execute("ALTER TABLE devices ADD COLUMN parent_digest TEXT")
            for name, definition in (
                ("pending", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_until", "REAL NOT NULL DEFAULT 0"),
                ("pending_generation", "INTEGER NOT NULL DEFAULT 0"),
                ("rotation_tick", "REAL"),
                ("rotation_epoch", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE devices ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS devices_chain ON devices(chain_id)")
            db.execute("CREATE TABLE IF NOT EXISTS device_clock "
                       "(singleton INTEGER PRIMARY KEY CHECK(singleton = 1), high_water REAL NOT NULL)")

    @contextmanager
    def _transaction(self):
        with self._lock:
            with self._registry_transaction() as db:
                for device_digest, generation in self._successful_releases:
                    db.execute("UPDATE devices SET pending = pending - 1 "
                               "WHERE device_digest = ? AND pending_generation = ? AND pending > 0",
                               (device_digest, generation))
                yield db
            self._successful_releases.clear()

    @contextmanager
    def _registry_transaction(self):
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
            raise DeviceRegistryUnavailable("identity device registry is unavailable") from None
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

    def _maintain(self, db, now):
        timestamp, tick = now.timestamp(), _boot_clock()
        if self._clock_origin is None:
            self._clock_origin = (timestamp, tick)
        origin, started = self._clock_origin
        previous = db.execute("SELECT high_water FROM device_clock WHERE singleton = 1").fetchone()
        high_water = max(timestamp, previous["high_water"] if previous else timestamp)
        db.execute("INSERT INTO device_clock VALUES (1, ?) ON CONFLICT(singleton) "
                   "DO UPDATE SET high_water = excluded.high_water", (high_water,))
        db.execute("UPDATE devices SET successor_used = 1 WHERE state = 'rotated' "
                   "AND ((? < ? AND NOT (rotation_epoch IS ? AND rotation_tick IS NOT NULL "
                   "AND rotation_tick <= ? AND ? - rotation_tick <= ?)) "
                   "OR last_used + ? < ? OR rotation_tick IS NULL "
                   "OR rotation_epoch IS NULL OR rotation_epoch != ? "
                   "OR rotation_tick > ? OR rotation_tick + ? < ?)",
                   (timestamp, high_water, self._clock_epoch, tick, tick, self.rotation_grace_seconds,
                    self.rotation_grace_seconds, high_water,
                    self._clock_epoch, tick, self.rotation_grace_seconds, tick))
        db.execute("UPDATE devices SET failures = failures + pending, pending = 0, "
                   "pending_generation = pending_generation + 1 "
                   "WHERE pending > 0 AND pending_until <= ?",
                   (max(high_water, origin + tick - started),))
        db.execute("UPDATE devices SET state = 'revoked' WHERE chain_id IN "
                   "(SELECT chain_id FROM devices WHERE failures >= ?)", (self.lifetime_failure_limit,))

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

    def _rotation_target(self, db, row, now):
        if db.execute("SELECT 1 FROM devices WHERE chain_id = ? AND state = 'revoked'",
                      (row["chain_id"],)).fetchone():
            return None
        if row["state"] == "active":
            return row
        tick = _boot_clock()
        boot_grace = (row["rotation_epoch"] == self._clock_epoch
                      and row["rotation_tick"] is not None
                      and 0 <= tick - row["rotation_tick"] <= self.rotation_grace_seconds)
        previous = db.execute("SELECT high_water FROM device_clock WHERE singleton = 1").fetchone()
        high_water = max(now.timestamp(), previous["high_water"] if previous else now.timestamp())
        wall_grace = high_water <= row["last_used"] + self.rotation_grace_seconds
        if row["state"] == "rotated" and not row["successor_used"] and boot_grace and wall_grace:
            return db.execute("SELECT * FROM devices WHERE chain_id = ? AND state = 'active' "
                              "AND admitted = 0",
                              (row["chain_id"],)).fetchone()
        return None

    def _select(self, db, cookie, username, revision, now) -> DeviceAttempt:
        result = DeviceAttempt()
        for token in self._entries(cookie):
            row = self._row(db, token)
            if row is None:
                continue
            target = self._rotation_target(db, row, now)
            if row["state"] == "rotated" and target is None:
                db.execute("UPDATE devices SET state = 'revoked' WHERE chain_id = ?",
                           (row["chain_id"],))
                result.replayed = True
            elif (target is not None and row["username_digest"] == username
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
                if state == "revoked":
                    result.predecessor = result.chain_id = None
        return result

    def _prune(self, db, now: datetime, *, make_room: bool = False) -> None:
        rows = db.execute("SELECT device_digest, last_used, state, pending FROM devices "
                          "ORDER BY (state = 'active'), last_used, created").fetchall()
        remaining = len(rows)
        present = {row["device_digest"] for row in rows}
        for key in list(self.windows):
            if key not in present and not self.windows[key].pending:
                del self.windows[key]
        for row in rows:
            key = row["device_digest"]
            if row["pending"] or self.windows.get(key, _Window()).pending:
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
            row = db.execute("SELECT * FROM devices WHERE device_digest = ?",
                             (attempt.predecessor,)).fetchone()
            target = self._rotation_target(db, row, now) if row is not None else None
            if target is None:
                if row is not None and row["state"] == "rotated":
                    attempt.replayed = True
                    db.execute("UPDATE devices SET state = 'revoked' WHERE chain_id = ?",
                               (attempt.chain_id,))
                return
            db.execute("UPDATE devices SET state = 'rotated', failures = 0, successor_used = 0, "
                       "admitted = CASE WHEN device_digest = ? THEN 1 ELSE admitted END, "
                       "rotation_tick = CASE WHEN state = 'active' THEN ? ELSE rotation_tick END, "
                       "rotation_epoch = CASE WHEN state = 'active' THEN ? ELSE rotation_epoch END, "
                       "last_used = CASE WHEN state = 'active' THEN ? ELSE last_used END "
                       "WHERE device_digest IN (?, ?)",
                       (attempt.predecessor, _boot_clock(), self._clock_epoch, now.timestamp(), attempt.predecessor,
                        target["device_digest"] if target["parent_digest"] is None else attempt.predecessor))
            if row["parent_digest"] is not None:
                db.execute("DELETE FROM devices WHERE parent_digest = ? AND state = 'active' "
                           "AND device_digest != ?", (row["parent_digest"], attempt.predecessor))
            db.execute("DELETE FROM devices WHERE device_digest IN "
                       "(SELECT device_digest FROM devices WHERE parent_digest = ? AND admitted = 0 "
                       "ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?)",
                       (attempt.predecessor, self.per_parent_unadmitted_limit - 1))
        excess = db.execute(
            "SELECT chain_id FROM (SELECT chain_id, last_used, created, rowid AS ordinal, "
            "ROW_NUMBER() OVER (PARTITION BY chain_id ORDER BY last_used DESC, created DESC, rowid DESC) "
            "AS position FROM devices WHERE username_digest = ? AND state = 'active') "
            "WHERE position = 1 ORDER BY last_used DESC, created DESC, ordinal DESC LIMIT -1 OFFSET ?",
            (username, self.per_user_active_limit - 1)).fetchall()
        for row in excess:
            db.execute("UPDATE devices SET state = 'revoked' WHERE chain_id = ?",
                       (row["chain_id"],))
        if any(row["chain_id"] == attempt.chain_id for row in excess):
            return
        self._prune(db, now, make_room=True)
        device_id = secrets.token_hex(16)
        chain = attempt.chain_id or secrets.token_hex(16)
        db.execute("INSERT INTO devices (device_digest, chain_id, username_digest, session_revision, "
                   "account_binding, created, last_used, failures, state, admitted, successor_used, parent_digest) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'active', 0, 1, ?)",
                   (self._digest("device-id", device_id), chain, username,
                    *attempt.revision, now.timestamp(), now.timestamp(), attempt.predecessor))
        signature = self._digest("device-cookie", device_id + "|" + username)
        attempt.cookie = "~".join([f"v1.{device_id}.{signature}", *retained[:4]])

    def _admit(self, cookie, username, revision, clock):
        result = DeviceAttempt()
        window = None
        generation = None
        if self._entries(cookie):
            admission_completed = False
            try:
                with self._lock:
                    with self._transaction() as db:
                        now = clock()
                        self._maintain(db, now)
                        result = self._select(db, cookie, username, revision, now)
                        if result.device_digest is not None:
                            if result.device_digest not in self.windows and len(self.windows) >= self.capacity:
                                self._prune(db, now)
                                if len(self.windows) >= self.capacity:
                                    raise LoginThrottled(900, now)
                            window = self.windows.setdefault(result.device_digest, _Window())
                            window.prune(now)
                            row = db.execute("SELECT * FROM devices WHERE device_digest = ?",
                                             (result.device_digest,)).fetchone()
                            if (window.count >= self.failure_limit
                                    or row["failures"] + row["pending"] >= self.lifetime_failure_limit):
                                delay = (window.retry_seconds(now, self.failure_limit)
                                         if window.count >= self.failure_limit else 900)
                                raise LoginThrottled(delay, now)
                            db.execute("UPDATE devices SET successor_used = 1 WHERE chain_id = ? "
                                       "AND state = 'rotated' AND admitted = 1 AND EXISTS "
                                       "(SELECT 1 FROM devices WHERE device_digest = ? AND state = 'active')",
                                       (result.chain_id, result.device_digest))
                            generation = row["pending_generation"]
                            db.execute("UPDATE devices SET admitted = 1, pending = pending + 1, "
                                       "pending_until = CASE WHEN pending = 0 THEN ? ELSE pending_until END, "
                                       "last_used = CASE WHEN state = 'active' THEN ? ELSE last_used END "
                                       "WHERE device_digest = ?",
                                       (now.timestamp() + self.pending_timeout_seconds,
                                        now.timestamp(), result.device_digest))
                        admission_completed = True
                    if window is not None:
                        window.pending += 1
            except DeviceRegistryUnavailable:
                if admission_completed:
                    raise RuntimeError("identity device admission could not commit") from None
                raise
        return result, window, generation

    def _release_successful_attempt(self, result, generation):
        if result.device_digest is not None:
            self._successful_releases.append((result.device_digest, generation))
            for _ in range(2):
                try:
                    with self._transaction():
                        pass
                except (OSError, RuntimeError):
                    continue
                break
        LOGGER.warning("Local login device completion failed; sign-in succeeded without a device cookie.")

    @contextmanager
    def attempt(self, cookie: str | None, username: str, revision: tuple[str, str] | None,
                clock: Callable[[], datetime]) -> Iterator[DeviceAttempt]:
        try:
            result, window, generation = self._admit(cookie, username, revision, clock)
        except DeviceRegistryUnavailable:
            LOGGER.warning("Local login device registry is unavailable; using the untrusted lane.")
            result, window, generation = DeviceAttempt(), None, None
            cookie = None
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
                    replayed_before_completion = result.replayed
                    for completion in range(2):
                        try:
                            with self._transaction() as db:
                                self._maintain(db, now)
                                if result.device_digest is not None:
                                    db.execute("UPDATE devices SET pending = pending - 1, failures = failures + ? "
                                               "WHERE device_digest = ? AND pending_generation = ? AND pending > 0",
                                               (int(result.outcome == "failure"), result.device_digest, generation))
                                if result.device_digest is not None and result.outcome == "failure":
                                    db.execute("UPDATE devices SET state = 'revoked' WHERE chain_id = ? "
                                               "AND EXISTS (SELECT 1 FROM devices WHERE device_digest = ? "
                                               "AND failures >= ?)",
                                               (result.chain_id, result.device_digest, self.lifetime_failure_limit))
                                if result.outcome == "success":
                                    self._issue(db, result, cookie, username, now)
                        except (OSError, RuntimeError):
                            if result.outcome != "success":
                                raise
                            result.cookie = None
                            result.replayed = replayed_before_completion
                            if completion == 1:
                                self._release_successful_attempt(result, generation)
                        else:
                            break
