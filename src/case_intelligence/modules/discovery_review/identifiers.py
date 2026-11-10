"""Store identifiers a case team assigns to items that arrived without one.

An assigned identifier is the team's own label. It is kept apart from produced
Bates numbers, never overwrites one, and is unique within a matter.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from case_intelligence.modules import ModuleDatabase

PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,19}")
MAX_DIGITS = 12
MAX_BATCH = 250_000


class IdentifierConflict(ValueError):
    """An identifier in the batch is already used in this matter."""


class IdentifierUnavailable(RuntimeError):
    """The matter no longer accepts changes."""


def sequence(prefix: str, start: int, digits: int, count: int) -> list[str]:
    """Return ``count`` zero-padded labels, or refuse a shape that cannot hold them."""
    if (PREFIX.fullmatch(prefix) is None or not 1 <= digits <= MAX_DIGITS or start < 0
            or not 0 < count <= MAX_BATCH or len(str(start + count - 1)) > digits):
        raise ValueError("identifier numbering is invalid")
    return [f"{prefix}{number:0{digits}d}" for number in range(start, start + count)]


class IdentifierRepository:
    def __init__(self, db: ModuleDatabase):
        self.db = db
        self.marks: dict[str, int] = {}

    def mark(self, matter_id: str) -> int:
        """Changes when this process writes identifiers, so derived lists rebuild."""
        return self.marks.get(matter_id, 0)

    def read(self, matter_id: str) -> dict[str, str]:
        with self.db.lock:
            return {row[0]: row[1] for row in self.db.connection.execute(
                "SELECT document_id,identifier FROM workbench_discovery_identifier WHERE matter_id=?",
                (matter_id,))}

    def assign(self, matter_id: str, actor_id: str, pairs) -> int:
        """Record every pair or none; an identifier already in use refuses the batch."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [(matter_id, document_id, identifier, actor_id, now) for document_id, identifier in pairs]
        try:
            with self.db.lock, self.db.connection:
                self.db.connection.executemany(
                    "INSERT INTO workbench_discovery_identifier VALUES (?,?,?,?,?)", rows)
        except sqlite3.IntegrityError as exc:
            raise IdentifierConflict("identifier already in use") from exc
        except sqlite3.DatabaseError as exc:
            raise IdentifierUnavailable("matter does not accept identifier changes") from exc
        self.marks[matter_id] = self.mark(matter_id) + 1
        return len(rows)

    def clear(self, matter_id: str, document_ids) -> int:
        try:
            with self.db.lock, self.db.connection:
                removed = sum(self.db.connection.execute(
                    "DELETE FROM workbench_discovery_identifier WHERE matter_id=? AND document_id=?",
                    (matter_id, document_id)).rowcount for document_id in document_ids)
        except sqlite3.DatabaseError as exc:
            raise IdentifierUnavailable("matter does not accept identifier changes") from exc
        self.marks[matter_id] = self.mark(matter_id) + 1
        return removed
