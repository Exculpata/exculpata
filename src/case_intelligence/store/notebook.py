"""Notebook operations on the shared workspace connection and lock."""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from . import (
    NOTEBOOK_ORIGINS,
    NOTEBOOK_STATUSES,
    NOTEBOOK_TYPES,
    _IDENTIFIER,
    _NOTEBOOK_ITEM,
    _SOURCE_DOCUMENT,
)
from ..store_records import (
    AnswerNotebookContextRecord,
    NotebookEditConflict,
    NotebookItemRecord,
    NotebookPageRecord,
    NotebookReferenceRecord,
    WorkspaceProblem,
)


class NotebookStoreMixin:
    """State-free notebook behavior supplied by WorkspaceStore."""

    @staticmethod
    def _notebook_reference(row: sqlite3.Row) -> NotebookReferenceRecord:
        values = dict(row)
        values["ordinal"] = int(values["ordinal"])
        values["unit_number"] = int(values["unit_number"])
        return NotebookReferenceRecord(**values)

    @staticmethod
    def _notebook_item(row: sqlite3.Row) -> NotebookItemRecord:
        values = dict(row)
        values.pop("dedupe_key", None)
        values["is_pinned"] = int(values["is_pinned"])
        values["reference_count"] = int(values.get("reference_count", 0))
        return NotebookItemRecord(**values)

    @staticmethod
    def _notebook_choice(value: str, choices: Sequence[str], label: str) -> str:
        normalized = (value or "").strip().casefold()
        if normalized not in choices:
            raise WorkspaceProblem(f"Choose a valid notebook {label}.")
        return normalized

    def _prepare_notebook_reference(
        self, value: Mapping[str, object]
    ) -> dict[str, object]:
        document_id = self._safe_text(
            str(value.get("document_id") or ""),
            label="Notebook source",
            maximum=100,
        )
        source_version_id = self._safe_text(
            str(value.get("source_version_id") or ""),
            label="Notebook source version",
            maximum=100,
        )
        source_name = self._safe_text(
            str(value.get("source_name") or ""),
            label="Notebook source name",
            maximum=240,
        )
        location = self._safe_text(
            str(value.get("location") or ""),
            label="Notebook source location",
            maximum=160,
        )
        chunk_id = self._safe_text(
            str(value.get("chunk_id") or ""),
            label="Notebook source section",
            maximum=80,
        )
        excerpt_digest = str(value.get("excerpt_digest") or "").strip().casefold()
        support_token = str(value.get("support_token") or "").strip().casefold()
        excerpt = self._source_text(
            str(value.get("excerpt") or ""),
            label="Notebook source excerpt",
            maximum=6_000,
            multiline=True,
        )
        try:
            unit_number = int(value.get("unit_number") or 0)
        except (TypeError, ValueError) as exc:
            raise WorkspaceProblem("The notebook source section is invalid.") from exc
        if (
            not _SOURCE_DOCUMENT.fullmatch(document_id)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", source_version_id)
            or not re.fullmatch(r"chunk-[1-9][0-9]{0,7}", chunk_id)
            or not re.fullmatch(r"[0-9a-f]{64}", excerpt_digest)
            or not re.fullmatch(r"[0-9a-f]{40}", support_token)
            or not 1 <= unit_number <= 10_000_000
        ):
            raise WorkspaceProblem("The notebook source reference is invalid.")
        return {
            "document_id": document_id,
            "source_version_id": source_version_id,
            "source_name": source_name,
            "location": location,
            "unit_number": unit_number,
            "chunk_id": chunk_id,
            "excerpt_digest": excerpt_digest,
            "excerpt": excerpt,
            "support_token": support_token,
        }

    def create_notebook_item(
        self,
        matter_id: str,
        actor_id: str,
        *,
        item_type: str,
        status: str,
        title: str,
        body: str = "",
        date_label: str = "",
        pinned: bool = False,
        origin: str = "manual",
        source_conversation_id: str | None = None,
        source_message_id: str | None = None,
        dedupe_key: str | None = None,
        references: Sequence[Mapping[str, object]] = (),
    ) -> tuple[NotebookItemRecord, bool]:
        actor = self.membership(matter_id, actor_id).principal_id
        kind = self._notebook_choice(item_type, NOTEBOOK_TYPES, "type")
        state = self._notebook_choice(status, NOTEBOOK_STATUSES, "status")
        source_kind = self._notebook_choice(origin, NOTEBOOK_ORIGINS, "origin")
        heading = self._derived_text(title, label="Notebook title", maximum=160)
        content = self._derived_text(
            body, label="Notebook details", maximum=20_000, required=False, multiline=True
        )
        date_value = self._derived_text(
            date_label, label="Notebook date", maximum=100, required=False
        )
        if kind in {"date", "event"} and not (content or date_value):
            raise WorkspaceProblem("Add a date or details for this notebook item.")
        if len(references) > 12:
            raise WorkspaceProblem("A notebook item can contain at most 12 source references.")
        prepared_references = tuple(
            self._prepare_notebook_reference(reference) for reference in references
        )
        if source_kind != "manual" and not prepared_references:
            raise WorkspaceProblem("Saved answer and source suggestions must retain source support.")
        conversation_id = (source_conversation_id or "").strip() or None
        message_id = (source_message_id or "").strip() or None
        if conversation_id is not None and not _IDENTIFIER.fullmatch(conversation_id):
            raise WorkspaceProblem("The originating conversation is invalid.")
        if message_id is not None and not _IDENTIFIER.fullmatch(message_id):
            raise WorkspaceProblem("The originating answer is invalid.")
        dedupe = None
        if dedupe_key is not None:
            dedupe = self._safe_text(
                dedupe_key,
                label="Notebook suggestion key",
                maximum=200,
                required=False,
            ).casefold() or None
        now = self._now()
        item_id = f"notebook-item-{uuid.uuid4().hex}"
        with self._lock, self.connection:
            if dedupe is not None:
                existing = self.connection.execute(
                    "SELECT item_id FROM workbench_notebook_item "
                    "WHERE matter_id=? AND dedupe_key=?",
                    (matter_id, dedupe),
                ).fetchone()
                if existing is not None:
                    return self._notebook_item_by_id_locked(
                        matter_id, existing["item_id"]
                    ), False
            if conversation_id is not None:
                bound_conversation = self.connection.execute(
                    "SELECT 1 FROM workbench_conversation "
                    "WHERE conversation_id=? AND matter_id=?",
                    (conversation_id, matter_id),
                ).fetchone()
                if bound_conversation is None:
                    raise KeyError(conversation_id)
            if message_id is not None:
                bound_message = self.connection.execute(
                    "SELECT 1 FROM workbench_message message "
                    "JOIN workbench_conversation conversation "
                    "ON conversation.conversation_id=message.conversation_id "
                    "WHERE message.message_id=? AND conversation.matter_id=?"
                    + (" AND conversation.conversation_id=?" if conversation_id else ""),
                    (message_id, matter_id, conversation_id)
                    if conversation_id
                    else (message_id, matter_id),
                ).fetchone()
                if bound_message is None:
                    raise KeyError(message_id)
            self.connection.execute(
                "INSERT INTO workbench_notebook_item("
                "item_id,matter_id,item_type,status,title,body,date_label,is_pinned,origin,"
                "source_conversation_id,source_message_id,dedupe_key,created_by,created_at,"
                "updated_by,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id, matter_id, kind, state, heading, content, date_value,
                    1 if pinned else 0, source_kind, conversation_id, message_id,
                    dedupe, actor, now, actor, now,
                ),
            )
            for ordinal, reference in enumerate(prepared_references, 1):
                self.connection.execute(
                    "INSERT INTO workbench_notebook_reference("
                    "reference_id,item_id,matter_id,ordinal,document_id,source_version_id,"
                    "source_name,location,unit_number,chunk_id,excerpt_digest,excerpt,"
                    "support_token,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"notebook-reference-{uuid.uuid4().hex}", item_id, matter_id,
                        ordinal, reference["document_id"], reference["source_version_id"],
                        reference["source_name"], reference["location"],
                        reference["unit_number"], reference["chunk_id"],
                        reference["excerpt_digest"], reference["excerpt"],
                        reference["support_token"], now,
                    ),
                )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?",
                (now, matter_id),
            )
            item = self._notebook_item_by_id_locked(matter_id, item_id)
        return item, True

    def _notebook_item_by_id_locked(
        self, matter_id: str, item_id: str
    ) -> NotebookItemRecord:
        row = self.connection.execute(
            "SELECT item.*,created.display_name AS created_by_name,"
            "updated.display_name AS updated_by_name,"
            "(SELECT COUNT(*) FROM workbench_notebook_reference reference "
            "WHERE reference.item_id=item.item_id) AS reference_count "
            "FROM workbench_notebook_item item "
            "JOIN workbench_principal created ON created.principal_id=item.created_by "
            "JOIN workbench_principal updated ON updated.principal_id=item.updated_by "
            "WHERE item.matter_id=? AND item.item_id=?",
            (matter_id, item_id),
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return self._notebook_item(row)

    def notebook_item(
        self,
        matter_id: str,
        actor_id: str,
        item_id: str,
        *,
        administrator_override: bool = False,
    ) -> NotebookItemRecord:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        if not _NOTEBOOK_ITEM.fullmatch(item_id):
            raise KeyError(item_id)
        with self._lock:
            return self._notebook_item_by_id_locked(matter_id, item_id)

    def notebook_references(
        self,
        matter_id: str,
        actor_id: str,
        item_id: str,
        *,
        administrator_override: bool = False,
    ) -> tuple[NotebookReferenceRecord, ...]:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        if not _NOTEBOOK_ITEM.fullmatch(item_id):
            raise KeyError(item_id)
        with self._lock:
            item = self.connection.execute(
                "SELECT 1 FROM workbench_notebook_item WHERE matter_id=? AND item_id=?",
                (matter_id, item_id),
            ).fetchone()
            if item is None:
                raise KeyError(item_id)
            rows = self.connection.execute(
                "SELECT reference_id,item_id,matter_id,ordinal,document_id,"
                "source_version_id,source_name,location,unit_number,chunk_id,"
                "excerpt_digest,excerpt,support_token,created_at "
                "FROM workbench_notebook_reference WHERE matter_id=? AND item_id=? "
                "ORDER BY ordinal",
                (matter_id, item_id),
            ).fetchall()
        return tuple(self._notebook_reference(row) for row in rows)

    @staticmethod
    def _notebook_like(value: str) -> str:
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def notebook_page(
        self,
        matter_id: str,
        actor_id: str,
        *,
        administrator_override: bool = False,
        query: str = "",
        item_type: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 24,
    ) -> NotebookPageRecord:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        query_value = " ".join(
            self._safe_text(
                query, label="Notebook search", maximum=200, required=False
            ).split()
        )
        kind = (item_type or "").strip().casefold()
        state = (status or "").strip().casefold()
        if kind and kind not in NOTEBOOK_TYPES:
            raise WorkspaceProblem("Choose a valid notebook type.")
        if state and state not in (*NOTEBOOK_STATUSES, "all"):
            raise WorkspaceProblem("Choose a valid notebook status.")
        try:
            page_value = max(int(page), 1)
            page_size_value = min(max(int(page_size), 10), 100)
        except (TypeError, ValueError) as exc:
            raise WorkspaceProblem("The notebook page is invalid.") from exc
        clauses = ["item.matter_id=?"]
        parameters: list[object] = [matter_id]
        if query_value:
            clauses.append(
                "(item.title LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
                "item.body LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
                "item.date_label LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            pattern = self._notebook_like(query_value)
            parameters.extend((pattern, pattern, pattern))
        if kind:
            clauses.append("item.item_type=?")
            parameters.append(kind)
        if state and state != "all":
            clauses.append("item.status=?")
            parameters.append(state)
        elif not state:
            clauses.append("item.status<>'dismissed'")
        where = " AND ".join(clauses)
        with self._lock:
            total = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_notebook_item item WHERE " + where,
                    parameters,
                ).fetchone()[0]
            )
            total_pages = max((total + page_size_value - 1) // page_size_value, 1)
            page_value = min(page_value, total_pages)
            offset = (page_value - 1) * page_size_value
            rows = self.connection.execute(
                "SELECT item.*,created.display_name AS created_by_name,"
                "updated.display_name AS updated_by_name,"
                "(SELECT COUNT(*) FROM workbench_notebook_reference reference "
                "WHERE reference.item_id=item.item_id) AS reference_count "
                "FROM workbench_notebook_item item "
                "JOIN workbench_principal created ON created.principal_id=item.created_by "
                "JOIN workbench_principal updated ON updated.principal_id=item.updated_by "
                "WHERE " + where + " ORDER BY item.is_pinned DESC,item.updated_at DESC,"
                "item.item_id DESC LIMIT ? OFFSET ?",
                (*parameters, page_size_value, offset),
            ).fetchall()
            status_rows = self.connection.execute(
                "SELECT status,COUNT(*) AS count FROM workbench_notebook_item "
                "WHERE matter_id=? GROUP BY status",
                (matter_id,),
            ).fetchall()
            type_rows = self.connection.execute(
                "SELECT item_type,COUNT(*) AS count FROM workbench_notebook_item "
                "WHERE matter_id=? AND status<>'dismissed' GROUP BY item_type",
                (matter_id,),
            ).fetchall()
            overall = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM workbench_notebook_item WHERE matter_id=?",
                    (matter_id,),
                ).fetchone()[0]
            )
        counts = {value: 0 for value in NOTEBOOK_STATUSES}
        counts.update({row["status"]: int(row["count"]) for row in status_rows})
        counts["all"] = overall
        type_counts = {value: 0 for value in NOTEBOOK_TYPES}
        type_counts.update({row["item_type"]: int(row["count"]) for row in type_rows})
        return NotebookPageRecord(
            tuple(self._notebook_item(row) for row in rows),
            page_value,
            page_size_value,
            total,
            total_pages,
            counts,
            type_counts,
            query_value,
            kind,
            state,
        )

    def all_notebook_items(
        self,
        matter_id: str,
        actor_id: str,
        *,
        include_dismissed: bool = True,
        limit: int = 10_000,
        administrator_override: bool = False,
    ) -> tuple[NotebookItemRecord, ...]:
        self._authorize_export_read(
            matter_id,
            actor_id,
            administrator_override=administrator_override,
        )
        limit_value = min(max(int(limit), 1), 10_000)
        with self._lock:
            rows = self.connection.execute(
                "SELECT item.*,created.display_name AS created_by_name,"
                "updated.display_name AS updated_by_name,"
                "(SELECT COUNT(*) FROM workbench_notebook_reference reference "
                "WHERE reference.item_id=item.item_id) AS reference_count "
                "FROM workbench_notebook_item item "
                "JOIN workbench_principal created ON created.principal_id=item.created_by "
                "JOIN workbench_principal updated ON updated.principal_id=item.updated_by "
                "WHERE item.matter_id=?" + ("" if include_dismissed else " AND item.status<>'dismissed'")
                + " ORDER BY item.is_pinned DESC,item.updated_at DESC,item.item_id DESC LIMIT ?",
                (matter_id, limit_value),
            ).fetchall()
        return tuple(self._notebook_item(row) for row in rows)

    def _notebook_item_for_edit_locked(
        self, matter_id: str, actor_id: str, item_id: str, expected_updated_at: str,
    ) -> NotebookItemRecord:
        # Callers hold an immediate transaction across the final authority and
        # revision checks and the mutation, including independent connections.
        self.membership(matter_id, actor_id)
        if not _NOTEBOOK_ITEM.fullmatch(item_id):
            raise KeyError(item_id)
        current = self._notebook_item_by_id_locked(matter_id, item_id)
        if not expected_updated_at or expected_updated_at != current.updated_at:
            raise NotebookEditConflict(
                "This case note changed since the page was opened. "
                "Review the saved note before trying again."
            )
        return current

    def _notebook_edit_time(self, current: NotebookItemRecord) -> str:
        # Existing timestamps are also edit tokens. Advance even if the clock
        # repeats or moves backwards so an earlier form cannot become current.
        previous = datetime.fromisoformat(current.updated_at.replace("Z", "+00:00"))
        return self._timestamp(max(self.current_time(), previous + timedelta(microseconds=1)))

    def update_notebook_item(
        self,
        matter_id: str,
        actor_id: str,
        item_id: str,
        *,
        expected_updated_at: str,
        item_type: str,
        status: str,
        title: str,
        body: str = "",
        date_label: str = "",
        pinned: bool = False,
    ) -> NotebookItemRecord:
        kind = self._notebook_choice(item_type, NOTEBOOK_TYPES, "type")
        state = self._notebook_choice(status, NOTEBOOK_STATUSES, "status")
        heading = self._derived_text(title, label="Notebook title", maximum=160)
        content = self._derived_text(
            body, label="Notebook details", maximum=20_000, required=False, multiline=True
        )
        date_value = self._derived_text(
            date_label, label="Notebook date", maximum=100, required=False
        )
        if kind in {"date", "event"} and not (content or date_value):
            raise WorkspaceProblem("Add a date or details for this notebook item.")
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self._notebook_item_for_edit_locked(matter_id, actor_id, item_id, expected_updated_at)
            now = self._notebook_edit_time(current)
            self.connection.execute(
                "UPDATE workbench_notebook_item SET item_type=?,status=?,title=?,body=?,"
                "date_label=?,is_pinned=?,updated_by=?,updated_at=? "
                "WHERE matter_id=? AND item_id=?",
                (kind, state, heading, content, date_value, 1 if pinned else 0,
                 actor_id, now, matter_id, item_id),
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (now, matter_id),
            )
            return self._notebook_item_by_id_locked(matter_id, item_id)

    def set_notebook_item_status(
        self, matter_id: str, actor_id: str, item_id: str, status: str, *, expected_updated_at: str,
    ) -> NotebookItemRecord:
        state = self._notebook_choice(status, NOTEBOOK_STATUSES, "status")
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self._notebook_item_for_edit_locked(matter_id, actor_id, item_id, expected_updated_at)
            now = self._notebook_edit_time(current)
            self.connection.execute(
                "UPDATE workbench_notebook_item SET status=?,updated_by=?,updated_at=? "
                "WHERE matter_id=? AND item_id=?", (state, actor_id, now, matter_id, item_id),
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (now, matter_id),
            )
            return self._notebook_item_by_id_locked(matter_id, item_id)

    def delete_notebook_item(
        self, matter_id: str, actor_id: str, item_id: str, *, expected_updated_at: str,
    ) -> NotebookItemRecord:
        with self._lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self._notebook_item_for_edit_locked(matter_id, actor_id, item_id, expected_updated_at)
            self.connection.execute(
                "DELETE FROM workbench_notebook_item WHERE matter_id=? AND item_id=?", (matter_id, item_id),
            )
            self.connection.execute(
                "UPDATE workbench_matter SET updated_at=? WHERE matter_id=?", (self._now(), matter_id),
            )
        return current

    def answer_notebook_context(
        self, matter_id: str, job_id: str
    ) -> tuple[str | None, tuple[AnswerNotebookContextRecord, ...]]:
        with self._lock:
            scope = self.connection.execute(
                "SELECT mode FROM workbench_answer_notebook_scope "
                "WHERE matter_id=? AND job_id=?",
                (matter_id, job_id),
            ).fetchone()
            if scope is None:
                return None, ()
            rows = self.connection.execute(
                "SELECT notebook_item_id,item_type,status,title,body,date_label,content_digest "
                "FROM workbench_answer_notebook_scope_item "
                "WHERE matter_id=? AND job_id=? ORDER BY ordinal",
                (matter_id, job_id),
            ).fetchall()
        return scope["mode"], tuple(
            AnswerNotebookContextRecord(**dict(row)) for row in rows
        )
