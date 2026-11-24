"""Matter-scoped preservation and bounded reads for production load-file records.

Original values are immutable. Associations are staged separately and become
current only through a validated completed attempt. The application supplies
the database connection, transaction lock, source guard and target validator.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextvars import ContextVar
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Mapping

from case_intelligence.modules import ModuleDatabase
from .matching import dat_text_paths, path_key

MAX_ORDINAL = 9_223_372_036_854_775_807
MAX_BATCH_RECORDS = 256
MAX_BATCH_BYTES = 4 * 1024 * 1024
TABLES = {
    "document": "workbench_discovery_dat_record",
    "page": "workbench_discovery_opt_page",
}
STATUSES = frozenset({"building", "complete", "failed", "stale"})
COUNT_NAMES = ("accepted", "rejected", "matched", "unmatched", "conflicted")
UNCHANGED = object()
KEY_NAMES = frozenset({"path_key", "begin_key", "end_key", "control_key", "family_begin_key",
                       "family_end_key", "family_prefix", "family_low", "family_high", "page_label_key", "group_key"})
NAME_EXPR = "substr(c.display_name_key,length(rtrim(c.display_name_key,replace(c.display_name_key,'/','')))+1)"
STEM_PREFIX = "rtrim(" + NAME_EXPR + ",replace(" + NAME_EXPR + ",'.',''))"
STEM_EXPR = "CASE WHEN length(" + STEM_PREFIX + ")=0 THEN " + NAME_EXPR + " ELSE substr(" + NAME_EXPR + ",1,length(" + STEM_PREFIX + ")-1) END"


class ProductionUnavailable(RuntimeError):
    """The owned production cannot currently be read or changed safely."""


class ProductionStale(ProductionUnavailable):
    """The caller's source or publication target is no longer current."""


class ProductionConflict(ValueError):
    """An immutable record or ownership assertion contradicts stored evidence."""


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    document_id: str
    version_id: str
    digest: str
    name: str
    relative_path: str
    source_format: str
    root_key: str = ""

    def data(self) -> tuple[str, ...]:
        return (self.document_id, self.version_id, self.digest, self.name, self.relative_path,
                self.source_format, self.root_key)


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def ordinal_value(value, *, allow_zero=False) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= MAX_ORDINAL:
        raise ValueError("record ordinal is outside the supported integer domain")
    return value


def bounded_limit(value, maximum=100) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("bounded read limit is invalid")
    return value


def string_array(values, *, nullable=False):
    if values is None and nullable:
        return None
    if not isinstance(values, (tuple, list)) or any(not isinstance(value, str) for value in values):
        raise ValueError("record cells and diagnostics must be ordered strings")
    return list(values)


def pair_array(values):
    if not isinstance(values, (tuple, list)):
        raise ValueError("optional values must be ordered string pairs")
    result = []
    for pair in values:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2 or any(not isinstance(v, str) for v in pair):
            raise ValueError("optional values must be ordered string pairs")
        result.append(list(pair))
    return result


def event_value(event, name, default=None):
    if isinstance(event, Mapping):
        return event.get(name, default)
    return {'kind': event.kind, 'category': event.category, 'source_format': event.source_format,
            'ordinal': event.ordinal, 'line': event.line, 'headers': event.headers, 'cells': event.cells,
            'diagnostics': event.diagnostics, 'optional_values': event.optional_values}.get(name, default)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def result_rows(cursor) -> tuple[dict, ...]:
    names = tuple(entry[0] for entry in cursor.description)
    return tuple(dict(zip(names, row)) for row in cursor)


def pack_cursor_value(value, depth=0):
    """Represent cursor maps as explicit ordered pairs for portable metadata."""
    if depth > 64:
        raise ValueError("production cursor nesting is invalid")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("production cursor map keys must be text")
        return ["map", [[key, pack_cursor_value(child, depth + 1)] for key, child in value.items()]]
    if isinstance(value, (list, tuple)):
        return ["list", [pack_cursor_value(child, depth + 1) for child in value]]
    if value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value)):
        return value
    raise ValueError("production cursor contains an unsupported value")


def unpack_cursor_value(value, depth=0):
    if depth > 64:
        raise ValueError("production cursor nesting is invalid")
    if isinstance(value, list):
        if (len(value) != 2 or not isinstance(value[0], str) or value[0] not in {"map", "list"}
                or not isinstance(value[1], list)):
            raise ValueError("production cursor container is invalid")
        if value[0] == "list":
            return [unpack_cursor_value(child, depth + 1) for child in value[1]]
        result = {}
        for pair in value[1]:
            if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                    or pair[0] in result):
                raise ValueError("production cursor map entry is invalid")
            result[pair[0]] = unpack_cursor_value(pair[1], depth + 1)
        return result
    if value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value)):
        return value
    raise ValueError("production cursor contains an unsupported value")


def encode_cursor(value):
    if not isinstance(value, Mapping):
        raise ValueError("production cursor must be a map")
    return encoded(pack_cursor_value(value))


def decode_cursor(value):
    parsed = json.loads(value)
    if parsed == {}:
        return {}  # Migration defaults and earlier empty fixture checkpoints.
    result = unpack_cursor_value(parsed)
    if not isinstance(result, dict):
        raise ValueError("production cursor must be a map")
    return result


def decode_attempt(row: dict | None):
    if row is not None:
        row["cursor"] = decode_cursor(row["cursor_json"])
        row['dependencies'] = {}
        try:
            revision = json.loads(row['input_revision'])
        except (ValueError, TypeError):
            revision = None
        if isinstance(revision, dict) and set(revision) == {'revision', 'dependencies'}:
            row['input_revision'] = revision['revision']
            row['dependencies'] = revision['dependencies']
        row["headers"] = json.loads(row["headers_json"])
        row["profile_positions"] = json.loads(row["profile_positions_json"])
        row["counts"] = {key: int(row[key + "_count"]) for key in COUNT_NAMES}
    return row


def decode_record(row: dict, category: str) -> dict:
    row["category"] = category
    row["profile_positions"] = json.loads(row["profile_positions_json"])
    for key in ("headers", "cells", "diagnostics", "optional_values", "semantic", "association_diagnostics"):
        row[key] = json.loads(row[key + "_json"])
    row["values"] = row["cells"]
    row["source_document_id"] = row["source_id"]
    row["source_version_id"] = row["version_id"]
    row["source_name"] = row["name"]
    row.setdefault("group_ordinal", None)
    row.setdefault("group_key", "")
    row["family"] = {"root": row["family_root"], "parent": row["family_parent"], "order": row["family_order"]}
    return row


def merge_failure(existing, kind, entry):
    """Keep header and stream citations together without inventing a data row."""
    if not existing:
        envelope = {}
    else:
        try:
            value = json.loads(existing)
        except (ValueError, TypeError):
            value = existing
        envelope = value if isinstance(value, dict) else {"file" if isinstance(value, list) else "message": value}
    if kind in envelope and envelope[kind] != entry:
        raise ProductionConflict("An immutable source failure changed on replay.")
    envelope[kind] = entry
    return encoded(envelope)


class CountedCursor:
    def __init__(self, cursor, budget, constituents):
        self.cursor, self.budget, self.constituents = cursor, budget, constituents
        self.description = cursor.description

    def fetchone(self):
        row = self.cursor.fetchone()
        self.budget.charge(self.constituents if row is not None else 0, credit=row is None)
        return row

    def fetchall(self):
        return list(self)

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class ProductionRepository:
    """Own production SQL while borrowing the workspace transaction boundary."""

    def __init__(self, db: ModuleDatabase):
        self.db = db
        self.accounting = ContextVar('production_row_budget', default=None)
        self.last_batch_records = 0
        self.last_batch_bytes = 0

    @contextmanager
    def counted(self, budget):
        previous = self.accounting.set(budget)
        try:
            yield
        finally:
            self.accounting.reset(previous)

    def budget(self):
        return self.accounting.get()

    def explain_read(self, statement):
        with self.db.lock:
            return tuple(tuple(row) for row in self.execute('EXPLAIN QUERY PLAN ' + statement))

    def execute(self, statement, parameters=()):
        cursor = self.db.connection.execute(statement, parameters)
        budget = self.budget()
        if budget is None:
            return cursor
        if cursor.description is None:
            if cursor.rowcount >= 0:
                budget.charge(cursor.rowcount, credit=not cursor.rowcount)
            return cursor
        return CountedCursor(cursor, budget, 1 + statement.upper().count(' JOIN '))

    def retarget(self, matter_id, attempt_id, target):
        with self.transaction():
            self.execute('UPDATE workbench_discovery_production SET target=? WHERE matter_id=? AND attempt_id=?',
                         (target, matter_id, attempt_id))

    @contextmanager
    def transaction(self):
        """Nested public methods participate in the same outer atomic step."""
        with self.db.lock:
            marker = "production_" + uuid.uuid4().hex
            self.execute("SAVEPOINT " + marker)
            try:
                yield self.db.connection
                self.execute("RELEASE SAVEPOINT " + marker)
            except BaseException as error:
                self.execute("ROLLBACK TO SAVEPOINT " + marker)
                self.execute("RELEASE SAVEPOINT " + marker)
                if isinstance(error, sqlite3.DatabaseError):
                    raise ProductionUnavailable("Production metadata cannot be changed in this matter.") from error
                raise

    @contextmanager
    def step(self, matter_id, attempt_id, *, target, validate: Callable[[], bool] | None = None,
             expected_serial=None, expected_cursor=None):
        """Commit only while the caller's guarded source/target remains current.

        The s2 caller holds its source guard outside this transaction and supplies
        the full epoch/revision/identifier/Coding validator. Capacity is checked
        before entering this method. Nested repository writes never commit it.
        """
        with self.transaction():
            before = self.attempt(matter_id, attempt_id)
            if (before is None or before["status"] != "building" or before["target"] != target
                    or (expected_serial is not None and before["invalidation_serial"] != expected_serial)
                    or (expected_cursor is not None and before["cursor"] != expected_cursor)
                    or (validate is not None and not validate())):
                raise ProductionStale("Production inputs changed. Reload to continue.")
            yield before
            after = self.attempt(matter_id, attempt_id)
            if (after is None or after["status"] == "stale" or after["target"] != target
                    or after["invalidation_serial"] != before["invalidation_serial"]
                    or (validate is not None and not validate())):
                raise ProductionStale("Production inputs changed before publication.")

    def attempt(self, matter_id, attempt_id):
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT * FROM workbench_discovery_production WHERE matter_id=? AND attempt_id=?",
                (matter_id, attempt_id)))
        return decode_attempt(rows[0]) if rows else None

    def current_attempt(self, matter_id, source_id):
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT * FROM workbench_discovery_production WHERE matter_id=? AND document_id=? AND status<>'stale'",
                (matter_id, source_id)))
        return decode_attempt(rows[0]) if rows else None

    def attempts(self, matter_id, *, after="", limit=100, root_key=None, include_stale=False):
        bounded_limit(limit)
        where, parameters = ["matter_id=?", "attempt_id>?"], [matter_id, after]
        if not include_stale:
            where.append("status<>'stale'")
        if root_key is not None:
            where.append("root_key=?")
            parameters.append(root_key)
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT * FROM workbench_discovery_production WHERE " + " AND ".join(where)
                + " ORDER BY attempt_id LIMIT ?", (*parameters, limit)))
        return tuple(decode_attempt(row) for row in rows)

    def begin_attempt(self, matter_id, source: SourceIdentity, *, parser_revision, definition_digest, target, input_revision=""):
        if (not isinstance(source, SourceIdentity) or any(not isinstance(v, str) for v in source.data())
                or not source.document_id or not source.version_id or not source.source_format
                or any(not isinstance(v, str) or not v for v in (parser_revision, definition_digest, target))):
            raise ValueError("production source identity or revision is invalid")
        with self.transaction():
            prior = self.current_attempt(matter_id, source.document_id)
            if prior is not None:
                fields = ("document_id", "version_id", "digest", "name", "relative_path", "source_format", "root_key")
                if (tuple(prior[key] for key in fields) == source.data()
                        and prior["parser_revision"] == parser_revision
                        and prior["definition_digest"] == definition_digest and prior["target"] == target):
                    return prior["attempt_id"]
                self.execute(
                    "UPDATE workbench_discovery_production SET status='stale',updated_at=? "
                    "WHERE matter_id=? AND attempt_id=?", (now(), matter_id, prior["attempt_id"]))
            attempt_id, timestamp = uuid.uuid4().hex, now()
            self.execute(
                "INSERT INTO workbench_discovery_production "
                "(matter_id,attempt_id,document_id,version_id,digest,name,relative_path,source_format,root_key,"
                "parser_revision,definition_digest,target,input_revision,generation,status,phase,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'building','parse',?,?)",
                (matter_id, attempt_id, *source.data(), parser_revision, definition_digest, target,
                 str(input_revision), attempt_id, timestamp, timestamp))
            return attempt_id

    def require_building(self, matter_id, attempt_id):
        attempt = self.attempt(matter_id, attempt_id)
        if attempt is None:
            raise ProductionUnavailable("This production is not available in the matter.")
        if attempt["status"] != "building":
            raise ProductionStale("This production attempt no longer accepts changes.")
        return attempt

    def prepare_events(self, events):
        prepared, byte_count = [], 0
        for event in events:
            kind, category = event_value(event, "kind"), event_value(event, "category")
            if kind not in {"header", "accepted", "rejected", "file_failure"} or category not in TABLES:
                raise ValueError("production event kind or category is invalid")
            ordinal = ordinal_value(event_value(event, "ordinal"), allow_zero=kind == "header")
            if kind == "header" and ordinal != 0:
                raise ValueError("header events must not invent a record ordinal")
            line = ordinal_value(event_value(event, "line"))
            headers = encoded(string_array(event_value(event, "headers"), nullable=True))
            cells = encoded(string_array(event_value(event, "cells"), nullable=True))
            diagnostics = encoded(string_array(event_value(event, "diagnostics", ())))
            optional = encoded(pair_array(event_value(event, "optional_values", ())))
            raw = (kind, line, headers, cells, diagnostics, optional)
            size = sum(len(value.encode("utf-8")) for value in (headers, cells, diagnostics, optional))
            if len(prepared) >= MAX_BATCH_RECORDS or (prepared and byte_count + size > MAX_BATCH_BYTES):
                raise ValueError("production batch exceeds its record or decoded-payload bound")
            if prepared and byte_count > MAX_BATCH_BYTES:
                raise ValueError("an oversized production event must be persisted alone")
            prepared.append((event, category, ordinal, raw, hashlib.sha256(encoded(raw).encode("utf-8")).hexdigest()))
            byte_count += size
        return prepared, byte_count

    def append_events(self, matter_id, attempt_id, events, *, cursor=None):
        prepared, byte_count = self.prepare_events(events)
        inserted = 0
        with self.transaction():
            attempt = self.require_building(matter_id, attempt_id)
            accepted = rejected = 0
            for event, category, ordinal, raw, fingerprint in prepared:
                kind, line, headers, cells, diagnostics, optional = raw
                if event_value(event, "source_format", attempt["source_format"]) != attempt["source_format"]:
                    raise ProductionConflict("The event format differs from its admitted source.")
                if kind == "header":
                    if attempt["headers_json"] not in {"[]", headers}:
                        raise ProductionConflict("The source header changed within an immutable attempt.")
                    self.execute(
                        "UPDATE workbench_discovery_production SET headers_json=? WHERE matter_id=? AND attempt_id=?",
                        (headers, matter_id, attempt_id))
                    attempt["headers_json"] = headers
                    if json.loads(diagnostics):
                        failure = merge_failure(attempt["file_failure"], "header", [line, json.loads(diagnostics)])
                        self.execute(
                            "UPDATE workbench_discovery_production SET file_failure=? WHERE matter_id=? AND attempt_id=?",
                            (failure, matter_id, attempt_id))
                        attempt["file_failure"] = failure
                    continue
                if kind == "file_failure":
                    failure = merge_failure(attempt["file_failure"], "file", [ordinal, line, json.loads(diagnostics)])
                    self.execute(
                        "UPDATE workbench_discovery_production SET file_failure=? WHERE matter_id=? AND attempt_id=?",
                        (failure, matter_id, attempt_id))
                    attempt["file_failure"] = failure
                    continue
                table = TABLES[category]
                old = self.execute(
                    "SELECT raw_fingerprint FROM " + table + " WHERE matter_id=? AND attempt_id=? AND ordinal=?",
                    (matter_id, attempt_id, ordinal)).fetchone()
                if old is not None:
                    if old[0] != fingerprint:
                        raise ProductionConflict("An immutable production record changed on replay.")
                    continue
                self.execute(
                    "INSERT INTO " + table + " (matter_id,attempt_id,ordinal,kind,line,headers_json,cells_json,"
                    "diagnostics_json,optional_values_json,raw_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (matter_id, attempt_id, ordinal, *raw, fingerprint))
                inserted += 1
                accepted += kind == "accepted"
                rejected += kind == "rejected"
            self.execute(
                "UPDATE workbench_discovery_production SET accepted_count=accepted_count+?,"
                "rejected_count=rejected_count+?,updated_at=? WHERE matter_id=? AND attempt_id=?",
                (accepted, rejected, now(), matter_id, attempt_id))
            if cursor is not None:
                self.execute(
                    "UPDATE workbench_discovery_production SET cursor_json=? WHERE matter_id=? AND attempt_id=?",
                    (encode_cursor(cursor), matter_id, attempt_id))
        self.last_batch_records, self.last_batch_bytes = len(prepared), byte_count
        return inserted

    def update_attempt(self, matter_id, attempt_id, *, status, phase, cursor=None, counts=None, failure="", input_revision=None):
        if status not in STATUSES or not isinstance(phase, str) or not phase or not isinstance(failure, str):
            raise ValueError("production attempt status is invalid")
        values, parameters = ["status=?", "phase=?", "updated_at=?"], [status, phase, now()]
        if input_revision is not None:
            values.append("input_revision=?")
            parameters.append(str(input_revision))
        if cursor is not None:
            values.append("cursor_json=?")
            parameters.append(encode_cursor(cursor))
        if failure:
            values.append("file_failure=?")
            parameters.append(failure)
        if counts is not None:
            if not isinstance(counts, Mapping) or set(counts) - set(COUNT_NAMES):
                raise ValueError("production coverage count names are invalid")
            for key, count in counts.items():
                ordinal_value(count, allow_zero=True)
                values.append(key + "_count=?")
                parameters.append(count)
        with self.transaction():
            attempt = self.attempt(matter_id, attempt_id)
            if attempt is None:
                raise ProductionUnavailable("This production is not available in the matter.")
            if attempt["status"] == "stale" and status != "stale":
                raise ProductionStale("A stale attempt cannot become current again.")
            if status == "complete" and (failure or attempt["file_failure"]):
                raise ProductionConflict("An unreadable source cannot be published complete.")
            if failure and attempt["file_failure"] and failure != attempt["file_failure"]:
                parameters[values.index("file_failure=?")] = merge_failure(attempt["file_failure"], "message", failure)
            self.execute(
                "UPDATE workbench_discovery_production SET " + ",".join(values)
                + " WHERE matter_id=? AND attempt_id=?", (*parameters, matter_id, attempt_id))

    def joined_select(self, category):
        if category not in TABLES:
            raise ValueError("unknown production record category")
        return ("SELECT r.*,p.document_id AS source_id,p.version_id,p.digest,p.name,p.relative_path,"
                "p.source_format,p.root_key,p.profile_positions_json,p.status AS attempt_status,p.generation AS attempt_generation "
                "FROM " + TABLES[category] + " r JOIN workbench_discovery_production p "
                "ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id ")

    def record(self, matter_id, attempt_id, ordinal, *, category):
        ordinal_value(ordinal)
        with self.db.lock:
            rows = result_rows(self.execute(
                self.joined_select(category) + "WHERE r.matter_id=? AND r.attempt_id=? AND r.ordinal=? LIMIT 1",
                (matter_id, attempt_id, ordinal)))
        return decode_record(rows[0], category) if rows else None

    def records(self, matter_id, attempt_id, *, category, after=0, limit=100, reverse=False,
                document_id="", group_ordinal=None):
        ordinal_value(after, allow_zero=True)
        bounded_limit(limit)
        where, parameters = ["r.matter_id=?", "r.attempt_id=?", "r.ordinal<?" if reverse else "r.ordinal>?"], [matter_id, attempt_id, after]
        if document_id:
            where.append("r.document_id=?")
            parameters.append(document_id)
        if group_ordinal is not None:
            if category != "page":
                raise ValueError("document records have no page group")
            ordinal_value(group_ordinal)
            where.append("r.group_ordinal=?")
            parameters.append(group_ordinal)
        with self.db.lock:
            rows = result_rows(self.execute(
                self.joined_select(category) + "WHERE " + " AND ".join(where)
                + " ORDER BY r.ordinal " + ("DESC" if reverse else "ASC") + " LIMIT ?", (*parameters, limit)))
        return tuple(decode_record(row, category) for row in rows)

    def competing_records(self, matter_id, document_id, *, after=None, limit=100, include_building=True):
        bounded_limit(limit)
        where = ["r.matter_id=?", "r.document_id=?", "r.generation=p.generation",
                 "p.status IN ('building','complete')" if include_building else "p.status='complete'"]
        parameters = [matter_id, document_id]
        if after is not None:
            attempt, ordinal = after
            ordinal_value(ordinal, allow_zero=True)
            where.append("(r.attempt_id>? OR (r.attempt_id=? AND r.ordinal>?))")
            parameters.extend((attempt, attempt, ordinal))
        with self.db.lock:
            rows = result_rows(self.execute(self.joined_select("document") + "WHERE "
                + " AND ".join(where) + " ORDER BY r.attempt_id,r.ordinal LIMIT ?", (*parameters, limit)))
        return tuple(decode_record(row, "document") for row in rows)

    def document_records(self, matter_id, document_id, *, after=None, limit=100, preferred_formats=()):
        if not preferred_formats:
            return self.competing_records(matter_id, document_id, after=after, limit=limit, include_building=False)
        bounded_limit(limit)
        if after is not None or len(preferred_formats) > 32 or any(not isinstance(value, str) for value in preferred_formats):
            raise ValueError("format-priority selection requires a bounded first page")
        priority = "CASE p.source_format " + " ".join("WHEN ? THEN " + str(index) for index, _ in enumerate(preferred_formats))
        priority += " ELSE " + str(len(preferred_formats)) + " END"
        with self.db.lock:
            rows = result_rows(self.execute(self.joined_select("document")
                + "WHERE r.matter_id=? AND r.document_id=? AND p.status='complete' "
                + "AND r.generation=p.generation ORDER BY " + priority
                + ",r.attempt_id,r.ordinal LIMIT ?", (matter_id, document_id, *preferred_formats, limit)))
        return tuple(decode_record(row, "document") for row in rows)

    def set_projection_fields(self, matter_id, attempt_id, ordinal, *, category="document", semantic=None,
                              raw_fingerprint=None, projection_fingerprint=None, **keys):
        ordinal_value(ordinal)
        allowed = KEY_NAMES - ({"page_label_key", "group_key"} if category == "document" else
                              {"begin_key", "end_key", "control_key", "family_begin_key", "family_end_key",
                               "family_prefix", "family_low", "family_high"})
        if set(keys) - allowed or any(not isinstance(value, str) for value in keys.values()):
            raise ValueError("projection comparison keys are invalid")
        values, parameters = [], []
        if semantic is not None:
            pairs = list(semantic.items()) if isinstance(semantic, Mapping) else semantic
            values.append("semantic_json=?")
            parameters.append(encoded(pair_array(pairs)))
        if projection_fingerprint is not None:
            if not isinstance(projection_fingerprint, str):
                raise ValueError("projection fingerprint must be text")
            values.append("projection_fingerprint=?")
            parameters.append(projection_fingerprint)
        for key, value in keys.items():
            values.append(key + "=?")
            parameters.append(value)
        if not values:
            return
        with self.transaction():
            self.require_building(matter_id, attempt_id)
            row = self.record(matter_id, attempt_id, ordinal, category=category)
            if row is None or (raw_fingerprint is not None and row["raw_fingerprint"] != raw_fingerprint):
                raise ProductionStale("The production record changed before projection.")
            self.execute("UPDATE " + TABLES[category] + " SET " + ",".join(values)
                + " WHERE matter_id=? AND attempt_id=? AND ordinal=?", (*parameters, matter_id, attempt_id, ordinal))

    def set_projection(self, matter_id, attempt_id, ordinal, semantic, raw_fingerprint, keys, *, category="document"):
        """Store the semantic equivalence hash separately from immutable event bytes."""
        aliases = {"path": "path_key", "begin_bates": "begin_key", "end_bates": "end_key",
                   "control_number": "control_key", "begin_attachment": "family_begin_key",
                   "end_attachment": "family_end_key", "bates": "page_label_key"}
        normalized = {aliases.get(key, key): value for key, value in keys.items()}
        self.set_projection_fields(matter_id, attempt_id, ordinal, category=category, semantic=semantic,
                                   projection_fingerprint=raw_fingerprint, **normalized)

    def set_association(self, matter_id, attempt_id, ordinal, *, category, document_id="", basis="",
                        status="unmatched", generation="", family=None, group_ordinal=UNCHANGED):
        ordinal_value(ordinal)
        if not all(isinstance(value, str) for value in (document_id, basis, status, generation)):
            raise ValueError("association identity is invalid")
        values = ["document_id=?", "basis=?", "association_status=?", "generation=?"]
        parameters = [document_id, basis, status, generation]
        if family is not None:
            if not isinstance(family, Mapping) or set(family) - {"root", "parent", "order"}:
                raise ValueError("family projection is invalid")
            for key in ("root", "parent", "order"):
                value = family.get(key, "")
                if not isinstance(value, str):
                    raise ValueError("family values must be text")
                values.append("family_" + key + "=?")
                parameters.append(value)
        if group_ordinal is not UNCHANGED:
            if category != "page":
                raise ValueError("document records have no page group")
            if group_ordinal is not None:
                ordinal_value(group_ordinal)
            values.append("group_ordinal=?")
            parameters.append(group_ordinal)
        with self.transaction():
            self.require_building(matter_id, attempt_id)
            count = self.execute("UPDATE " + TABLES[category] + " SET " + ",".join(values)
                + " WHERE matter_id=? AND attempt_id=? AND ordinal=?", (*parameters, matter_id, attempt_id, ordinal)).rowcount
            if count != 1:
                raise ProductionUnavailable("The production record is not available in this matter.")

    def set_diagnostics(self, matter_id, attempt_id, ordinal, diagnostics, *, category="document"):
        value = encoded(string_array(diagnostics))
        ordinal_value(ordinal)
        if category not in TABLES:
            raise ValueError("unknown production record category")
        with self.transaction():
            self.require_building(matter_id, attempt_id)
            count = self.execute(
                "UPDATE " + TABLES[category] + " SET association_diagnostics_json=? "
                "WHERE matter_id=? AND attempt_id=? AND ordinal=?", (value, matter_id, attempt_id, ordinal)).rowcount
            if count != 1:
                raise ProductionUnavailable("The production record is not available in this matter.")

    def set_page_group(self, matter_id, attempt_id, ordinal, *, group_ordinal, group_key="",
                       supplied_count="", observed_count=0):
        ordinal_value(ordinal)
        if group_ordinal is not None:
            ordinal_value(group_ordinal)
        ordinal_value(observed_count, allow_zero=True)
        if not isinstance(group_key, str) or not isinstance(supplied_count, str):
            raise ValueError("page group supplied values must be strings")
        with self.transaction():
            self.require_building(matter_id, attempt_id)
            count = self.execute(
                "UPDATE workbench_discovery_opt_page SET group_ordinal=?,group_key=?,supplied_count=?,observed_count=? "
                "WHERE matter_id=? AND attempt_id=? AND ordinal=?",
                (group_ordinal, group_key, supplied_count, observed_count, matter_id, attempt_id, ordinal)).rowcount
            if count != 1:
                raise ProductionUnavailable("The page record is not available in this matter.")

    def records_by_key(self, matter_id, *, key, value, category="document", root_key=None,
                       after=None, limit=100, include_building=True):
        bounded_limit(limit)
        if (key not in KEY_NAMES or category not in TABLES
                or (category == "document" and key in {"page_label_key", "group_key"})
                or (category == "page" and key not in {"page_label_key", "path_key", "group_key"})):
            raise ValueError("unsupported indexed production key")
        where = ["r.matter_id=?", "r." + key + "=?", "p.status IN ('building','complete')" if include_building else "p.status='complete'"]
        parameters = [matter_id, value]
        if root_key is not None:
            where.append("p.root_key=?")
            parameters.append(root_key)
        if after is not None:
            attempt, ordinal = after
            ordinal_value(ordinal, allow_zero=True)
            where.append("(r.attempt_id>? OR (r.attempt_id=? AND r.ordinal>?))")
            parameters.extend((attempt, attempt, ordinal))
        with self.db.lock:
            rows = result_rows(self.execute(self.joined_select(category) + "WHERE "
                + " AND ".join(where) + " ORDER BY r.attempt_id,r.ordinal LIMIT ?", (*parameters, limit)))
        return tuple(decode_record(row, category) for row in rows)

    def key_count(self, matter_id, *, key, value, category="document", root_key=None, maximum=2):
        """Return a capped ambiguity proof without decoding producer payloads."""
        bounded_limit(maximum)
        if (key not in KEY_NAMES or category not in TABLES
                or (category == "document" and key in {"page_label_key", "group_key"})
                or (category == "page" and key not in {"page_label_key", "path_key", "group_key"})):
            raise ValueError("unsupported indexed production key")
        where = ["r.matter_id=?", "r." + key + "=?", "p.status IN ('building','complete')"]
        parameters = [matter_id, value]
        if root_key is not None:
            where.append("p.root_key=?")
            parameters.append(root_key)
        with self.db.lock:
            return self.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM " + TABLES[category] + " r "
                "JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE "
                + " AND ".join(where) + " LIMIT ?)", (*parameters, maximum)).fetchone()[0]

    def matching_records(self, matter_id, root_key, *, key, value, category="document", after=None, limit=100):
        aliases = {"begin_bates": "begin_key", "end_bates": "end_key", "control_number": "control_key",
                   "begin_attachment": "family_begin_key", "end_attachment": "family_end_key", "path": "path_key"}
        return self.records_by_key(matter_id, key=aliases.get(key, key), value=value, category=category,
                                   root_key=root_key, after=after, limit=limit)

    def root_records(self, matter_id, root_key, *, category="document", after=None, limit=100):
        bounded_limit(limit)
        where, parameters = ["r.matter_id=?", "p.root_key=?", "p.status IN ('building','complete')"], [matter_id, root_key]
        if after is not None:
            attempt, ordinal = after
            ordinal_value(ordinal, allow_zero=True)
            where.append("(r.attempt_id>? OR (r.attempt_id=? AND r.ordinal>?))")
            parameters.extend((attempt, attempt, ordinal))
        with self.db.lock:
            rows = result_rows(self.execute(self.joined_select(category) + "WHERE "
                + " AND ".join(where) + " ORDER BY r.attempt_id,r.ordinal LIMIT ?", (*parameters, limit)))
        return tuple(decode_record(row, category) for row in rows)

    def family_members(self, matter_id, root_key, *, begin, end, after=None, limit=100):
        bounded_limit(limit)
        where = ["r.matter_id=?", "p.root_key=?", "p.status IN ('building','complete')",
                 "r.family_begin_key=?", "r.family_end_key=?", "r.generation=p.generation"]
        parameters = [matter_id, root_key, begin, end]
        if after is not None:
            attempt, ordinal = after
            ordinal_value(ordinal, allow_zero=True)
            where.append("(r.attempt_id>? OR (r.attempt_id=? AND r.ordinal>?))")
            parameters.extend((attempt, attempt, ordinal))
        with self.db.lock:
            rows = result_rows(self.execute(self.joined_select("document") + "WHERE "
                + " AND ".join(where) + " ORDER BY r.attempt_id,r.ordinal LIMIT ?", (*parameters, limit)))
        return tuple(decode_record(row, "document") for row in rows)

    def interval_overlap(self, matter_id, root_key, *, prefix, low, high, begin=None, end=None, after=None, limit=1):
        """Find a nonidentical interval witness using the persisted comparison index."""
        bounded_limit(limit)
        if any(not isinstance(value, str) for value in (prefix, low, high)) or not low or high < low:
            raise ValueError("family interval is invalid")
        where = ["r.matter_id=?", "r.family_prefix=?", "r.family_low<=?", "r.family_high>=?",
                 "p.root_key=?", "p.status IN ('building','complete')", "r.generation=p.generation"]
        parameters = [matter_id, prefix, high, low, root_key]
        if begin is not None or end is not None:
            if not isinstance(begin, str) or not isinstance(end, str):
                raise ValueError("family endpoint pair must contain text")
            where.append("(r.family_begin_key<>? OR r.family_end_key<>?)")
            parameters.extend((begin, end))
        else:
            where.append("(r.family_low<>? OR r.family_high<>?)")
            parameters.extend((low, high))
        if after is not None:
            attempt, ordinal = after
            ordinal_value(ordinal, allow_zero=True)
            where.append("(r.attempt_id>? OR (r.attempt_id=? AND r.ordinal>?))")
            parameters.extend((attempt, attempt, ordinal))
        with self.db.lock:
            rows = result_rows(self.execute(self.joined_select("document") + "WHERE "
                + " AND ".join(where) + " LIMIT ?", (*parameters, limit)))
        return tuple(decode_record(row, "document") for row in rows)

    def mark_family_checked(self, matter_id, attempt_id, ordinal, generation):
        ordinal_value(ordinal)
        if not isinstance(generation, str) or not generation:
            raise ValueError("family generation must be nonempty text")
        with self.transaction():
            attempt = self.require_building(matter_id, attempt_id)
            if attempt["generation"] != generation:
                raise ProductionStale("The family generation is no longer current.")
            changed = self.execute(
                "UPDATE workbench_discovery_dat_record SET family_checked_generation=? "
                "WHERE matter_id=? AND attempt_id=? AND ordinal=?", (generation, matter_id, attempt_id, ordinal)).rowcount
            if changed != 1:
                raise ProductionUnavailable("The family record is not available in this matter.")

    def coverage(self, matter_id, attempt_id):
        counts = dict.fromkeys(COUNT_NAMES, 0)
        with self.db.lock:
            for table in TABLES.values():
                rows = self.execute(
                    "SELECT kind,association_status,COUNT(*) FROM " + table
                    + " WHERE matter_id=? AND attempt_id=? GROUP BY kind,association_status", (matter_id, attempt_id))
                for kind, status, count in rows:
                    counts["accepted" if kind == "accepted" else "rejected"] += count
                    if kind == "accepted":
                        bucket = "matched" if status == "matched" else "unmatched" if status in {"pending", "unmatched"} else "conflicted"
                        counts[bucket] += count
        return counts

    def catalog(self, matter_id, document_id):
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT * FROM workbench_source_catalog WHERE matter_id=? AND document_id=?", (matter_id, document_id)))
        return rows[0] if rows else None

    def source_catalog_page(self, matter_id, *, after="", limit=100):
        bounded_limit(limit)
        with self.db.lock:
            return result_rows(self.execute(
                "SELECT * FROM workbench_source_catalog WHERE matter_id=? AND document_id>? "
                "ORDER BY document_id LIMIT ?", (matter_id, after, limit)))

    def provenance(self, matter_id, document_id, version_id) -> str:
        """Return one receipt/root proof, or empty when absent or ambiguous."""
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT DISTINCT t.receipt_id,i.relative_path FROM workbench_intake_transfer t "
                "JOIN workbench_upload_item u ON u.upload_item_id=t.upload_item_id AND u.matter_id=t.matter_id "
                "JOIN workbench_intake_item i ON i.receipt_id=t.receipt_id AND i.matter_id=t.matter_id AND i.ordinal=t.ordinal "
                "JOIN workbench_source_catalog c ON c.matter_id=u.matter_id AND c.document_id=u.document_id "
                "WHERE t.matter_id=? AND u.document_id=? AND t.source_version_id=? AND c.version_id=? "
                "AND u.state='queued' AND c.origin='upload' AND i.relative_path=u.relative_path "
                "AND c.relative_path=u.relative_path ORDER BY t.receipt_id,i.relative_path LIMIT 2",
                (matter_id, document_id, version_id, version_id)))
        if len(rows) != 1:
            return ""
        path = PurePosixPath(rows[0]["relative_path"])
        if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
            return ""
        root = path.parts[0] if len(path.parts) > 1 else ""
        return encoded([rows[0]["receipt_id"], root])

    def dat_profile(self, matter_id, source):
        """Require the mapped receipt entry to be the only proof for this ready upload."""
        from .matching import DatProfileError
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT t.receipt_id,i.relative_path,p.digest,p.fields_json FROM workbench_upload_item u "
                "JOIN workbench_source_catalog c ON c.matter_id=u.matter_id AND c.document_id=u.document_id "
                "JOIN workbench_intake_transfer t ON t.matter_id=u.matter_id AND t.upload_item_id=u.upload_item_id "
                "JOIN workbench_intake_item i ON i.receipt_id=t.receipt_id AND i.matter_id=t.matter_id AND i.ordinal=t.ordinal "
                "JOIN workbench_intake_receipt r ON r.receipt_id=i.receipt_id AND r.matter_id=i.matter_id "
                "LEFT JOIN workbench_intake_dat_profile p ON p.receipt_id=i.receipt_id AND p.matter_id=i.matter_id AND p.ordinal=i.ordinal "
                "WHERE c.matter_id=? AND c.document_id=? AND t.source_version_id=? AND c.version_id=t.source_version_id "
                "AND c.source_state='ready' AND c.origin='upload' AND u.state='queued' AND r.state='ready' "
                "AND c.relative_path=u.relative_path AND i.relative_path=u.relative_path "
                "ORDER BY (p.digest IS NULL),t.receipt_id,t.ordinal LIMIT 2",
                (matter_id, source.document_id, source.version_id)))
        if not rows or rows[0]["fields_json"] is None:
            return ()
        row = rows[0]
        if (len(rows) != 1 or not source.root_key or json.loads(source.root_key)[0] != row["receipt_id"]
                or row["relative_path"] != source.relative_path):
            raise DatProfileError("The confirmed DAT mapping needs an unambiguous production receipt.")
        if row["digest"] != source.digest:
            raise DatProfileError("The original digest differs from the confirmed DAT mapping.")
        return json.loads(row["fields_json"])

    def set_dat_profile_positions(self, matter_id, attempt_id, positions):
        self.execute("UPDATE workbench_discovery_production SET profile_positions_json=? "
                     "WHERE matter_id=? AND attempt_id=? AND status='building' AND phase='parse'",
                     (encoded(positions), matter_id, attempt_id))

    def candidates(self, matter_id, root_key, *, after="", limit=100):
        """Seek a bounded proven-root candidate window, retaining ambiguity flags."""
        bounded_limit(limit)
        try:
            receipt, root = json.loads(root_key)
            if not isinstance(receipt, str) or not isinstance(root, str):
                raise ValueError
        except (ValueError, TypeError):
            return ()
        prefix = root + "/" if root else ""
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT DISTINCT c.* FROM workbench_source_catalog c "
                "JOIN workbench_upload_item u ON u.matter_id=c.matter_id AND u.document_id=c.document_id "
                "JOIN workbench_intake_transfer t ON t.matter_id=u.matter_id AND t.upload_item_id=u.upload_item_id "
                "JOIN workbench_intake_item i ON i.matter_id=t.matter_id AND i.receipt_id=t.receipt_id AND i.ordinal=t.ordinal "
                "WHERE c.matter_id=? AND c.document_id>? AND t.receipt_id=? AND c.origin='upload' "
                "AND t.source_version_id=c.version_id AND u.state='queued' AND i.relative_path=u.relative_path "
                "AND c.relative_path=u.relative_path AND substr(c.relative_path,1,?)=? "
                "AND (?<>'' OR c.relative_path NOT LIKE '%/%') "
                "AND NOT EXISTS (SELECT 1 FROM workbench_intake_transfer other "
                "JOIN workbench_upload_item item ON item.matter_id=other.matter_id AND item.upload_item_id=other.upload_item_id "
                "WHERE other.matter_id=c.matter_id AND item.document_id=c.document_id "
                "AND other.source_version_id=c.version_id AND other.receipt_id<>t.receipt_id) "
                "ORDER BY c.document_id LIMIT ?", (matter_id, after, receipt, len(prefix), prefix, root, limit)))
            for row in rows:
                row["root_key"] = root_key
        return rows

    def catalog_key_candidates(self, matter_id, root_key, *, kind, value, compatible_path="",
                               excluded_document_id="", excluded_stem_id=""):
        """Return at most two qualifying identities: sufficient to prove ambiguity."""
        try:
            receipt, root = json.loads(root_key)
            if not isinstance(receipt, str) or not isinstance(root, str):
                raise ValueError
        except (ValueError, TypeError):
            return ()
        expressions = {"name": NAME_EXPR, "stem": STEM_EXPR}
        produced = {"begin": "begin_key", "end": "end_key", "control": "control_key"}
        if kind != "path" and kind not in expressions and kind not in produced:
            raise ValueError("unsupported candidate key")
        prefix = root + "/" if root else ""
        tables = "workbench_source_catalog c "
        where, arguments = [], []
        if kind in produced:
            tables = ("workbench_discovery_dat_record d CROSS JOIN workbench_discovery_production p "
                      "CROSS JOIN workbench_source_catalog c ")
            where.extend(("d.matter_id=?", "d." + produced[kind] + "=?", "p.matter_id=d.matter_id",
                          "p.attempt_id=d.attempt_id", "p.root_key=?", "p.status='complete'",
                          "d.association_status='matched'", "d.generation=p.generation",
                          "c.document_id=d.document_id", "c.matter_id=d.matter_id"))
            arguments.extend((matter_id, value, root_key))
        elif kind == "path":
            rooted = prefix.casefold() + value
            # Modern display names contain the upload path. Some historical
            # catalogs store the basename. Both preserve the folded relative
            # path in the search key; require that full path before LIMIT.
            where.extend(("c.display_name_key IN (?,?,?)",
                          "c.search_key IN (c.display_name_key||char(10)||?,c.display_name_key||char(10)||?)"))
            arguments.extend((value, rooted, value.rsplit("/", 1)[-1], value, rooted))
        else:
            where.append(expressions[kind] + "=?")
            arguments.append(value)
        tables += "CROSS JOIN workbench_upload_item u CROSS JOIN workbench_intake_transfer t CROSS JOIN workbench_intake_item i "
        where.extend(("c.matter_id=?", "u.matter_id=c.matter_id", "u.document_id=c.document_id",
                      "t.matter_id=u.matter_id", "t.upload_item_id=u.upload_item_id",
                      "i.matter_id=t.matter_id", "i.receipt_id=t.receipt_id", "i.ordinal=t.ordinal",
                      "t.receipt_id=?", "c.origin='upload'", "c.source_state='ready'", "c.document_id<>?",
                      "t.source_version_id=c.version_id", "u.state='queued'",
                      "i.relative_path=u.relative_path", "c.relative_path=u.relative_path",
                      "substr(c.relative_path,1,?)=?", "(?<>'' OR c.relative_path NOT LIKE '%/%')"))
        arguments.extend((matter_id, receipt, excluded_document_id, len(prefix), prefix, root))
        if kind == "stem" and excluded_stem_id:
            where.append("c.document_id<>?")
            arguments.append(excluded_stem_id)
        where.append("NOT EXISTS (SELECT 1 FROM workbench_upload_item item "
                     "CROSS JOIN workbench_intake_transfer other WHERE item.matter_id=c.matter_id "
                     "AND item.document_id=c.document_id AND other.matter_id=item.matter_id "
                     "AND other.upload_item_id=item.upload_item_id AND other.source_version_id=c.version_id "
                     "AND other.receipt_id<>t.receipt_id)")
        if compatible_path:
            suffix = "/" + compatible_path
            where.append("(c.display_name_key=? OR substr(c.display_name_key,-?)=? "
                         "OR substr(?,-length(c.display_name_key)-1)='/'||c.display_name_key)")
            arguments.extend((compatible_path, len(suffix), suffix, compatible_path))
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT DISTINCT c.* FROM " + tables + " WHERE " + " AND ".join(where)
                + " ORDER BY c.document_id LIMIT 2", tuple(arguments)))
        return rows

    def exact_path_candidates(self, matter_id, root_key, value, *, excluded_document_id=""):
        """Require the full supplied path relative to the proven production root."""
        key = path_key(value)
        if not key or not root_key:
            return ()
        return self.catalog_key_candidates(matter_id, root_key, kind="path", value=key,
            excluded_document_id=excluded_document_id)

    def dat_text_companion(self, matter_id, attempt, record):
        """Resolve one explicitly supplied DAT text path within its source receipt."""
        if attempt["source_format"] != "dat" or record["kind"] != "accepted":
            return "", "absent", None
        paths = dat_text_paths(record["headers"] or (), record["cells"] or (), record["profile_positions"])
        if len(paths) > 1:
            return "", "ambiguous", None
        if not paths or not paths[0].strip():
            return "", "absent", None
        value = paths[0]
        if not path_key(value):
            return value, "unsafe_path", None
        candidates = self.exact_path_candidates(matter_id, attempt["root_key"], value,
            excluded_document_id=attempt["document_id"])
        if len(candidates) > 1:
            return value, "ambiguous", None
        if not candidates or candidates[0]["media_type"] != "text/plain":
            return value, "unavailable", None
        return value, "matched", candidates[0]

    def candidate_lookup(self, matter_id, root_key, *, paths=(), labels=(), after="", limit=100,
                         excluded_document_id="", excluded_stem_id=""):
        """Combine bounded per-key uniqueness proofs, retaining independent keys.

        Matching only needs zero, one, or two qualifying identities for each key.
        These are not reviewer counts: every preserved raw record remains pageable.
        The caller bounds keys through its configured unambiguous semantic aliases.
        """
        bounded_limit(limit)
        try:
            receipt, root = json.loads(root_key)
            if not isinstance(receipt, str) or not isinstance(root, str):
                raise ValueError
        except (ValueError, TypeError):
            return ()
        selected = {}

        def retain(rows):
            for row in rows:
                identity = row["document_id"]
                if identity > after:
                    selected[identity] = row
                    if len(selected) > limit:
                        del selected[max(selected)]

        for path in paths:
            if not isinstance(path, str):
                raise ValueError("candidate paths must be text")
            key = path_key(path)
            if not key:
                continue
            exact = self.exact_path_candidates(matter_id, root_key, path, excluded_document_id=excluded_document_id)
            if exact:
                retain(exact)
            else:
                retain(self.catalog_key_candidates(matter_id, root_key, kind="name",
                    value=key.rsplit("/", 1)[-1], compatible_path=key, excluded_document_id=excluded_document_id))
        supplied = labels.items() if isinstance(labels, Mapping) else ((None, label) for label in labels)
        names = {"begin_bates": ("begin",), "end_bates": ("end",), "control_number": ("control",)}
        for field, label in supplied:
            if not isinstance(label, str):
                raise ValueError("candidate labels must be text")
            key = label.strip().casefold()
            if not key:
                continue
            found = False
            for kind in names.get(field, ("begin", "end", "control")):
                rows = self.catalog_key_candidates(matter_id, root_key, kind=kind, value=key, excluded_document_id=excluded_document_id)
                if rows:
                    retain(rows)
                    found = True
            if not found:
                retain(self.catalog_key_candidates(matter_id, root_key, kind="stem", value=key,
                    excluded_document_id=excluded_document_id, excluded_stem_id=excluded_stem_id))
        with self.db.lock:
            for row in selected.values():
                row["root_key"] = root_key
                produced_rows = tuple(self.execute(
                    "SELECT DISTINCT r.begin_key,r.end_key,r.control_key FROM workbench_discovery_dat_record r "
                    "JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id "
                    "WHERE r.matter_id=? AND r.document_id=? AND p.status='complete' "
                    "AND r.association_status='matched' AND r.generation=p.generation LIMIT 2", (matter_id, row["document_id"])))
                row["produced_values"] = (dict(zip(("begin_bates", "end_bates", "control_number"), produced_rows[0]))
                    if len(produced_rows) == 1 else {})
        return tuple(selected[key] for key in sorted(selected))

    def refresh_counts(self, matter_id, attempt_id):
        with self.transaction():
            counts = self.coverage(matter_id, attempt_id)
            attempt = self.require_building(matter_id, attempt_id)
            self.update_attempt(matter_id, attempt_id, status=attempt["status"], phase=attempt["phase"], counts=counts)
        return counts

    def restart_matching(self, matter_id, attempt_id, target, *, input_revision=None):
        """Revalidate derived associations without reparsing unchanged preserved bytes."""
        if not isinstance(target, str) or not target:
            raise ValueError("production target must be nonempty text")
        with self.transaction():
            attempt = self.attempt(matter_id, attempt_id)
            if attempt is None or attempt["status"] in {"stale", "failed"} or attempt["file_failure"]:
                raise ProductionStale("This attempt cannot resume association work.")
            self.execute(
                "UPDATE workbench_discovery_production SET status='building',phase='match',target=?,generation=?,input_revision=?,"
                "cursor_json='{}',matched_count=0,unmatched_count=0,conflicted_count=0,"
                "invalidation_serial=invalidation_serial+1,updated_at=? WHERE matter_id=? AND attempt_id=?",
                (target, uuid.uuid4().hex, attempt["input_revision"] if input_revision is None else str(input_revision),
                 now(), matter_id, attempt_id))
        return self.attempt(matter_id, attempt_id)

    def team_conflicts(self, matter_id, labels) -> bool:
        with self.db.lock:
            for label in labels:
                if not isinstance(label, str):
                    raise ValueError("identifier labels must be text")
                key = label.strip().casefold()
                if key and self.execute(
                    "SELECT 1 FROM workbench_discovery_identifier WHERE matter_id=? AND lower(identifier)=? LIMIT 1",
                    (matter_id, key)).fetchone() is not None:
                    return True
        return False

    def document_conflicts(self, matter_id, document_id) -> bool:
        with self.db.lock:
            return self.execute(
                "SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p "
                "ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? "
                "AND p.status IN ('building','complete') AND r.generation=p.generation "
                "AND r.association_status NOT IN ('matched','unmatched','pending') LIMIT 1",
                (matter_id, document_id)).fetchone() is not None

    def document_status(self, matter_id, document_id) -> str:
        source = self.catalog(matter_id, document_id)
        root = self.provenance(matter_id, document_id, source["version_id"]) if source is not None else ""
        with self.db.lock:
            if root and self.execute(
                "SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND root_key=? AND status='building' LIMIT 1",
                (matter_id, root)).fetchone() is not None:
                return "pending"
            if self.execute(
                "SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p "
                "ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=? "
                "AND p.status='building' LIMIT 1", (matter_id, document_id)).fetchone() is not None:
                return "pending"
            if root and self.execute(
                "SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND root_key=? "
                "AND source_format IN ('csv','tsv') AND status='failed' LIMIT 1", (matter_id, root)).fetchone() is not None:
                return "conflicted"
            return "conflicted" if self.document_conflicts(matter_id, document_id) else "complete"

    def root_work(self, matter_id, root_key, *, formats=None, phases=None, excluding="") -> bool:
        where, parameters = ["matter_id=?", "root_key=?", "status='building'", "attempt_id<>?"], [matter_id, root_key, excluding]
        for name, values in (("source_format", formats), ("phase", phases)):
            if values is not None:
                values = string_array(list(values))
                if not values:
                    return False
                where.append(name + " IN (" + ",".join("?" for _ in values) + ")")
                parameters.extend(values)
        with self.db.lock:
            return self.execute("SELECT 1 FROM workbench_discovery_production WHERE "
                + " AND ".join(where) + " LIMIT 1", tuple(parameters)).fetchone() is not None

    def document_problem(self, matter_id, document_id) -> str:
        source = self.catalog(matter_id, document_id)
        root = self.provenance(matter_id, document_id, source["version_id"]) if source is not None else ""
        with self.db.lock:
            if root and self.execute(
                "SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND root_key=? "
                "AND source_format IN ('csv','tsv') AND status='failed' LIMIT 1", (matter_id, root)).fetchone() is not None:
                return "conflicting_load_file_metadata"
            rows = self.execute(
                "SELECT DISTINCT r.association_status FROM workbench_discovery_dat_record r "
                "JOIN workbench_discovery_production p ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id "
                "WHERE r.matter_id=? AND r.document_id=? AND p.status IN ('building','complete') "
                "AND r.generation=p.generation AND r.association_status NOT IN ('matched','unmatched','pending') "
                "ORDER BY CASE r.association_status WHEN 'conflicting_load_file_metadata' THEN 0 "
                "WHEN 'team_identifier_conflict' THEN 1 WHEN 'conflicting_match' THEN 2 "
                "WHEN 'conflicting_dat_rows' THEN 3 ELSE 4 END,r.association_status LIMIT 1",
                (matter_id, document_id)).fetchone()
            return rows[0] if rows is not None else ""

    def identifier_stamp(self, matter_id):
        with self.db.lock:
            row = self.execute('SELECT COUNT(*),MAX(created_at) FROM workbench_discovery_identifier WHERE matter_id=?',
                               (matter_id,)).fetchone()
        return hashlib.sha256(encoded(list(row)).encode()).hexdigest()

    def stamp(self, matter_id):
        with self.db.lock:
            row = self.execute('SELECT COUNT(*),MAX(updated_at),SUM(CASE WHEN status=\'complete\' THEN 1 ELSE 0 END) '
                               'FROM workbench_discovery_production WHERE matter_id=?', (matter_id,)).fetchone()
        return hashlib.sha256(encoded(list(row)).encode()).hexdigest()

    def save_dependencies(self, matter_id, attempt_id, dependencies, revision):
        with self.transaction():
            self.execute('UPDATE workbench_discovery_production SET input_revision=?,updated_at=? '
                         'WHERE matter_id=? AND attempt_id=?',
                         (encoded({'revision': revision, 'dependencies': dependencies}), now(), matter_id, attempt_id))

    def pending_work(self, matter_id, binding):
        revision = f"{binding['source_identity']}:{binding['revision']}:{binding['human']}"
        prefix = '{"revision":' + encoded(revision) + ','
        target = binding['target'] or ''
        with self.db.lock:
            return self.execute("SELECT 1 FROM workbench_discovery_production WHERE matter_id=? AND status<>'stale' "
                "AND (status='building' OR (input_revision<>? AND substr(input_revision,1,?)<>?) "
                "OR (?<>'' AND target<>?)) LIMIT 1",
                (matter_id, revision, len(prefix), prefix, target, target)).fetchone() is not None

    def status_counts(self, matter_id):
        with self.db.lock:
            rows = self.execute(
                "SELECT status,COUNT(*) FROM workbench_discovery_production WHERE matter_id=? "
                "AND status<>'stale' GROUP BY status", (matter_id,))
            return {status: count for status, count in rows}

    def associated_page(self, matter_id, document_id):
        if self.document_status(matter_id, document_id) != "complete":
            return None
        with self.db.lock:
            rows = result_rows(self.execute(self.joined_select("page")
                + "WHERE r.matter_id=? AND r.document_id=? AND r.association_status='matched' "
                "AND p.status='complete' AND r.generation=p.generation ORDER BY r.attempt_id,r.ordinal LIMIT 1",
                (matter_id, document_id)))
        return decode_record(rows[0], "page") if rows else None

    def attempt_counts(self, matter_id):
        with self.db.lock:
            rows = result_rows(self.execute(
                "SELECT source_format,COUNT(*) AS sources,"
                + ",".join("SUM(" + key + "_count) AS " + key for key in COUNT_NAMES)
                + ",SUM(CASE WHEN status='building' THEN 1 ELSE 0 END) AS pending,"
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
                "FROM workbench_discovery_production WHERE matter_id=? AND status<>'stale' GROUP BY source_format",
                (matter_id,)))
        return {row["source_format"]: row for row in rows}

    def identifier_conflicts(self, matter_id, labels, *, connection=None) -> bool:
        """Compare supplied literal identifiers inside the assignment transaction."""
        if connection is not None and connection is not self.db.connection:
            raise ValueError("identifier checks must borrow the owning workspace connection")
        with self.db.lock:
            for label in labels:
                if not isinstance(label, str):
                    raise ValueError("identifier labels must be text")
                key = label.strip().casefold()
                if not key:
                    continue
                for table, names in ((TABLES["document"], ("begin_key", "end_key", "control_key")),
                                     (TABLES["page"], ("page_label_key",))):
                    found = self.execute(
                        "SELECT 1 FROM " + table + " r JOIN workbench_discovery_production p "
                        "ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? "
                        "AND p.status<>'stale' AND (" + " OR ".join("r." + name + "=?" for name in names) + ") LIMIT 1",
                        (matter_id, *(key for _ in names))).fetchone()
                    if found is not None:
                        return True
        return False

    def document_classifications(self, matter_id, document_ids, *, read):
        if not 1 <= len(document_ids) <= 12:
            raise ValueError('publication classification accepts at most 12 documents')
        marks = ','.join('?' for _ in document_ids)
        return {row[0]: row[1:] for row in read(
            'SELECT document_id,version_id,display_name FROM workbench_source_catalog '
            f'WHERE matter_id=? AND document_id IN ({marks})',
            (matter_id, *document_ids), maximum=len(document_ids))}

    def projection_rows(self, statement, parameters, *, maximum, constituents=1):
        """Read one bounded preparation primitive, reserving room to yield safely."""
        if not 1 <= maximum * constituents <= 24:
            raise ValueError('production projection reads require at most 24 touched rows')
        budget = self.budget()
        if budget is not None and budget.remaining < maximum * constituents + 32:
            raise ProjectionWindowFull('production preparation needs a smaller window')
        with self.db.lock, self.counted(None):
            rows = result_rows(self.execute(statement, parameters))
        if len(rows) > maximum:
            raise ProductionUnavailable('production projection read exceeded its bound')
        if budget is not None:
            budget.charge(len(rows) * constituents, credit=not rows)
        return rows

    def projection_reader(self, matter_id, pairs, preferred_formats):
        """Capture the bounded read model used by document_projection, without writes."""
        if len(pairs) > 12:
            raise ValueError('production projections accept at most 12 document pairs')
        view = ProjectionReader(matter_id, preferred_formats)
        ids = tuple(dict.fromkeys(document for document, version in pairs))
        if not ids:
            return view
        marks = ','.join('?' for _ in ids)
        priority = 'CASE p.source_format ' + ' '.join('WHEN ? THEN ' + str(i) for i, _ in enumerate(preferred_formats))
        priority += ' ELSE ' + str(len(preferred_formats)) + ' END,'
        for category, order, predicate in (
                ('document', priority + 'r.attempt_id,r.ordinal', ''),
                ('page', 'r.attempt_id,r.ordinal', " AND r.association_status='matched'")):
            select = self.joined_select(category).replace('SELECT r.*,',
                'SELECT ROW_NUMBER() OVER (PARTITION BY r.document_id ORDER BY ' + order + ') AS projection_rank,r.*,', 1)
            arguments = (*preferred_formats, matter_id, *ids) if category == 'document' else (matter_id, *ids)
            rows = self.projection_rows('SELECT * FROM (' + select
                + f"WHERE r.matter_id=? AND r.document_id IN ({marks}) AND p.status='complete' "
                + 'AND r.generation=p.generation' + predicate + ') ranked WHERE projection_rank=1',
                arguments, maximum=len(ids), constituents=2)
            selected = {row['document_id']: decode_record(row, category) for row in rows}
            if category == 'document':
                view.records = selected
            else:
                view.pages = selected
        attempts = sorted({row['attempt_id'] for row in (*view.records.values(), *view.pages.values())})
        attempt_clause = (' OR attempt_id IN (' + ','.join('?' for _ in attempts) + ')') if attempts else ''
        after = ''
        while True:
            rows = self.projection_rows('SELECT * FROM workbench_discovery_production '
                f"WHERE matter_id=? AND ((document_id IN ({marks}) AND status<>'stale')"
                + attempt_clause + ') AND attempt_id>? ORDER BY attempt_id LIMIT 24',
                (matter_id, *ids, *attempts, after), maximum=24)
            for row in rows:
                attempt = decode_attempt(row)
                view.attempts[row['attempt_id']] = attempt
                if row['document_id'] in ids and row['status'] != 'stale':
                    view.own[row['document_id']] = attempt
            if len(rows) < 24:
                break
            after = rows[-1]['attempt_id']
        catalog_ids = sorted(set(ids) | {row['document_id'] for row in view.attempts.values()}
            | {row['family']['root'] for row in view.records.values() if row['family']['root']})
        for start in range(0, len(catalog_ids), 24):
            part = catalog_ids[start:start + 24]
            slots = ','.join('?' for _ in part)
            view.catalogs.update(dict.fromkeys(part))
            rows = self.projection_rows('SELECT * FROM workbench_source_catalog '
                f'WHERE matter_id=? AND document_id IN ({slots})', (matter_id, *part), maximum=len(part))
            view.catalogs.update((row['document_id'], row) for row in rows)
        for document, version in pairs:
            if view.catalogs[document] is None or view.catalogs[document]['version_id'] != version:
                raise KeyError(document)
        proven = tuple(dict.fromkeys((*ids, *(row['document_id'] for row in view.attempts.values()))))
        for start in range(0, len(proven), 3):
            part = proven[start:start + 3]
            slots = ','.join('?' for _ in part)
            rows = self.projection_rows(
                'WITH proofs AS (SELECT DISTINCT u.document_id,c.version_id,t.receipt_id,i.relative_path '
                'FROM workbench_intake_transfer t '
                'JOIN workbench_upload_item u ON u.upload_item_id=t.upload_item_id AND u.matter_id=t.matter_id '
                'JOIN workbench_intake_item i ON i.receipt_id=t.receipt_id AND i.matter_id=t.matter_id AND i.ordinal=t.ordinal '
                'JOIN workbench_source_catalog c ON c.matter_id=u.matter_id AND c.document_id=u.document_id '
                f'WHERE t.matter_id=? AND u.document_id IN ({slots}) AND t.source_version_id=c.version_id '
                "AND u.state='queued' AND c.origin='upload' AND i.relative_path=u.relative_path "
                'AND c.relative_path=u.relative_path), ranked AS (SELECT *,ROW_NUMBER() OVER '
                '(PARTITION BY document_id ORDER BY receipt_id,relative_path) AS proof_rank FROM proofs) '
                'SELECT * FROM ranked WHERE proof_rank<=2 ORDER BY document_id,proof_rank',
                (matter_id, *part), maximum=2 * len(part), constituents=4)
            for document in part:
                proofs = [row for row in rows if row['document_id'] == document]
                root = ''
                if len(proofs) == 1:
                    path = PurePosixPath(proofs[0]['relative_path'])
                    if not path.is_absolute() and path.parts and not any(p in {'.', '..'} for p in path.parts):
                        root = encoded([proofs[0]['receipt_id'], path.parts[0] if len(path.parts) > 1 else ''])
                current = view.catalogs[document]
                if current is not None:
                    view.proofs[(document, current['version_id'])] = root
        requested = ','.join('(?,?)' for _ in ids)
        args = tuple(value for document in ids for value in
            (document, view.proofs.get((document, view.catalogs[document]['version_id']), '')))
        rows = self.projection_rows(
            'WITH requested(document_id,root_key) AS (VALUES ' + requested + ') '
            'SELECT q.document_id,'
            "EXISTS(SELECT 1 FROM workbench_discovery_production p WHERE p.matter_id=? AND p.root_key=q.root_key "
            "AND q.root_key<>'' AND p.status='building') AS root_pending,"
            'EXISTS(SELECT 1 FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p '
            "ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=q.document_id "
            "AND p.status='building') AS document_pending,"
            'EXISTS(SELECT 1 FROM workbench_discovery_production p WHERE p.matter_id=? AND p.root_key=q.root_key '
            "AND q.root_key<>'' AND p.source_format IN ('csv','tsv') AND p.status='failed') AS root_failed,"
            '(SELECT r.association_status FROM workbench_discovery_dat_record r JOIN workbench_discovery_production p '
            'ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id WHERE r.matter_id=? AND r.document_id=q.document_id '
            "AND p.status IN ('building','complete') AND r.generation=p.generation "
            "AND r.association_status NOT IN ('matched','unmatched','pending') "
            "ORDER BY CASE r.association_status WHEN 'conflicting_load_file_metadata' THEN 0 "
            "WHEN 'team_identifier_conflict' THEN 1 WHEN 'conflicting_match' THEN 2 "
            "WHEN 'conflicting_dat_rows' THEN 3 ELSE 4 END,r.association_status LIMIT 1) AS problem FROM requested q",
            (*args, matter_id, matter_id, matter_id, matter_id), maximum=len(ids))
        for row in rows:
            problem = 'conflicting_load_file_metadata' if row['root_failed'] else row['problem'] or ''
            status = ('pending' if row['root_pending'] or row['document_pending']
                      else 'conflicted' if problem else 'complete')
            view.statuses[row['document_id']] = status
            view.problems[row['document_id']] = problem
        if view.attempts:
            view.identifier = self.identifier_stamp(matter_id)
        return view

    def document_bates_ranges(self, matter_id: str, document_ids=None) -> dict[str, str]:
        """Return exact produced Bates range per document when uniquely matched and fresh."""
        ids = None if document_ids is None else tuple(dict.fromkeys(str(d) for d in document_ids if d))
        if ids is not None and not ids:
            return {}
        found: dict[str, set[str]] = {}
        chunks = [None] if ids is None else [ids[i:i + 200] for i in range(0, len(ids), 200)]
        with self.db.lock:
            for chunk in chunks:
                base_sql = (
                    "SELECT r.document_id,r.semantic_json,p.input_revision "
                    "FROM workbench_discovery_dat_record r "
                    "JOIN workbench_discovery_production p "
                    "ON p.matter_id=r.matter_id AND p.attempt_id=r.attempt_id "
                    "JOIN workbench_source_catalog c "
                    "ON c.matter_id=p.matter_id AND c.document_id=p.document_id "
                    "AND c.version_id=p.version_id AND c.source_state='ready' "
                    "WHERE r.matter_id=? AND p.status='complete' "
                    "AND r.association_status='matched' AND r.generation=p.generation "
                )
                if chunk is None:
                    rows = tuple(self.execute(base_sql + "AND r.document_id<>''", (matter_id,)))
                else:
                    marks = ",".join("?" for _ in chunk)
                    rows = tuple(self.execute(base_sql + f"AND r.document_id IN ({marks})", (matter_id, *chunk)))
                for doc_id, semantic_json, input_rev in rows:
                    try:
                        parsed_rev = json.loads(input_rev) if input_rev and input_rev.startswith("{") else {}
                    except (TypeError, ValueError):
                        parsed_rev = {}
                    deps = parsed_rev.get("dependencies", {}) if isinstance(parsed_rev, Mapping) else {}
                    if isinstance(deps, Mapping) and deps.get("paused"):
                        continue
                    try:
                        raw = json.loads(semantic_json)
                    except (TypeError, ValueError):
                        continue
                    pairs = dict(raw) if isinstance(raw, list) else (raw if isinstance(raw, Mapping) else {})
                    begin = str(pairs.get("begin_bates") or "").strip()
                    end = str(pairs.get("end_bates") or "").strip()
                    if begin:
                        label = f"{begin}\u2013{end}" if end and end != begin else begin
                        found.setdefault(str(doc_id), set()).add(label)
        return {
            doc_id: next(iter(labels))
            for doc_id, labels in found.items()
            if len(labels) == 1 and self.document_status(matter_id, doc_id) == "complete"
        }


class ProjectionWindowFull(RuntimeError):
    """Yield before a speculative read would consume the publication reserve."""


class ProjectionReader:
    """Read-only, matter-scoped snapshot of a bounded document projection window."""

    def __init__(self, matter_id, preferred_formats):
        self.matter_id, self.preferred_formats = matter_id, preferred_formats
        self.catalogs, self.own, self.attempts, self.records = {}, {}, {}, {}
        self.pages, self.proofs, self.statuses, self.problems = {}, {}, {}, {}
        self.identifier = ''

    def check(self, matter_id):
        if matter_id != self.matter_id:
            raise KeyError(matter_id)

    def catalog(self, matter_id, document_id):
        self.check(matter_id)
        return self.catalogs[document_id]

    def current_attempt(self, matter_id, document_id):
        self.check(matter_id)
        return self.own.get(document_id)

    def attempt(self, matter_id, attempt_id):
        self.check(matter_id)
        return self.attempts[attempt_id]

    def document_status(self, matter_id, document_id):
        self.check(matter_id)
        return self.statuses[document_id]

    def document_problem(self, matter_id, document_id):
        self.check(matter_id)
        return self.problems[document_id]

    def document_records(self, matter_id, document_id, *, limit, preferred_formats):
        self.check(matter_id)
        if limit != 1 or preferred_formats != self.preferred_formats:
            raise ValueError('projection snapshot supports only the preferred first record')
        row = self.records.get(document_id)
        return (row,) if row else ()

    def associated_page(self, matter_id, document_id):
        self.check(matter_id)
        return self.pages.get(document_id) if self.statuses[document_id] == 'complete' else None

    def provenance(self, matter_id, document_id, version_id):
        self.check(matter_id)
        return self.proofs.get((document_id, version_id), '')

    def identifier_stamp(self, matter_id):
        self.check(matter_id)
        return self.identifier
