from __future__ import annotations

import sqlite3
import threading
from importlib import resources
from pathlib import Path


class SQLiteStore:
    """Small explicit SQLite development adapter; one lock per connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=5
        )
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'"
        ).fetchone()
        if exists:
            versions = [row[0] for row in self.connection.execute(
                "SELECT version FROM schema_migration ORDER BY version"
            )]
            if versions != [1]:
                raise RuntimeError("unsupported SQLite migration state")
            return
        sql = (
            resources.files("case_intelligence")
            .joinpath("migrations/sqlite/0001_slice1a.sql")
            .read_text(encoding="utf-8")
        )
        with self._lock:
            self.connection.executescript(
                "BEGIN IMMEDIATE;\n" + sql +
                "\nINSERT INTO schema_migration(version,applied_at) VALUES(1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));\nCOMMIT;"
            )

    def register_matter(self, matter_id: str, display_name: str) -> None:
        if not matter_id.strip() or not display_name.strip():
            raise ValueError("matter identity is required")
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO matter_catalog(matter_id,display_name) VALUES(?,?) "
                "ON CONFLICT(matter_id) DO UPDATE SET display_name=excluded.display_name",
                (matter_id, display_name),
            )

    def transaction(self, *, immediate: bool = False):
        return _Transaction(self, immediate)

    def close(self) -> None:
        self.connection.close()


class _Transaction:
    def __init__(self, store: SQLiteStore, immediate: bool) -> None:
        self.store = store
        self.immediate = immediate

    def __enter__(self) -> sqlite3.Connection:
        self.store._lock.acquire()
        try:
            self.store.connection.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        except BaseException:
            self.store._lock.release()
            raise
        return self.store.connection

    def __exit__(self, typ, _value, _traceback) -> None:
        try:
            self.store.connection.execute("COMMIT" if typ is None else "ROLLBACK")
        finally:
            self.store._lock.release()
