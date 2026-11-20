"""SQLite durable state for transcription v2.

Every operation opens its own SQLite connection.  Queue claims use
``BEGIN IMMEDIATE`` plus a conditional update, making a claim atomic across
threads and independent worker processes.  The event schema is deliberately
content-free: it has no message or JSON payload column.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Sequence

from .domain import (
    ConcurrencyConflictError,
    EventType,
    IdentityMethod,
    IdentityStatus,
    InvalidTransitionError,
    Job,
    JobEvent,
    JobFile,
    JobStatus,
    NotFoundError,
    PipelineStage,
    RecoverySummary,
    RetryLimitError,
    SegmentDraft,
    SpeakerMapping,
    StageStatus,
    StoreError,
    TERMINAL_JOB_STATUSES,
    TranscriptSegment,
    TranscriptionOptions,
)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSET = object()
_PROFILES = frozenset({"balanced", "high_accuracy", "fast"})
_DELETION_REASON_CODES = frozenset(
    {"active_lifetime_expired", "owner_deleted", "retention_expired"}
)

_STAGE_ORDER = {stage: index for index, stage in enumerate(PipelineStage)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | str | None = None) -> str:
    if value is None:
        parsed = _now()
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601") from exc
    else:
        raise TypeError("timestamp must be datetime, ISO-8601 string, or None")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _timestamp_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _expiry_from(value: str, hours: int) -> str:
    return _timestamp(_timestamp_datetime(value) + timedelta(hours=hours))


def _validate_identifier(value: str, field_name: str = "identifier") -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} must be 1-64 ASCII letters, numbers, underscores, or hyphens"
        )
    return value


def _validate_code(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a short machine-readable code")
    return value


def _validate_owner_key(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 256:
        raise ValueError("owner_key must be an opaque non-empty string up to 256 characters")
    return value


def _validate_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return result


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("relative_path is required")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("relative_path must be a normalized relative POSIX path")
    return path.as_posix()


class SQLiteJobStore:
    """Durable SQLite repository and atomic work queue."""

    SCHEMA_VERSION = 1

    @staticmethod
    def has_unfinished_jobs_offline(path: Path) -> bool:
        """Inspect an existing store without running migrations or hardening files."""
        terminal = tuple(status.value for status in TERMINAL_JOB_STATUSES)
        slots = ",".join("?" for _ in terminal)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute(
                f"SELECT 1 FROM jobs WHERE status NOT IN ({slots}) LIMIT 1", terminal
            ).fetchone() is not None

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = 10_000,
        failed_retention_hours: int = 1,
    ) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("a durable on-disk SQLite path is required")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if (
            not isinstance(failed_retention_hours, int)
            or isinstance(failed_retention_hours, bool)
            or not 1 <= failed_retention_hours <= 24
        ):
            raise ValueError("failed_retention_hours must be between 1 and 24")
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path.absolute()
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.failed_retention_hours = failed_retention_hours
        self._initialize()
        self._harden_database_files()
        self._reconcile_all_batch_retention()

    def _harden_database_files(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        try:
            yield connection
        finally:
            self._harden_database_files()
            connection.close()

    @contextmanager
    def _transaction(
        self, connection: sqlite3.Connection, *, immediate: bool = False
    ) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _initialize(self) -> None:
        job_statuses = ",".join(f"'{item.value}'" for item in JobStatus)
        stages = ",".join(f"'{item.value}'" for item in PipelineStage)
        stage_statuses = ",".join(f"'{item.value}'" for item in StageStatus)
        identity_statuses = ",".join(f"'{item.value}'" for item in IdentityStatus)
        identity_methods = ",".join(f"'{item.value}'" for item in IdentityMethod)
        schema = f"""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            owner_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ({job_statuses})),
            stage TEXT NOT NULL CHECK (stage IN ({stages})),
            stage_status TEXT NOT NULL CHECK (stage_status IN ({stage_statuses})),
            profile TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
            available_at TEXT NOT NULL,
            worker_id TEXT,
            claimed_at TEXT,
            heartbeat_at TEXT,
            cancel_requested_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            error_code TEXT,
            options_json TEXT NOT NULL,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS jobs_claim_idx
            ON jobs(status, available_at, priority DESC, created_at, id);
        CREATE INDEX IF NOT EXISTS jobs_owner_idx
            ON jobs(owner_key, created_at DESC);
        CREATE INDEX IF NOT EXISTS jobs_expiry_idx
            ON jobs(status, expires_at);

        CREATE TABLE IF NOT EXISTS job_files (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            original_name TEXT NOT NULL,
            safe_name TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            media_type TEXT,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            created_at TEXT NOT NULL,
            UNIQUE (id, job_id),
            UNIQUE (job_id, relative_path)
        );
        CREATE INDEX IF NOT EXISTS job_files_sha_idx ON job_files(sha256);

        CREATE TABLE IF NOT EXISTS job_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            stage TEXT,
            job_status TEXT,
            stage_status TEXT,
            worker_id TEXT,
            reason_code TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS job_events_job_idx
            ON job_events(job_id, sequence);

        CREATE TABLE IF NOT EXISTS segments (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            file_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
            end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
            speaker_key TEXT,
            model_text TEXT NOT NULL,
            edited_text TEXT,
            translated_text TEXT,
            confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            overlap INTEGER NOT NULL DEFAULT 0 CHECK (overlap IN (0, 1)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (file_id, ordinal),
            FOREIGN KEY (file_id, job_id)
                REFERENCES job_files(id, job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS segments_job_order_idx
            ON segments(job_id, file_id, ordinal);

        CREATE TABLE IF NOT EXISTS speaker_mappings (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            speaker_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            identity_status TEXT NOT NULL CHECK (identity_status IN ({identity_statuses})),
            identity_method TEXT NOT NULL CHECK (identity_method IN ({identity_methods})),
            confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (job_id, speaker_key)
        );
        """
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > self.SCHEMA_VERSION:
                raise StoreError(
                    f"database schema {current} is newer than supported {self.SCHEMA_VERSION}"
                )
            connection.executescript(schema)
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        try:
            options = TranscriptionOptions.from_json(row["options_json"])
        except ValueError as exc:
            raise StoreError("job contains invalid persisted options") from exc
        return Job(
            id=row["id"],
            owner_key=row["owner_key"],
            status=JobStatus(row["status"]),
            stage=PipelineStage(row["stage"]),
            stage_status=StageStatus(row["stage_status"]),
            profile=row["profile"],
            priority=int(row["priority"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            available_at=row["available_at"],
            worker_id=row["worker_id"],
            claimed_at=row["claimed_at"],
            heartbeat_at=row["heartbeat_at"],
            cancel_requested_at=row["cancel_requested_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_code=row["error_code"],
            options=options,
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _file_from_row(row: sqlite3.Row) -> JobFile:
        return JobFile(
            id=row["id"],
            job_id=row["job_id"],
            original_name=row["original_name"],
            safe_name=row["safe_name"],
            relative_path=row["relative_path"],
            media_type=row["media_type"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            sequence=int(row["sequence"]),
            job_id=row["job_id"],
            event_type=EventType(row["event_type"]),
            stage=PipelineStage(row["stage"]) if row["stage"] else None,
            job_status=JobStatus(row["job_status"]) if row["job_status"] else None,
            stage_status=StageStatus(row["stage_status"])
            if row["stage_status"]
            else None,
            worker_id=row["worker_id"],
            reason_code=row["reason_code"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _segment_from_row(row: sqlite3.Row) -> TranscriptSegment:
        return TranscriptSegment(
            id=row["id"],
            job_id=row["job_id"],
            file_id=row["file_id"],
            ordinal=int(row["ordinal"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            speaker_key=row["speaker_key"],
            model_text=row["model_text"],
            edited_text=row["edited_text"],
            translated_text=row["translated_text"],
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            overlap=bool(row["overlap"]),
            revision=int(row["revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _speaker_from_row(row: sqlite3.Row) -> SpeakerMapping:
        return SpeakerMapping(
            job_id=row["job_id"],
            speaker_key=row["speaker_key"],
            display_name=row["display_name"],
            identity_status=IdentityStatus(row["identity_status"]),
            identity_method=IdentityMethod(row["identity_method"]),
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            revision=int(row["revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _require_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError("job not found")
        return row

    @staticmethod
    def _require_owner(row: sqlite3.Row, owner_key: str) -> None:
        owner_key = _validate_owner_key(owner_key)
        if not secrets.compare_digest(row["owner_key"], owner_key):
            # Do not reveal whether another owner has a job with this identifier.
            raise NotFoundError("job not found")

    @staticmethod
    def _row_options(row: sqlite3.Row) -> TranscriptionOptions:
        try:
            return TranscriptionOptions.from_json(row["options_json"])
        except ValueError as exc:
            raise StoreError("job contains invalid persisted options") from exc

    @classmethod
    def _batch_rows_locked(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_batch: bool = True,
    ) -> list[sqlite3.Row]:
        """Return one owner-scoped persisted batch, or only the given job."""

        options = cls._row_options(row)
        if not include_batch or options.batch_id is None:
            return [row]
        candidates = connection.execute(
            "SELECT * FROM jobs WHERE owner_key = ? ORDER BY created_at, id",
            (row["owner_key"],),
        ).fetchall()
        members = [
            candidate
            for candidate in candidates
            if cls._row_options(candidate).batch_id == options.batch_id
        ]
        return members or [row]

    @classmethod
    def _coordinate_batch_retention_locked(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> list[sqlite3.Row]:
        """Hold child expiry until the batch is terminal, then share one TTL."""

        options = cls._row_options(row)
        if options.batch_id is None:
            return [row]
        members = cls._batch_rows_locked(connection, row)
        # A failed filesystem purge deliberately leaves every row reserved at
        # its existing deadline. Do not turn a cleanup retry into a new
        # retention window merely because CANCELED is a terminal status.
        if any(member["error_code"] in _DELETION_REASON_CODES for member in members):
            return members
        identifiers = [member["id"] for member in members]
        placeholders = ",".join("?" for _ in identifiers)
        if any(JobStatus(member["status"]) not in TERMINAL_JOB_STATUSES for member in members):
            connection.execute(
                f"UPDATE jobs SET expires_at = NULL WHERE id IN ({placeholders})",
                identifiers,
            )
        else:
            retention_hours = min(
                cls._row_options(member).retention_hours for member in members
            )
            final_terminal_at = max(
                member["finished_at"] or member["updated_at"] for member in members
            )
            shared_expiry = _expiry_from(final_terminal_at, retention_hours)
            connection.execute(
                f"UPDATE jobs SET expires_at = ? WHERE id IN ({placeholders})",
                (shared_expiry, *identifiers),
            )
        return [
            cls._require_job(connection, identifier) for identifier in identifiers
        ]

    @staticmethod
    def _batch_state_snapshot_locked(
        connection: sqlite3.Connection,
        members: Sequence[sqlite3.Row],
    ) -> str:
        """Hash content-free job/review revisions for conditional batch purge."""

        digest = hashlib.sha256()
        for member in sorted(members, key=lambda item: (item["created_at"], item["id"])):
            for value in (
                member["id"],
                member["status"],
                member["stage"],
                member["stage_status"],
                str(member["attempt"]),
                member["updated_at"],
                member["finished_at"] or "",
                member["error_code"] or "",
            ):
                digest.update(str(value).encode("utf-8"))
                digest.update(b"\0")
            segments = connection.execute(
                """
                SELECT id, revision, updated_at FROM segments
                WHERE job_id = ? ORDER BY id
                """,
                (member["id"],),
            ).fetchall()
            for segment in segments:
                digest.update(
                    f"segment\0{segment['id']}\0{segment['revision']}\0{segment['updated_at']}\0".encode(
                        "utf-8"
                    )
                )
            speakers = connection.execute(
                """
                SELECT speaker_key, revision, updated_at FROM speaker_mappings
                WHERE job_id = ? ORDER BY speaker_key
                """,
                (member["id"],),
            ).fetchall()
            for speaker in speakers:
                digest.update(
                    f"speaker\0{speaker['speaker_key']}\0{speaker['revision']}\0{speaker['updated_at']}\0".encode(
                        "utf-8"
                    )
                )
        return digest.hexdigest()

    def _reconcile_all_batch_retention(self) -> None:
        """Repair batch deadlines after a process restart or interrupted upgrade."""

        with self._connect() as connection, self._transaction(connection, immediate=True):
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY owner_key, created_at, id"
            ).fetchall()
            reconciled: set[tuple[str, str]] = set()
            for row in rows:
                options = self._row_options(row)
                if options.batch_id is None:
                    continue
                key = (row["owner_key"], options.batch_id)
                if key in reconciled:
                    continue
                self._coordinate_batch_retention_locked(connection, row)
                reconciled.add(key)

    @staticmethod
    def _mark_deletion_group_locked(
        connection: sqlite3.Connection,
        members: Sequence[sqlite3.Row],
        *,
        reason_code: str,
        now: str,
    ) -> tuple[str, ...]:
        reason_code = _validate_code(reason_code, "reason_code") or "owner_deleted"
        identifiers = tuple(member["id"] for member in members)
        placeholders = ",".join("?" for _ in identifiers)
        connection.execute(
            f"""
            UPDATE jobs
            SET status = ?, stage_status = ?, worker_id = NULL,
                claimed_at = NULL, heartbeat_at = NULL,
                finished_at = COALESCE(finished_at, ?), expires_at = ?,
                error_code = ?, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (
                JobStatus.CANCELED.value,
                StageStatus.CANCELED.value,
                now,
                now,
                reason_code,
                now,
                *identifiers,
            ),
        )
        return identifiers

    def _delete_reserved_group(
        self,
        identifiers: Sequence[str],
        owner_key: str,
        *,
        reason_code: str,
        delete_files: Callable[[str], Any] | None,
    ) -> list[str]:
        first_error: Exception | None = None
        if delete_files is not None:
            for identifier in identifiers:
                try:
                    delete_files(identifier)
                except Exception as exc:  # cleanup retry retains every DB row
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error

        placeholders = ",".join("?" for _ in identifiers)
        with self._connect() as connection, self._transaction(connection, immediate=True):
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE id IN ({placeholders})",
                tuple(identifiers),
            ).fetchall()
            if not rows:
                return []
            for row in rows:
                self._require_owner(row, owner_key)
                if (
                    JobStatus(row["status"]) is not JobStatus.CANCELED
                    or row["error_code"] != reason_code
                ):
                    raise ConcurrencyConflictError(
                        "job deletion reservation changed before hard deletion"
                    )
            connection.execute(
                f"DELETE FROM jobs WHERE id IN ({placeholders})",
                tuple(identifiers),
            )
        self.checkpoint_wal()
        return [row["id"] for row in rows]

    @staticmethod
    def _require_worker(row: sqlite3.Row, worker_id: str) -> None:
        worker_id = _validate_code(worker_id, "worker_id")
        if row["worker_id"] != worker_id:
            raise ConcurrencyConflictError("job is not claimed by this worker")

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_type: EventType,
        stage: PipelineStage | None,
        job_status: JobStatus | None,
        stage_status: StageStatus | None,
        worker_id: str | None = None,
        reason_code: str | None = None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events (
                job_id, event_type, stage, job_status, stage_status,
                worker_id, reason_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                event_type.value,
                stage.value if stage else None,
                job_status.value if job_status else None,
                stage_status.value if stage_status else None,
                worker_id,
                _validate_code(reason_code, "reason_code"),
                created_at,
            ),
        )

    def create_job(
        self,
        *,
        owner_key: str,
        options: TranscriptionOptions | None = None,
        profile: str = "high_accuracy",
        priority: int = 0,
        max_attempts: int = 3,
        job_id: str | None = None,
        available_at: datetime | str | None = None,
    ) -> Job:
        owner_key = _validate_owner_key(owner_key)
        profile = _validate_code(profile, "profile") or "high_accuracy"
        if profile not in _PROFILES:
            raise ValueError("profile must be one of: balanced, high_accuracy, fast")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("priority must be an integer")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        job_id = _validate_identifier(job_id or uuid.uuid4().hex, "job_id")
        options = options or TranscriptionOptions()
        if not isinstance(options, TranscriptionOptions):
            raise TypeError("options must be TranscriptionOptions")
        now_dt = _now()
        now = _timestamp(now_dt)
        available = _timestamp(available_at or now_dt)
        with self._connect() as connection, self._transaction(connection, immediate=True):
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, owner_key, status, stage, stage_status, profile,
                        priority, attempt, max_attempts, available_at,
                        options_json, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        owner_key,
                        JobStatus.CREATED.value,
                        PipelineStage.INGEST.value,
                        StageStatus.PENDING.value,
                        profile,
                        priority,
                        max_attempts,
                        available,
                        options.to_json(),
                        None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyConflictError("job identifier already exists") from exc
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.JOB_CREATED,
                stage=PipelineStage.INGEST,
                job_status=JobStatus.CREATED,
                stage_status=StageStatus.PENDING,
                created_at=now,
            )
            created_row = self._require_job(connection, job_id)
            self._coordinate_batch_retention_locked(connection, created_row)
            return self._job_from_row(self._require_job(connection, job_id))

    def get_job(self, job_id: str) -> Job:
        """Internal lookup. Owner-facing callers should use get_owned_job."""

        job_id = _validate_identifier(job_id, "job_id")
        with self._connect() as connection:
            return self._job_from_row(self._require_job(connection, job_id))

    def get_owned_job(self, job_id: str, owner_key: str) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        with self._connect() as connection:
            row = self._require_job(connection, job_id)
            self._require_owner(row, owner_key)
            return self._job_from_row(row)

    def reconcile_job_retention(self, job_id: str, owner_key: str) -> Job:
        """Persist the effective owner-scoped batch deadline and return the job."""

        job_id = _validate_identifier(job_id, "job_id")
        owner_key = _validate_owner_key(owner_key)
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_owner(row, owner_key)
            self._coordinate_batch_retention_locked(connection, row)
            return self._job_from_row(self._require_job(connection, job_id))

    def batch_state_snapshot(self, job_id: str, owner_key: str) -> str:
        """Return an opaque revision snapshot for one complete owner batch."""

        job_id = _validate_identifier(job_id, "job_id")
        owner_key = _validate_owner_key(owner_key)
        with self._connect() as connection, self._transaction(connection):
            row = self._require_job(connection, job_id)
            self._require_owner(row, owner_key)
            members = self._batch_rows_locked(connection, row)
            return self._batch_state_snapshot_locked(connection, members)

    def delete_batch_if_unchanged(
        self,
        job_id: str,
        owner_key: str,
        expected_snapshot: str,
        *,
        delete_files: Callable[[str], Any] | None = None,
    ) -> bool:
        """Purge a terminal batch only if no retry/edit occurred after delivery."""

        job_id = _validate_identifier(job_id, "job_id")
        owner_key = _validate_owner_key(owner_key)
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected_snapshot)):
            raise ValueError("expected_snapshot must be a SHA-256 identifier")
        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            self._require_owner(row, owner_key)
            members = self._batch_rows_locked(connection, row)
            if any(
                JobStatus(member["status"]) not in TERMINAL_JOB_STATUSES
                for member in members
            ):
                return False
            current_snapshot = self._batch_state_snapshot_locked(connection, members)
            if not secrets.compare_digest(current_snapshot, expected_snapshot):
                return False
            identifiers = self._mark_deletion_group_locked(
                connection,
                members,
                reason_code="owner_deleted",
                now=now,
            )
        deleted = self._delete_reserved_group(
            identifiers,
            owner_key,
            reason_code="owner_deleted",
            delete_files=delete_files,
        )
        return job_id in deleted

    def list_jobs(
        self,
        owner_key: str,
        *,
        statuses: Sequence[JobStatus] | None = None,
        limit: int = 100,
    ) -> list[Job]:
        owner_key = _validate_owner_key(owner_key)
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        parameters: list[Any] = [owner_key]
        where = "owner_key = ?"
        if statuses:
            normalized = [JobStatus(item).value for item in statuses]
            where += f" AND status IN ({','.join('?' for _ in normalized)})"
            parameters.extend(normalized)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE {where} ORDER BY created_at DESC, id LIMIT ?",
                parameters,
            ).fetchall()
            return [self._job_from_row(row) for row in rows]

    def retained_usage(self, owner_key: str | None = None) -> tuple[int, int]:
        """Return retained job count and registered source bytes for admission."""

        parameters: tuple[Any, ...] = ()
        where = ""
        if owner_key is not None:
            where = "WHERE jobs.owner_key = ?"
            parameters = (_validate_owner_key(owner_key),)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(DISTINCT jobs.id) AS job_count,
                       COALESCE(SUM(job_files.size_bytes), 0) AS byte_count
                FROM jobs
                LEFT JOIN job_files ON job_files.job_id = jobs.id
                {where}
                """,
                parameters,
            ).fetchone()
        return int(row["job_count"]), int(row["byte_count"])

    def register_file(
        self,
        job_id: str,
        *,
        owner_key: str,
        original_name: str,
        safe_name: str,
        relative_path: str,
        size_bytes: int,
        sha256: str,
        media_type: str | None = None,
        file_id: str | None = None,
    ) -> JobFile:
        job_id = _validate_identifier(job_id, "job_id")
        owner_key = _validate_owner_key(owner_key)
        file_id = _validate_identifier(file_id or uuid.uuid4().hex, "file_id")
        relative_path = _validate_relative_path(relative_path)
        if not isinstance(safe_name, str) or not safe_name or any(
            character in safe_name for character in ("/", "\\", "\x00")
        ):
            raise ValueError("safe_name must be a single safe path component")
        if not isinstance(original_name, str) or "\x00" in original_name:
            raise ValueError("original_name must be a string without NUL bytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        sha256 = str(sha256).lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if media_type is not None and (not isinstance(media_type, str) or len(media_type) > 255):
            raise ValueError("media_type must be a string up to 255 characters")
        now = _timestamp()

        with self._connect() as connection, self._transaction(connection, immediate=True):
            job_row = self._require_job(connection, job_id)
            self._require_owner(job_row, owner_key)
            if JobStatus(job_row["status"]) is not JobStatus.CREATED:
                raise InvalidTransitionError("files can only be registered while a job is created")
            try:
                connection.execute(
                    """
                    INSERT INTO job_files (
                        id, job_id, original_name, safe_name, relative_path,
                        media_type, size_bytes, sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        job_id,
                        original_name,
                        safe_name,
                        relative_path,
                        media_type,
                        size_bytes,
                        sha256,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyConflictError("file identifier or storage path already exists") from exc
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.FILE_REGISTERED,
                stage=PipelineStage.INGEST,
                job_status=JobStatus.CREATED,
                stage_status=StageStatus.PENDING,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM job_files WHERE id = ?", (file_id,)
            ).fetchone()
            return self._file_from_row(row)

    def list_files(self, job_id: str, owner_key: str) -> list[JobFile]:
        job_id = _validate_identifier(job_id, "job_id")
        with self._connect() as connection:
            job_row = self._require_job(connection, job_id)
            self._require_owner(job_row, owner_key)
            rows = connection.execute(
                "SELECT * FROM job_files WHERE job_id = ? ORDER BY created_at, id",
                (job_id,),
            ).fetchall()
            return [self._file_from_row(row) for row in rows]

    def find_files_by_sha256(self, sha256: str, owner_key: str) -> list[JobFile]:
        sha256 = str(sha256).lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        owner_key = _validate_owner_key(owner_key)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT f.* FROM job_files AS f
                JOIN jobs AS j ON j.id = f.job_id
                WHERE f.sha256 = ? AND j.owner_key = ?
                ORDER BY f.created_at, f.id
                """,
                (sha256, owner_key),
            ).fetchall()
            return [self._file_from_row(row) for row in rows]

    def enqueue_job(self, job_id: str, owner_key: str) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_owner(row, owner_key)
            status = JobStatus(row["status"])
            if status is JobStatus.QUEUED:
                return self._job_from_row(row)
            if status is not JobStatus.CREATED:
                raise InvalidTransitionError("only a created job can be initially queued")
            file_count = int(
                connection.execute(
                    "SELECT count(*) FROM job_files WHERE job_id = ?", (job_id,)
                ).fetchone()[0]
            )
            if file_count == 0:
                raise InvalidTransitionError("a job needs at least one registered file")
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage = ?, stage_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    PipelineStage.PROBE.value,
                    StageStatus.PENDING.value,
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.JOB_QUEUED,
                stage=PipelineStage.PROBE,
                job_status=JobStatus.QUEUED,
                stage_status=StageStatus.PENDING,
                created_at=now,
            )
            self._coordinate_batch_retention_locked(
                connection, self._require_job(connection, job_id)
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime | str | None = None,
    ) -> Job | None:
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        now_value = _timestamp(now)
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                  AND available_at <= ?
                  AND (expires_at IS NULL OR expires_at > ?)
                  AND attempt < max_attempts
                ORDER BY priority DESC, available_at, created_at, id
                LIMIT 1
                """,
                (JobStatus.QUEUED.value, now_value, now_value),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage_status = ?, attempt = attempt + 1,
                    worker_id = ?, claimed_at = ?, heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?), finished_at = NULL,
                    cancel_requested_at = NULL, error_code = NULL, updated_at = ?
                WHERE id = ? AND status = ? AND attempt = ?
                """,
                (
                    JobStatus.RUNNING.value,
                    StageStatus.RUNNING.value,
                    worker_id,
                    now_value,
                    now_value,
                    now_value,
                    now_value,
                    row["id"],
                    JobStatus.QUEUED.value,
                    row["attempt"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflictError("queue claim lost to another worker")
            claimed = self._require_job(connection, row["id"])
            self._insert_event(
                connection,
                job_id=row["id"],
                event_type=EventType.JOB_CLAIMED,
                stage=PipelineStage(claimed["stage"]),
                job_status=JobStatus.RUNNING,
                stage_status=StageStatus.RUNNING,
                worker_id=worker_id,
                created_at=now_value,
            )
            self._coordinate_batch_retention_locked(connection, claimed)
            return self._job_from_row(self._require_job(connection, row["id"]))

    def heartbeat(self, job_id: str, worker_id: str) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_worker(row, worker_id)
            if JobStatus(row["status"]) not in {
                JobStatus.RUNNING,
                JobStatus.CANCEL_REQUESTED,
            }:
                raise InvalidTransitionError("only an active job can be heartbeated")
            connection.execute(
                "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE id = ?",
                (now, now, job_id),
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def set_stage(
        self,
        job_id: str,
        worker_id: str,
        stage: PipelineStage,
        stage_status: StageStatus,
        *,
        reason_code: str | None = None,
    ) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        stage = PipelineStage(stage)
        stage_status = StageStatus(stage_status)
        if stage_status in {StageStatus.FAILED, StageStatus.CANCELED}:
            raise ValueError("use fail_job or cancellation APIs for terminal stage states")
        reason_code = _validate_code(reason_code, "reason_code")
        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_worker(row, worker_id)
            if JobStatus(row["status"]) is not JobStatus.RUNNING:
                raise InvalidTransitionError("stage changes require a running job")
            current_stage = PipelineStage(row["stage"])
            if _STAGE_ORDER[stage] < _STAGE_ORDER[current_stage]:
                raise InvalidTransitionError("a running attempt cannot move to an earlier stage")
            connection.execute(
                """
                UPDATE jobs
                SET stage = ?, stage_status = ?, heartbeat_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (stage.value, stage_status.value, now, now, job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.STAGE_CHANGED,
                stage=stage,
                job_status=JobStatus.RUNNING,
                stage_status=stage_status,
                worker_id=worker_id,
                reason_code=reason_code,
                created_at=now,
            )
            self._coordinate_batch_retention_locked(
                connection, self._require_job(connection, job_id)
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def request_cancel(
        self,
        job_id: str,
        owner_key: str,
        *,
        reason_code: str = "owner_requested",
    ) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        reason_code = _validate_code(reason_code, "reason_code") or "owner_requested"
        now = _timestamp()
        short_expiry = _expiry_from(now, self.failed_retention_hours)
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_owner(row, owner_key)
            status = JobStatus(row["status"])
            if status in {JobStatus.CANCELED, JobStatus.SUCCEEDED, JobStatus.FAILED}:
                self._coordinate_batch_retention_locked(connection, row)
                return self._job_from_row(self._require_job(connection, job_id))
            if status is JobStatus.CANCEL_REQUESTED:
                self._coordinate_batch_retention_locked(connection, row)
                return self._job_from_row(self._require_job(connection, job_id))
            if status in {JobStatus.CREATED, JobStatus.QUEUED}:
                new_status = JobStatus.CANCELED
                new_stage_status = StageStatus.CANCELED
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, stage_status = ?, cancel_requested_at = ?,
                        finished_at = ?, expires_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_status.value,
                        new_stage_status.value,
                        now,
                        now,
                        short_expiry,
                        now,
                        job_id,
                    ),
                )
                event_type = EventType.JOB_CANCELED
            elif status is JobStatus.RUNNING:
                new_status = JobStatus.CANCEL_REQUESTED
                new_stage_status = StageStatus(row["stage_status"])
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, cancel_requested_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status.value, now, now, job_id),
                )
                event_type = EventType.CANCEL_REQUESTED
            else:  # pragma: no cover - enum/schema make this defensive only
                raise InvalidTransitionError("job cannot be canceled from its current state")
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=event_type,
                stage=PipelineStage(row["stage"]),
                job_status=new_status,
                stage_status=new_stage_status,
                worker_id=row["worker_id"],
                reason_code=reason_code,
                created_at=now,
            )
            self._coordinate_batch_retention_locked(
                connection, self._require_job(connection, job_id)
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def acknowledge_cancel(self, job_id: str, worker_id: str) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        now = _timestamp()
        expiry = _expiry_from(now, self.failed_retention_hours)
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_worker(row, worker_id)
            if JobStatus(row["status"]) is not JobStatus.CANCEL_REQUESTED:
                raise InvalidTransitionError("job has no cancellation request to acknowledge")
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage_status = ?, worker_id = NULL,
                    heartbeat_at = NULL, finished_at = ?, expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.CANCELED.value,
                    StageStatus.CANCELED.value,
                    now,
                    expiry,
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.JOB_CANCELED,
                stage=PipelineStage(row["stage"]),
                job_status=JobStatus.CANCELED,
                stage_status=StageStatus.CANCELED,
                worker_id=worker_id,
                reason_code="worker_acknowledged",
                created_at=now,
            )
            self._coordinate_batch_retention_locked(
                connection, self._require_job(connection, job_id)
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def complete_job(self, job_id: str, worker_id: str) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_worker(row, worker_id)
            if JobStatus(row["status"]) is not JobStatus.RUNNING:
                raise InvalidTransitionError("only a running job can succeed")
            if PipelineStage(row["stage"]) is not PipelineStage.EXPORT or StageStatus(
                row["stage_status"]
            ) not in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}:
                raise InvalidTransitionError("export must finish before a job can succeed")
            options = TranscriptionOptions.from_json(row["options_json"])
            expiry = _expiry_from(now, options.retention_hours)
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, worker_id = NULL, heartbeat_at = NULL,
                    finished_at = ?, expires_at = ?, error_code = NULL, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.SUCCEEDED.value, now, expiry, now, job_id),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.JOB_SUCCEEDED,
                stage=PipelineStage.EXPORT,
                job_status=JobStatus.SUCCEEDED,
                stage_status=StageStatus.SUCCEEDED,
                worker_id=worker_id,
                created_at=now,
            )
            self._coordinate_batch_retention_locked(
                connection, self._require_job(connection, job_id)
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        *,
        retryable: bool = False,
        retry_delay_seconds: int = 0,
    ) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        error_code = _validate_code(error_code, "error_code") or "unknown_error"
        if (
            not isinstance(retry_delay_seconds, int)
            or isinstance(retry_delay_seconds, bool)
            or retry_delay_seconds < 0
        ):
            raise ValueError("retry_delay_seconds must be a non-negative integer")
        now_dt = _now()
        now = _timestamp(now_dt)
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_worker(row, worker_id)
            status = JobStatus(row["status"])
            if status not in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}:
                raise InvalidTransitionError("only an active job can fail")

            if status is JobStatus.CANCEL_REQUESTED:
                new_status = JobStatus.CANCELED
                new_stage_status = StageStatus.CANCELED
                event_type = EventType.JOB_CANCELED
                available_at = row["available_at"]
                finished_at = now
                expiry = _expiry_from(now, self.failed_retention_hours)
                reason_code = "cancel_won_race"
            elif retryable and int(row["attempt"]) < int(row["max_attempts"]):
                new_status = JobStatus.QUEUED
                new_stage_status = StageStatus.PENDING
                event_type = EventType.RETRY_QUEUED
                available_at = _timestamp(now_dt + timedelta(seconds=retry_delay_seconds))
                finished_at = None
                expiry = None
                reason_code = error_code
                self._insert_event(
                    connection,
                    job_id=job_id,
                    event_type=EventType.ATTEMPT_FAILED,
                    stage=PipelineStage(row["stage"]),
                    job_status=JobStatus.RUNNING,
                    stage_status=StageStatus.FAILED,
                    worker_id=worker_id,
                    reason_code=error_code,
                    created_at=now,
                )
            else:
                new_status = JobStatus.FAILED
                new_stage_status = StageStatus.FAILED
                event_type = EventType.JOB_FAILED
                available_at = row["available_at"]
                finished_at = now
                expiry = _expiry_from(now, self.failed_retention_hours)
                reason_code = error_code

            connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage_status = ?, available_at = ?,
                    worker_id = NULL, heartbeat_at = NULL, finished_at = ?,
                    error_code = ?, expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_status.value,
                    new_stage_status.value,
                    available_at,
                    finished_at,
                    error_code,
                    expiry,
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=event_type,
                stage=PipelineStage(row["stage"]),
                job_status=new_status,
                stage_status=new_stage_status,
                worker_id=worker_id,
                reason_code=reason_code,
                created_at=now,
            )
            self._coordinate_batch_retention_locked(
                connection, self._require_job(connection, job_id)
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def retry_job(
        self,
        job_id: str,
        owner_key: str,
        *,
        from_stage: PipelineStage = PipelineStage.PROBE,
        delay_seconds: int = 0,
    ) -> Job:
        job_id = _validate_identifier(job_id, "job_id")
        from_stage = PipelineStage(from_stage)
        if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool) or delay_seconds < 0:
            raise ValueError("delay_seconds must be a non-negative integer")
        now_dt = _now()
        now = _timestamp(now_dt)
        available = _timestamp(now_dt + timedelta(seconds=delay_seconds))
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_owner(row, owner_key)
            if JobStatus(row["status"]) not in {JobStatus.FAILED, JobStatus.CANCELED}:
                raise InvalidTransitionError("only a failed or canceled job can be retried")
            if row["error_code"] in _DELETION_REASON_CODES:
                raise InvalidTransitionError("a job reserved for deletion cannot be retried")
            if int(row["attempt"]) >= int(row["max_attempts"]):
                raise RetryLimitError("job has no attempts remaining")
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage = ?, stage_status = ?, available_at = ?,
                    worker_id = NULL, claimed_at = NULL, heartbeat_at = NULL,
                    cancel_requested_at = NULL, finished_at = NULL,
                    error_code = NULL, expires_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.QUEUED.value,
                    from_stage.value,
                    StageStatus.PENDING.value,
                    available,
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.RETRY_QUEUED,
                stage=from_stage,
                job_status=JobStatus.QUEUED,
                stage_status=StageStatus.PENDING,
                reason_code="owner_retry",
                created_at=now,
            )
            self._coordinate_batch_retention_locked(
                connection, self._require_job(connection, job_id)
            )
            return self._job_from_row(self._require_job(connection, job_id))

    def recover_running_jobs(
        self,
        *,
        stale_before: datetime | str | None = None,
        reason_code: str = "worker_restart",
    ) -> RecoverySummary:
        cutoff = _timestamp(stale_before)
        reason_code = _validate_code(reason_code, "reason_code") or "worker_restart"
        now = _timestamp()
        counts = {"requeued": 0, "canceled": 0, "failed": 0}
        with self._connect() as connection, self._transaction(connection, immediate=True):
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN (?, ?)
                  AND COALESCE(heartbeat_at, claimed_at, updated_at) <= ?
                ORDER BY created_at, id
                """,
                (
                    JobStatus.RUNNING.value,
                    JobStatus.CANCEL_REQUESTED.value,
                    cutoff,
                ),
            ).fetchall()
            for row in rows:
                status = JobStatus(row["status"])
                if status is JobStatus.CANCEL_REQUESTED:
                    new_status = JobStatus.CANCELED
                    stage_status = StageStatus.CANCELED
                    event_type = EventType.JOB_CANCELED
                    expiry = _expiry_from(now, self.failed_retention_hours)
                    finished_at = now
                    counts["canceled"] += 1
                elif int(row["attempt"]) < int(row["max_attempts"]):
                    new_status = JobStatus.QUEUED
                    stage_status = StageStatus.PENDING
                    event_type = EventType.RECOVERED_REQUEUED
                    expiry = None
                    finished_at = None
                    counts["requeued"] += 1
                else:
                    new_status = JobStatus.FAILED
                    stage_status = StageStatus.FAILED
                    event_type = EventType.RECOVERED_FAILED
                    expiry = _expiry_from(now, self.failed_retention_hours)
                    finished_at = now
                    counts["failed"] += 1
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, stage_status = ?, worker_id = NULL,
                        claimed_at = NULL, heartbeat_at = NULL,
                        finished_at = ?, expires_at = ?, error_code = ?,
                        available_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_status.value,
                        stage_status.value,
                        finished_at,
                        expiry,
                        reason_code if new_status is JobStatus.FAILED else None,
                        now,
                        now,
                        row["id"],
                    ),
                )
                self._insert_event(
                    connection,
                    job_id=row["id"],
                    event_type=event_type,
                    stage=PipelineStage(row["stage"]),
                    job_status=new_status,
                    stage_status=stage_status,
                    worker_id=row["worker_id"],
                    reason_code=reason_code,
                    created_at=now,
                )
            coordinated: set[tuple[str, str]] = set()
            for affected in rows:
                current = self._require_job(connection, affected["id"])
                options = self._row_options(current)
                if options.batch_id is None:
                    continue
                key = (current["owner_key"], options.batch_id)
                if key in coordinated:
                    continue
                self._coordinate_batch_retention_locked(connection, current)
                coordinated.add(key)
        return RecoverySummary(**counts)

    def list_events(self, job_id: str, owner_key: str) -> list[JobEvent]:
        job_id = _validate_identifier(job_id, "job_id")
        with self._connect() as connection:
            job_row = self._require_job(connection, job_id)
            self._require_owner(job_row, owner_key)
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def list_expired_job_ids(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> list[str]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        self._reconcile_all_batch_retention()
        now_value = _timestamp(now)
        purgeable = (
            JobStatus.CREATED.value,
            JobStatus.QUEUED.value,
            JobStatus.CANCELED.value,
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM jobs
                WHERE expires_at IS NOT NULL AND expires_at <= ?
                  AND status IN ({','.join('?' for _ in purgeable)})
                ORDER BY expires_at, id LIMIT ?
                """,
                (now_value, *purgeable, limit),
            ).fetchall()
            return [row["id"] for row in rows]

    def delete_job(
        self,
        job_id: str,
        owner_key: str,
        *,
        delete_files: Callable[[str], Any] | None = None,
        include_batch: bool = True,
    ) -> bool:
        """Hard-delete an owner job, atomically including its persisted batch.

        File deletion happens before the database row is removed.  If it
        raises, every reserved database row remains so cleanup can be retried
        without silently shrinking a batch. ``include_batch=False`` is only
        for a newly-created child whose upload was never accepted.
        """

        job_id = _validate_identifier(job_id, "job_id")
        owner_key = _validate_owner_key(owner_key)
        if not isinstance(include_batch, bool):
            raise ValueError("include_batch must be a boolean")
        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = self._require_job(connection, job_id)
            self._require_owner(row, owner_key)
            row_options = self._row_options(row)
            if not include_batch and row_options.batch_id is not None:
                if JobStatus(row["status"]) is not JobStatus.CREATED:
                    raise InvalidTransitionError(
                        "only an unaccepted created batch child can be deleted alone"
                    )
                # Detach a child that never completed ingestion so a rare file
                # cleanup retry cannot later shorten or delete the accepted
                # siblings' coordinated delivery window.
                connection.execute(
                    "UPDATE jobs SET options_json = ? WHERE id = ?",
                    (replace(row_options, batch_id=None).to_json(), job_id),
                )
                row = self._require_job(connection, job_id)
            members = self._batch_rows_locked(
                connection, row, include_batch=include_batch
            )
            if any(
                JobStatus(member["status"])
                in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
                for member in members
            ):
                raise InvalidTransitionError(
                    "cancel and drain every active batch job before deleting it"
                )
            identifiers = self._mark_deletion_group_locked(
                connection,
                members,
                reason_code="owner_deleted",
                now=now,
            )
        try:
            deleted = self._delete_reserved_group(
                identifiers,
                owner_key,
                reason_code="owner_deleted",
                delete_files=delete_files,
            )
        finally:
            if not include_batch:
                self._reconcile_all_batch_retention()
        return job_id in deleted

    def checkpoint_wal(self) -> bool:
        """Best-effort WAL truncation after confidential rows are hard-deleted."""

        try:
            with self._connect() as connection:
                row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return row is not None and int(row[0]) == 0
        except sqlite3.OperationalError:
            # Another short-lived connection may still hold a read lock. The
            # independent cleanup timer retries this on its next pass.
            return False

    def purge_expired(
        self,
        *,
        delete_files: Callable[[str], Any] | None = None,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> list[str]:
        """Delete expired job rows/content, optionally deleting file trees too."""

        now_value = _timestamp(now)
        candidates = self.list_expired_job_ids(now=now_value, limit=limit)
        deleted_ids: list[str] = []
        processed: set[str] = set()
        for job_id in candidates:
            if job_id in processed:
                continue
            with self._connect() as connection, self._transaction(connection, immediate=True):
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    continue
                self._coordinate_batch_retention_locked(connection, row)
                row = self._require_job(connection, job_id)
                members = self._batch_rows_locked(connection, row)
                if any(
                    JobStatus(member["status"]) not in TERMINAL_JOB_STATUSES
                    or member["expires_at"] is None
                    or member["expires_at"] > now_value
                    for member in members
                ):
                    continue
                identifiers = self._mark_deletion_group_locked(
                    connection,
                    members,
                    reason_code="retention_expired",
                    now=now_value,
                )
                owner = row["owner_key"]
            processed.update(identifiers)
            deleted_ids.extend(
                self._delete_reserved_group(
                    identifiers,
                    owner,
                    reason_code="retention_expired",
                    delete_files=delete_files,
                )
            )
        return deleted_ids

    def purge_stale_unfinished(
        self,
        *,
        stale_before: datetime | str,
        delete_files: Callable[[str], Any] | None = None,
        limit: int = 100,
    ) -> list[str]:
        """Hard-delete abandoned CREATED/QUEUED uploads after an active-job cap.

        Running work is never selected. Each queued candidate is atomically
        changed to CANCELED before its file tree is removed, so it cannot race a
        worker claim. This cap prevents an offline worker from turning the
        ephemeral delivery service into indefinite upload storage.
        """

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        cutoff = _timestamp(stale_before)
        candidates_status = (JobStatus.CREATED.value, JobStatus.QUEUED.value)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE created_at <= ? AND status IN (?, ?)
                ORDER BY created_at, id LIMIT ?
                """,
                (cutoff, *candidates_status, limit),
            ).fetchall()
        deleted_ids: list[str] = []
        processed: set[str] = set()
        now = _timestamp()
        for row in rows:
            job_id = row["id"]
            if job_id in processed:
                continue
            with self._connect() as connection, self._transaction(connection, immediate=True):
                current = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if current is None or (
                    current["created_at"] > cutoff
                    or JobStatus(current["status"])
                    not in {JobStatus.CREATED, JobStatus.QUEUED}
                ):
                    continue
                members = self._batch_rows_locked(connection, current)
                if any(
                    JobStatus(member["status"])
                    in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
                    or (
                        JobStatus(member["status"])
                        in {JobStatus.CREATED, JobStatus.QUEUED}
                        and member["created_at"] > cutoff
                    )
                    for member in members
                ):
                    continue
                identifiers = self._mark_deletion_group_locked(
                    connection,
                    members,
                    reason_code="active_lifetime_expired",
                    now=now,
                )
                owner = current["owner_key"]
            processed.update(identifiers)
            deleted_ids.extend(
                self._delete_reserved_group(
                    identifiers,
                    owner,
                    reason_code="active_lifetime_expired",
                    delete_files=delete_files,
                )
            )
        return deleted_ids

    @staticmethod
    def _validate_segment_draft(segment: SegmentDraft) -> SegmentDraft:
        if not isinstance(segment, SegmentDraft):
            raise TypeError("segments must contain SegmentDraft values")
        if (
            not isinstance(segment.ordinal, int)
            or isinstance(segment.ordinal, bool)
            or segment.ordinal < 0
        ):
            raise ValueError("segment ordinal must be a non-negative integer")
        if (
            not isinstance(segment.start_ms, int)
            or isinstance(segment.start_ms, bool)
            or segment.start_ms < 0
        ):
            raise ValueError("segment start_ms must be a non-negative integer")
        if (
            not isinstance(segment.end_ms, int)
            or isinstance(segment.end_ms, bool)
            or segment.end_ms < segment.start_ms
        ):
            raise ValueError("segment end_ms must be at least start_ms")
        if not isinstance(segment.model_text, str):
            raise ValueError("segment model_text must be a string")
        if segment.translated_text is not None and not isinstance(segment.translated_text, str):
            raise ValueError("segment translated_text must be a string or None")
        if segment.speaker_key is not None:
            _validate_code(segment.speaker_key, "speaker_key")
        _validate_confidence(segment.confidence)
        if not isinstance(segment.overlap, bool):
            raise ValueError("segment overlap must be a boolean")
        return segment

    def add_segments(
        self,
        job_id: str,
        file_id: str,
        worker_id: str,
        segments: Sequence[SegmentDraft],
    ) -> list[TranscriptSegment]:
        """Insert one file's immutable model output in a single transaction."""

        job_id = _validate_identifier(job_id, "job_id")
        file_id = _validate_identifier(file_id, "file_id")
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        if len(segments) > 1_000_000:
            raise ValueError("segment batch is unreasonably large")
        validated = [self._validate_segment_draft(segment) for segment in segments]
        ordinals = [segment.ordinal for segment in validated]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("segment ordinals must be unique within a file")
        if not validated:
            return []
        now = _timestamp()
        identifiers = [uuid.uuid4().hex for _ in validated]
        with self._connect() as connection, self._transaction(connection, immediate=True):
            job_row = self._require_job(connection, job_id)
            self._require_worker(job_row, worker_id)
            if JobStatus(job_row["status"]) is not JobStatus.RUNNING:
                raise InvalidTransitionError("segments can only be added by an active worker")
            file_row = connection.execute(
                "SELECT id FROM job_files WHERE id = ? AND job_id = ?",
                (file_id, job_id),
            ).fetchone()
            if file_row is None:
                raise NotFoundError("job file not found")
            try:
                connection.executemany(
                    """
                    INSERT INTO segments (
                        id, job_id, file_id, ordinal, start_ms, end_ms,
                        speaker_key, model_text, translated_text, confidence,
                        overlap, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    [
                        (
                            segment_id,
                            job_id,
                            file_id,
                            segment.ordinal,
                            segment.start_ms,
                            segment.end_ms,
                            segment.speaker_key,
                            segment.model_text,
                            segment.translated_text,
                            _validate_confidence(segment.confidence),
                            int(segment.overlap),
                            now,
                            now,
                        )
                        for segment_id, segment in zip(identifiers, validated)
                    ],
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrencyConflictError(
                    "one or more segment identifiers or ordinals already exist"
                ) from exc
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.SEGMENT_CREATED,
                stage=PipelineStage(job_row["stage"]),
                job_status=JobStatus.RUNNING,
                stage_status=StageStatus(job_row["stage_status"]),
                worker_id=worker_id,
                created_at=now,
            )
            placeholders = ",".join("?" for _ in identifiers)
            rows = connection.execute(
                f"SELECT * FROM segments WHERE id IN ({placeholders}) ORDER BY ordinal",
                identifiers,
            ).fetchall()
            return [self._segment_from_row(row) for row in rows]

    def add_segment(
        self,
        job_id: str,
        file_id: str,
        worker_id: str,
        segment: SegmentDraft,
    ) -> TranscriptSegment:
        return self.add_segments(job_id, file_id, worker_id, [segment])[0]

    def replace_segments(
        self,
        job_id: str,
        file_id: str,
        worker_id: str,
        segments: Sequence[SegmentDraft],
    ) -> list[TranscriptSegment]:
        """Atomically replace one file's complete worker-generated draft.

        This is the retry/rerun operation: all prior segments for the file are
        deleted before the new batch is inserted in the same transaction.
        Consequently, prior ``edited_text`` and its revision are intentionally
        discarded rather than being applied to different model output.
        """

        job_id = _validate_identifier(job_id, "job_id")
        file_id = _validate_identifier(file_id, "file_id")
        worker_id = _validate_code(worker_id, "worker_id") or "worker"
        if len(segments) > 1_000_000:
            raise ValueError("segment batch is unreasonably large")
        validated = [self._validate_segment_draft(segment) for segment in segments]
        ordinals = [segment.ordinal for segment in validated]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("segment ordinals must be unique within a file")
        now = _timestamp()
        identifiers = [uuid.uuid4().hex for _ in validated]

        with self._connect() as connection, self._transaction(connection, immediate=True):
            job_row = self._require_job(connection, job_id)
            self._require_worker(job_row, worker_id)
            if JobStatus(job_row["status"]) is not JobStatus.RUNNING:
                raise InvalidTransitionError(
                    "segments can only be replaced by an active worker"
                )
            file_row = connection.execute(
                "SELECT id FROM job_files WHERE id = ? AND job_id = ?",
                (file_id, job_id),
            ).fetchone()
            if file_row is None:
                raise NotFoundError("job file not found")

            connection.execute(
                "DELETE FROM segments WHERE job_id = ? AND file_id = ?",
                (job_id, file_id),
            )
            if validated:
                try:
                    connection.executemany(
                        """
                        INSERT INTO segments (
                            id, job_id, file_id, ordinal, start_ms, end_ms,
                            speaker_key, model_text, translated_text, confidence,
                            overlap, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        [
                            (
                                segment_id,
                                job_id,
                                file_id,
                                segment.ordinal,
                                segment.start_ms,
                                segment.end_ms,
                                segment.speaker_key,
                                segment.model_text,
                                segment.translated_text,
                                _validate_confidence(segment.confidence),
                                int(segment.overlap),
                                now,
                                now,
                            )
                            for segment_id, segment in zip(identifiers, validated)
                        ],
                    )
                except sqlite3.IntegrityError as exc:
                    # The surrounding transaction restores the previous draft.
                    raise ConcurrencyConflictError(
                        "replacement segment batch violates durable constraints"
                    ) from exc
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.SEGMENTS_REPLACED,
                stage=PipelineStage(job_row["stage"]),
                job_status=JobStatus.RUNNING,
                stage_status=StageStatus(job_row["stage_status"]),
                worker_id=worker_id,
                reason_code="worker_rerun",
                created_at=now,
            )
            if not identifiers:
                return []
            placeholders = ",".join("?" for _ in identifiers)
            rows = connection.execute(
                f"SELECT * FROM segments WHERE id IN ({placeholders}) ORDER BY ordinal",
                identifiers,
            ).fetchall()
            return [self._segment_from_row(row) for row in rows]

    def list_segments(
        self,
        job_id: str,
        owner_key: str,
        *,
        file_id: str | None = None,
    ) -> list[TranscriptSegment]:
        job_id = _validate_identifier(job_id, "job_id")
        parameters: list[Any] = [job_id]
        where = "job_id = ?"
        if file_id is not None:
            file_id = _validate_identifier(file_id, "file_id")
            where += " AND file_id = ?"
            parameters.append(file_id)
        with self._connect() as connection:
            job_row = self._require_job(connection, job_id)
            self._require_owner(job_row, owner_key)
            rows = connection.execute(
                f"SELECT * FROM segments WHERE {where} ORDER BY file_id, ordinal",
                parameters,
            ).fetchall()
            return [self._segment_from_row(row) for row in rows]

    def update_segment(
        self,
        segment_id: str,
        owner_key: str,
        expected_revision: int,
        *,
        edited_text: str | None | object = _UNSET,
        translated_text: str | None | object = _UNSET,
        speaker_key: str | None | object = _UNSET,
        confidence: float | None | object = _UNSET,
        overlap: bool | object = _UNSET,
    ) -> TranscriptSegment:
        """Optimistically apply human-review fields; model_text is immutable."""

        segment_id = _validate_identifier(segment_id, "segment_id")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        updates: list[str] = []
        values: list[Any] = []
        if edited_text is not _UNSET:
            if edited_text is not None and not isinstance(edited_text, str):
                raise ValueError("edited_text must be a string or None")
            updates.append("edited_text = ?")
            values.append(edited_text)
        if translated_text is not _UNSET:
            if translated_text is not None and not isinstance(translated_text, str):
                raise ValueError("translated_text must be a string or None")
            updates.append("translated_text = ?")
            values.append(translated_text)
        if speaker_key is not _UNSET:
            if speaker_key is not None:
                _validate_code(speaker_key, "speaker_key")
            updates.append("speaker_key = ?")
            values.append(speaker_key)
        if confidence is not _UNSET:
            normalized_confidence = _validate_confidence(confidence)  # type: ignore[arg-type]
            updates.append("confidence = ?")
            values.append(normalized_confidence)
        if overlap is not _UNSET:
            if not isinstance(overlap, bool):
                raise ValueError("overlap must be a boolean")
            updates.append("overlap = ?")
            values.append(int(overlap))
        if not updates:
            raise ValueError("at least one editable segment field is required")

        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            row = connection.execute(
                """
                SELECT s.*, j.owner_key, j.status AS current_job_status,
                       j.stage AS current_stage, j.stage_status AS current_stage_status,
                       j.error_code AS current_error_code
                FROM segments AS s JOIN jobs AS j ON j.id = s.job_id
                WHERE s.id = ?
                """,
                (segment_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("segment not found")
            self._require_owner(row, owner_key)
            if row["current_error_code"] in _DELETION_REASON_CODES:
                raise InvalidTransitionError("a job reserved for deletion cannot be edited")
            values.extend((now, segment_id, expected_revision))
            updated = connection.execute(
                f"""
                UPDATE segments SET {', '.join(updates)},
                    revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                values,
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflictError("segment revision no longer matches")
            self._insert_event(
                connection,
                job_id=row["job_id"],
                event_type=EventType.SEGMENT_UPDATED,
                stage=PipelineStage(row["current_stage"]),
                job_status=JobStatus(row["current_job_status"]),
                stage_status=StageStatus(row["current_stage_status"]),
                reason_code="owner_edit",
                created_at=now,
            )
            result = connection.execute(
                "SELECT * FROM segments WHERE id = ?", (segment_id,)
            ).fetchone()
            return self._segment_from_row(result)

    def set_speaker_mapping(
        self,
        job_id: str,
        owner_key: str,
        speaker_key: str,
        display_name: str,
        *,
        identity_status: IdentityStatus = IdentityStatus.UNKNOWN,
        identity_method: IdentityMethod = IdentityMethod.NONE,
        confidence: float | None = None,
        expected_revision: int | None = None,
    ) -> SpeakerMapping:
        job_id = _validate_identifier(job_id, "job_id")
        speaker_key = _validate_code(speaker_key, "speaker_key") or "UNKNOWN"
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 500:
            raise ValueError("display_name must be a non-empty string up to 500 characters")
        display_name = display_name.strip()
        identity_status = IdentityStatus(identity_status)
        identity_method = IdentityMethod(identity_method)
        confidence = _validate_confidence(confidence)
        if identity_status is IdentityStatus.CONFIRMED and identity_method is IdentityMethod.NONE:
            raise ValueError("a confirmed identity requires an identity method")
        if expected_revision is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be zero or a positive integer")
        now = _timestamp()
        with self._connect() as connection, self._transaction(connection, immediate=True):
            job_row = self._require_job(connection, job_id)
            self._require_owner(job_row, owner_key)
            if job_row["error_code"] in _DELETION_REASON_CODES:
                raise InvalidTransitionError("a job reserved for deletion cannot be edited")
            existing = connection.execute(
                "SELECT * FROM speaker_mappings WHERE job_id = ? AND speaker_key = ?",
                (job_id, speaker_key),
            ).fetchone()
            if existing is None:
                if expected_revision not in {None, 0}:
                    raise ConcurrencyConflictError("speaker mapping does not yet exist")
                connection.execute(
                    """
                    INSERT INTO speaker_mappings (
                        job_id, speaker_key, display_name, identity_status,
                        identity_method, confidence, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        job_id,
                        speaker_key,
                        display_name,
                        identity_status.value,
                        identity_method.value,
                        confidence,
                        now,
                        now,
                    ),
                )
            else:
                if expected_revision is not None and int(existing["revision"]) != expected_revision:
                    raise ConcurrencyConflictError("speaker mapping revision no longer matches")
                connection.execute(
                    """
                    UPDATE speaker_mappings
                    SET display_name = ?, identity_status = ?, identity_method = ?,
                        confidence = ?, revision = revision + 1, updated_at = ?
                    WHERE job_id = ? AND speaker_key = ?
                    """,
                    (
                        display_name,
                        identity_status.value,
                        identity_method.value,
                        confidence,
                        now,
                        job_id,
                        speaker_key,
                    ),
                )
            self._insert_event(
                connection,
                job_id=job_id,
                event_type=EventType.SPEAKER_MAPPING_SET,
                stage=PipelineStage(job_row["stage"]),
                job_status=JobStatus(job_row["status"]),
                stage_status=StageStatus(job_row["stage_status"]),
                reason_code="owner_mapping",
                created_at=now,
            )
            result = connection.execute(
                "SELECT * FROM speaker_mappings WHERE job_id = ? AND speaker_key = ?",
                (job_id, speaker_key),
            ).fetchone()
            return self._speaker_from_row(result)

    def list_speaker_mappings(
        self, job_id: str, owner_key: str
    ) -> list[SpeakerMapping]:
        job_id = _validate_identifier(job_id, "job_id")
        with self._connect() as connection:
            job_row = self._require_job(connection, job_id)
            self._require_owner(job_row, owner_key)
            rows = connection.execute(
                """
                SELECT * FROM speaker_mappings
                WHERE job_id = ? ORDER BY speaker_key
                """,
                (job_id,),
            ).fetchall()
            return [self._speaker_from_row(row) for row in rows]
