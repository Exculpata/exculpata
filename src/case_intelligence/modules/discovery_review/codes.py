"""Matter code definitions and immutable item events, using the shared database."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

from case_intelligence.modules import ModuleDatabase

MAX_LABEL = 80


class CodeUnavailable(RuntimeError):
    """The matter does not accept code changes."""


@dataclass(frozen=True, slots=True)
class Code:
    code_id: str
    label: str
    position: int
    active: bool
    digit: int | None


class CodeRepository:
    def __init__(self, db: ModuleDatabase):
        self.db = db

    def list(self, matter_id: str, *, include_retired: bool = False) -> list[Code]:
        with self.db.lock:
            return [Code(*row) for row in self.db.connection.execute(
                "SELECT code_id,label,position,active,digit FROM workbench_discovery_code "
                "WHERE matter_id=? AND (active=1 OR ?) ORDER BY position,code_id",
                (matter_id, include_retired))]

    def define(self, matter_id: str, actor_id: str, label: str, *, digit: int | None = None) -> Code:
        label = label.strip()
        if not label or len(label) > MAX_LABEL:
            raise ValueError("Use a label of 1 to 80 characters.")
        if digit is not None and (type(digit) is not int or not 1 <= digit <= 9):
            raise ValueError("Choose a shortcut from 1 to 9, or leave it empty.")
        code_id = uuid4().hex
        try:
            with self.db.lock, self.db.connection:
                self.db.connection.execute("BEGIN IMMEDIATE")
                self.db.connection.execute(
                    "INSERT INTO workbench_discovery_code "
                    "(code_id,matter_id,label,position,active,digit,created_by,created_at) "
                    "SELECT ?,?,?,COALESCE(MAX(position),0)+1,1,?,?,? "
                    "FROM workbench_discovery_code WHERE matter_id=?",
                    (code_id, matter_id, label, digit, actor_id,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), matter_id))
                return next(c for c in self.list(matter_id) if c.code_id == code_id)
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise ValueError("That label or shortcut is already defined in this matter.") from exc
            raise CodeUnavailable("This matter does not accept code changes.") from exc
        except sqlite3.DatabaseError as exc:
            raise CodeUnavailable("This matter does not accept code changes.") from exc

    def retire(self, matter_id: str, code_id: str) -> None:
        try:
            with self.db.lock, self.db.connection:
                changed = self.db.connection.execute(
                    "UPDATE workbench_discovery_code SET active=0 WHERE matter_id=? AND code_id=?",
                    (matter_id, code_id)).rowcount
                if not changed:
                    raise KeyError(code_id)
        except sqlite3.DatabaseError as exc:
            raise CodeUnavailable("This matter does not accept code changes.") from exc

    def record(self, matter_id: str, document_id: str, code_id: str, actor_id: str, action: str) -> bool:
        if action not in {"apply", "remove"}:
            raise ValueError("Choose apply or remove.")
        try:
            with self.db.lock, self.db.connection:
                self.db.connection.execute("BEGIN IMMEDIATE")
                if not any(c.code_id == code_id for c in self.list(matter_id)):
                    raise KeyError(code_id)
                previous = next(iter(self.db.connection.execute(
                    "SELECT action FROM workbench_discovery_code_event "
                    "WHERE matter_id=? AND document_id=? AND code_id=? ORDER BY event_id DESC LIMIT 1",
                    (matter_id, document_id, code_id))), None)
                if (previous[0] if previous else "remove") == action:
                    return False
                self.db.connection.execute(
                    "INSERT INTO workbench_discovery_code_event "
                    "(matter_id,document_id,code_id,action,created_by,created_at) VALUES (?,?,?,?,?,?)",
                    (matter_id, document_id, code_id, action, actor_id,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                return True
        except sqlite3.DatabaseError as exc:
            raise CodeUnavailable("This matter does not accept code changes.") from exc

    def current_rows(self, matter_id: str):
        with self.db.lock:
            return tuple(self.db.connection.execute(
                "SELECT e.document_id,e.code_id,c.label,c.active,e.created_by,e.created_at FROM workbench_discovery_code_event e "
                "JOIN workbench_discovery_code c ON c.matter_id=e.matter_id AND c.code_id=e.code_id "
                "WHERE e.matter_id=? AND e.action='apply' AND e.event_id=("
                "SELECT MAX(n.event_id) FROM workbench_discovery_code_event n "
                "WHERE n.matter_id=e.matter_id AND n.document_id=e.document_id AND n.code_id=e.code_id) "
                "ORDER BY c.position,c.code_id", (matter_id,)))

    def applied(self, matter_id: str) -> dict[str, tuple[str, ...]]:
        found: dict[str, list[str]] = {}
        for document_id, code_id, *_ in self.current_rows(matter_id):
            found.setdefault(document_id, []).append(code_id)
        return {document: tuple(codes) for document, codes in found.items()}

    def controls(self, matter_id: str, document_id: str) -> list[dict]:
        with self.db.lock:
            applied = self.applied(matter_id).get(document_id, ())
            return [{"id": code.code_id, "label": code.label, "applied": code.code_id in applied,
                     "key": str(code.digit) if code.digit is not None else ""}
                    for code in self.list(matter_id)]

    def project(self, matter_id: str, index):
        """Overlay live labels without mutating the shared source index or its sort cache."""
        entries: dict[str, list[dict]] = {}
        for document_id, code_id, label, active, created_by, created_at in self.current_rows(matter_id):
            entries.setdefault(document_id, []).append({
                "value": label if active else f"{label} (retired)", "active": bool(active),
                "created_by": created_by, "created_at": created_at,
            })
        keys = [f.key for f in index.definition.fields if f.computed == "review_codes"]
        items = []
        for item in index.items:
            details = tuple(entries.get(item.document_id, ()))
            value = ", ".join(entry["value"] for entry in details)
            items.append(replace(item, values={**item.values, **dict.fromkeys(keys, value)},
                                 details={**item.details, **dict.fromkeys(keys, details)}))
        filled = frozenset(key for item in items for key, value in item.values.items() if value)
        return replace(index, items=tuple(items), filled=filled, orders=OrderedDict())

    def current_page(self, matter_id: str, document_ids, *, after=None, limit: int = 500):
        """Seek effective events for a bounded document batch, including retired labels."""
        if len(document_ids) > 500 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("effective code reads accept at most 500 documents and rows")
        if not document_ids:
            return ()
        marks = ",".join("?" for _ in document_ids)
        boundary, args = "", [matter_id, *document_ids]
        if after is not None:
            position, code_id, document_id = after
            boundary = " AND (c.position>? OR (c.position=? AND c.code_id>?) OR (c.position=? AND c.code_id=? AND e.document_id>?))"
            args.extend((position, position, code_id, position, code_id, document_id))
        with self.db.lock:
            return tuple(self.db.connection.execute(
                "SELECT e.document_id,e.code_id,c.label,c.active,e.created_by,e.created_at,c.position "
                "FROM workbench_discovery_code_event e JOIN workbench_discovery_code c "
                "ON c.matter_id=e.matter_id AND c.code_id=e.code_id "
                f"WHERE e.matter_id=? AND e.document_id IN ({marks}) AND e.action='apply' "
                "AND e.event_id=(SELECT MAX(n.event_id) FROM workbench_discovery_code_event n "
                "WHERE n.matter_id=e.matter_id AND n.document_id=e.document_id AND n.code_id=e.code_id)" + boundary +
                " ORDER BY c.position,c.code_id,e.document_id LIMIT ?", (*args, limit)))

    def change_token(self, matter_id: str):
        """Read the event watermark and definition state without materializing events."""
        with self.db.lock:
            event = next(iter(self.db.connection.execute(
                "SELECT event_id FROM workbench_discovery_code_event WHERE matter_id=? ORDER BY event_id DESC LIMIT 1",
                (matter_id,))), (0,))[0]
            definitions = tuple(next(iter(self.db.connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(active),0) FROM workbench_discovery_code WHERE matter_id=?", (matter_id,)))))
            return (event, *definitions)
