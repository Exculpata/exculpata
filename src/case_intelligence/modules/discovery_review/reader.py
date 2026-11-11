"""Build the review list from the matter's stored sources, read-only.

The index is derived view state held in memory. It is rebuilt when a source is
added, removed or reprocessed, and dropped when the matter is deleted. Original
files are only read; nothing here writes to disk.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesHeaderParser
from pathlib import PurePosixPath
from typing import Any, Mapping

from case_intelligence.modules.discovery_review import load_files
from case_intelligence.modules.discovery_review.fields import (
    LOAD_FILE_PREFIX, Definition, Field, normalize_header,
)

EMBEDDED_MEDIA_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})
MAX_EMAIL_HEADER_BYTES = 256 * 1024
MAX_CACHED_MATTERS = 8
MAX_CACHED_ORDERS = 6
_DIGITS = re.compile(r"(\d+)")


@dataclass(frozen=True, slots=True)
class Item:
    token: str
    document_id: str
    version_id: str
    kind: str
    media_type: str
    embedded: str
    has_text: bool
    values: Mapping[str, str]
    numbers: Mapping[str, int]
    load_file: Mapping[str, str]
    details: Mapping[str, tuple[dict, ...]] = field(default_factory=dict)

    def value(self, key: str) -> str:
        if key.startswith(LOAD_FILE_PREFIX):
            return self.load_file.get(key[len(LOAD_FILE_PREFIX):], "")
        return self.values.get(key, "")

    def has(self, key: str) -> bool:
        """Tell an empty value from a field this item's load file does not carry."""
        return not key.startswith(LOAD_FILE_PREFIX) or key[len(LOAD_FILE_PREFIX):] in self.load_file


@dataclass(frozen=True, slots=True)
class Index:
    store: Any
    revision: int
    assigned_mark: int
    definition: Definition
    items: tuple[Item, ...]
    by_token: Mapping[str, int]
    headers: tuple[str, ...]
    filled: frozenset[str]
    reports: tuple[load_files.Report, ...]
    reads: Mapping[tuple, Any] = field(default_factory=dict, compare=False)
    orders: OrderedDict = field(default_factory=OrderedDict, compare=False)
    lock: Any = field(default_factory=threading.Lock, compare=False)

    def order(self, definition: Definition, sort: str, descending: bool) -> tuple[int, ...]:
        """Positions in display order; blanks sort last in either direction."""
        key = (sort, descending)
        with self.lock:
            if key in self.orders:
                self.orders.move_to_end(key)
                return self.orders[key]
        chosen = definition.field(sort) or definition.by_key[definition.default_sort]
        filled, blank = [], []
        for position, item in enumerate(self.items):
            value = sort_key(chosen, item)
            (blank if value is None else filled).append((value, tie_key(item), position))
        filled.sort(key=lambda entry: entry[:2], reverse=descending)
        blank.sort(key=lambda entry: entry[1])
        result = tuple(entry[2] for entry in (*filled, *blank))
        with self.lock:
            self.orders[key] = result
            while len(self.orders) > MAX_CACHED_ORDERS:
                self.orders.popitem(last=False)
        return result


def natural_key(text: str) -> tuple:
    """Order ABC2 before ABC10 while leaving the shown label untouched."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
                 for part in _DIGITS.split(text) if part)


def sort_key(chosen: Field, item: Item):
    if chosen.sort == "number":
        if chosen.key in item.numbers:
            return item.numbers[chosen.key]
        digits = item.value(chosen.key).replace(",", "").strip()
        return int(digits) if digits.isdigit() else None
    value = item.value(chosen.key)
    if not value.strip():
        return None
    return natural_key(value) if chosen.sort == "natural" else value.casefold()


def tie_key(item: Item) -> tuple:
    return natural_key(item.values.get("path") or item.values.get("name", "")), item.document_id


def action_token(document_id: str, version_id: str) -> str:
    """The same address the existing source pages use for one source version."""
    return hashlib.sha256(
        ("pilot-action\x00" + document_id + "\x00" + version_id).encode("utf-8")).hexdigest()[:32]


def format_size(size: int) -> str:
    value, units = float(size), ("bytes", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def email_headers(path, names) -> dict[str, str]:
    """Read header values from the start of a stored message, as written."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            raw = os.read(descriptor, MAX_EMAIL_HEADER_BYTES)
        finally:
            os.close(descriptor)
        message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    except (OSError, ValueError):
        return {}
    found = {}
    for name in names:
        try:
            value = " ".join(str(message.get(name, "")).split())
        except Exception:  # a malformed header is an absent value, never a failed page
            value = ""
        if value:
            found[name] = value[:load_files.MAX_VALUE_CHARS]
    return found


@dataclass(frozen=True, slots=True)
class Snapshot:
    document_id: str
    version_id: str
    display_name: str
    relative_path: str
    media_type: str
    size: int
    state: str
    message: str
    digest: str
    page_count: int
    origin: str
    has_text: bool


def snapshot(store) -> tuple[int, tuple[Snapshot, ...]]:
    """Copy what the list needs, with the registry revision it was read at."""
    with store.mutation_guard():
        return store.revision, tuple(Snapshot(
            d.document_id, d.version_id, d.display_name, d.relative_path or d.display_name,
            d.media_type, int(d.size), d.state, d.message, d.digest, int(d.page_count), d.origin,
            d.state == "ready" and bool(d.units or d.units_file),
        ) for d in store.documents.values())


def read_load_files(store, documents, definition: Definition, earlier: Mapping, reads: dict):
    """Parse each stored table once per source version; later builds reuse it."""
    found = []
    for d in sorted(documents, key=lambda d: natural_key(d.relative_path)):
        suffix = PurePosixPath(d.display_name).suffix.casefold()
        if (suffix not in load_files.DELIMITERS or d.origin != "upload" or d.state != "ready"
                or d.size > load_files.MAX_LOAD_FILE_BYTES):
            continue
        key = ("table", d.document_id, d.version_id)
        if key in earlier:
            parsed = earlier[key]
        else:
            try:
                raw = store.source_path(d.document_id, verify_digest=True).read_bytes()
            except (OSError, ValueError, KeyError):
                continue
            parsed = load_files.parse(raw, document_id=d.document_id, name=d.display_name, definition=definition)
        reads[key] = parsed
        if parsed is not None:
            found.append(parsed)
    return found


def build(store, revision: int, documents, definition: Definition, previous: Index | None = None,
          assigned: Mapping[str, str] | None = None, assigned_mark: int = 0) -> Index:
    assigned = assigned or {}
    earlier = previous.reads if previous is not None and previous.definition is definition else {}
    reads: dict[tuple, Any] = {}
    tables = read_load_files(store, documents, definition, earlier, reads)
    table_ids = {table.document_id for table in tables}
    matches, reports = load_files.attach(
        tables, {d.document_id: d.relative_path for d in documents if d.document_id not in table_ids})
    aliases = {normalize_header(header): f.key for f in definition.fields for header in f.headers}
    wanted = {f.email_header: f.key for f in definition.fields if f.email_header}
    items, headers, filled = [], {}, set()
    for d in documents:
        suffix = PurePosixPath(d.display_name).suffix.casefold()
        match = matches.get(d.document_id)
        folder = PurePosixPath(d.relative_path).parent.as_posix()
        paged = d.media_type == "application/pdf" and d.page_count > 0  # other types record sections, not pages
        values = {
            "assigned_id": assigned.get(d.document_id, ""),
            "name": PurePosixPath(d.display_name).name or d.display_name,
            "type": suffix[1:].upper() or d.media_type,
            "pages": str(d.page_count) if paged else "",
            "folder": "" if folder == "." else folder,
            "size": format_size(d.size),
            "text_status": "Extracted text available" if d.has_text else "No extracted text",
            "status": "Ready" if d.state == "ready" else " · ".join(part for part in (d.state.capitalize(), d.message) if part),
            "stored_from": "Copy stored in this matter" if d.origin == "upload" else "Linked source location",
            "sha256": d.digest,
            "path": d.relative_path,
            "load_file": f"{match.name}, line {match.line} (matched by {match.basis})" if match else "",
        }
        produced = match.values if match else {}
        for header, value in produced.items():
            headers.setdefault(header, None)
            key = aliases.get(normalize_header(header))
            if key and value.strip() and not values.get(key):
                values[key] = value
        is_email = d.media_type in definition.email_media_types or suffix in definition.email_suffixes
        if is_email and d.origin == "upload" and any(not values.get(key) for key in wanted.values()):
            key = ("email", d.document_id, d.version_id)
            if key in earlier:
                stored = earlier[key]
            else:
                try:
                    stored = email_headers(store.source_path(d.document_id), tuple(wanted))
                except (OSError, ValueError, KeyError):
                    stored = {}
            reads[key] = stored
            for name, value in stored.items():
                values.setdefault(wanted[name], value)
        if not is_email:
            is_email = any(values.get(key) for key in definition.email_evidence_fields)
        filled.update(key for key, value in values.items() if value)
        items.append(Item(
            token=action_token(d.document_id, d.version_id), document_id=d.document_id,
            version_id=d.version_id, kind="email" if is_email else "document", media_type=d.media_type,
            embedded=("image" if d.media_type.startswith("image/") else "pdf")
            if d.origin == "upload" and d.media_type in EMBEDDED_MEDIA_TYPES else "",
            has_text=d.has_text, values=values, numbers={"size": d.size, "pages": d.page_count} if paged else {"size": d.size}, load_file=produced))
    return Index(weakref.ref(store), revision, assigned_mark, definition, tuple(items), {item.token: position for position, item in enumerate(items)},
                 tuple(headers), frozenset(filled), reports, reads)


_cache: OrderedDict[str, Index] = OrderedDict()
_cache_lock = threading.Lock()


def index_for(matter_id: str, store, definition: Definition, identifiers=None) -> Index:
    """Return the current index, rebuilding only when sources or team identifiers changed."""
    mark = identifiers.mark(matter_id) if identifiers is not None else 0
    with _cache_lock:
        cached = _cache.get(matter_id)
        if (cached is not None and cached.store() is store and cached.revision == store.revision
                and cached.definition is definition and cached.assigned_mark == mark):
            _cache.move_to_end(matter_id)
            return cached
    revision, documents = snapshot(store)
    assigned = identifiers.read(matter_id) if identifiers is not None else {}
    built = build(store, revision, documents, definition, cached, assigned, mark)
    with _cache_lock:
        _cache[matter_id] = built
        _cache.move_to_end(matter_id)
        while len(_cache) > MAX_CACHED_MATTERS:
            _cache.popitem(last=False)
    return built


def forget(matter_id: str) -> None:
    """Drop derived view state when its matter is deleted."""
    with _cache_lock:
        _cache.pop(matter_id, None)
