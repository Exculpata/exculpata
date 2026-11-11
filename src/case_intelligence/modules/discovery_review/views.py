"""Store each reviewer's chosen columns and item fields, per matter.

A matter may also hold one shared starting view. Rows are view state only:
field keys, never document content. They are deleted with the matter.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from case_intelligence.modules import ModuleDatabase

SCOPES = ("user", "matter")
MAX_STORED_CHARS = 16_000


class ViewUnavailable(RuntimeError):
    """The matter no longer accepts changes, or the view is too large."""


@dataclass(frozen=True, slots=True)
class SavedView:
    scope: str
    columns: tuple[str, ...]
    item_fields: tuple[str, ...]
    updated_at: str


def _keys(text: str) -> tuple[str, ...]:
    try:
        value = json.loads(text)
    except ValueError:
        return ()
    return tuple(key for key in value if isinstance(key, str)) if isinstance(value, list) else ()


class ViewRepository:
    def __init__(self, db: ModuleDatabase, authority):
        self.db, self.authority = db, authority

    def read(self, matter_id: str, principal_id: str) -> dict[str, SavedView]:
        """Return the reviewer's own view and the matter's shared view, when saved."""
        found = {}
        with self.db.lock:
            for scope, owner in (("user", principal_id), ("matter", "")):
                row = next(iter(self.db.connection.execute(
                    "SELECT columns,item_fields,updated_at FROM workbench_discovery_view "
                    "WHERE matter_id=? AND scope=? AND principal_id=?", (matter_id, scope, owner),
                )), None)
                if row is not None:
                    found[scope] = SavedView(scope, _keys(row[0]), _keys(row[1]), row[2])
        return found

    def save(self, matter_id: str, scope: str, principal_id: str, columns, item_fields) -> None:
        stored = [json.dumps(list(keys), ensure_ascii=False) for keys in (columns, item_fields)]
        if scope not in SCOPES or sum(map(len, stored)) > MAX_STORED_CHARS:
            raise ViewUnavailable("view is invalid")
        self._write(matter_id, principal_id,
            "INSERT INTO workbench_discovery_view(matter_id,scope,principal_id,columns,item_fields,updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(matter_id,scope,principal_id) DO UPDATE SET "
            "columns=excluded.columns,item_fields=excluded.item_fields,updated_at=excluded.updated_at",
            (matter_id, scope, principal_id if scope == "user" else "", *stored,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def clear(self, matter_id: str, scope: str, principal_id: str) -> None:
        if scope not in SCOPES:
            raise ViewUnavailable("view is invalid")
        self._write(matter_id, principal_id, "DELETE FROM workbench_discovery_view WHERE matter_id=? AND scope=? AND principal_id=?",
                    (matter_id, scope, principal_id if scope == "user" else ""))

    def _write(self, matter_id: str, actor_id: str, statement: str, values: tuple) -> None:
        try:
            with self.db.authorized_write(self.authority, matter_id, actor_id):
                self.db.connection.execute(statement, values)
        except sqlite3.DatabaseError as exc:
            raise ViewUnavailable("matter does not accept view changes") from exc
