"""Store identifiers a case team assigns to items that arrived without one.

An assigned identifier is the team's own label. It is kept apart from produced
Bates numbers, never overwrites one, and is unique within a matter.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from case_intelligence.modules import ModuleDatabase

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,19}[0-9]{1,12}")
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
    def __init__(self, db: ModuleDatabase, authority):
        self.db, self.authority = db, authority
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
        if any(IDENTIFIER.fullmatch(row[2]) is None for row in rows):
            raise IdentifierConflict("unsupported Team ID")
        try:
            with self.db.authorized_write(self.authority, matter_id, actor_id):
                self.db.connection.executemany(
                    "INSERT INTO workbench_discovery_identifier VALUES (?,?,?,?,?)", rows)
        except sqlite3.IntegrityError as exc:
            raise IdentifierConflict("identifier already in use") from exc
        except sqlite3.DatabaseError as exc:
            raise IdentifierUnavailable("matter does not accept identifier changes") from exc
        self.marks[matter_id] = self.mark(matter_id) + 1
        return len(rows)

    def clear(self, matter_id: str, actor_id: str, document_ids) -> int:
        try:
            with self.db.authorized_write(self.authority, matter_id, actor_id):
                removed = sum(self.db.connection.execute(
                    "DELETE FROM workbench_discovery_identifier WHERE matter_id=? AND document_id=?",
                    (matter_id, document_id)).rowcount for document_id in document_ids)
        except sqlite3.DatabaseError as exc:
            raise IdentifierUnavailable("matter does not accept identifier changes") from exc
        self.marks[matter_id] = self.mark(matter_id) + 1
        return removed


class RangeUnavailable(ValueError):
    """Produced metadata does not establish a safe collision check."""


def produced_collisions(index, definition, labels) -> bool:
    """Compare complete endpoints and intervals without creating Bates values."""
    from case_intelligence.modules.discovery_review.fields import normalize_header
    pairs = [tuple(item.value(key) for key in definition.heading_range) for item in index.items]
    for key, table in index.reads.items():
        if key[0] != "table" or table is None:
            continue
        if table.problem:
            raise RangeUnavailable("load file was not completely read")
        headings = [{normalize_header(h) for h in definition.by_key[k].headers}
                    for k in definition.heading_range]
        pairs.extend(tuple(next((v for h, v in row.values.items() if normalize_header(h) in aliases and v.strip()), "")
                           for aliases in headings) for row in table.rows)
    taken = {item.value(definition.heading_fallback).casefold() for item in index.items}
    intervals = []
    for begin, end in pairs:
        begin, end = begin.strip().casefold(), end.strip().casefold()
        taken.update((begin, end))
        if not begin or not end or begin == end:
            continue
        bounds = [re.fullmatch(r"(.*?)([0-9]+)", value) for value in (begin, end)]
        if (not all(bounds) or bounds[0][1] != bounds[1][1]
                or len(bounds[0][2]) != len(bounds[1][2]) or bounds[0][2] > bounds[1][2]):
            raise RangeUnavailable("produced range cannot be compared safely")
        intervals.append((bounds[0][1], bounds[0][2], bounds[1][2]))
    def number(value):
        value = value.lstrip("0") or "0"
        return len(value), value

    for label in labels:
        value = label.casefold()
        if value in taken:
            return True
        candidate = re.fullmatch(r"(.*?)([0-9]+)", value)
        if candidate and any(candidate[1] == prefix and number(begin) <= number(candidate[2]) <= number(end)
                             for prefix, begin, end in intervals):
            return True
    return False
