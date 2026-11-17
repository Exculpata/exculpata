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
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from email import policy
from email.parser import BytesHeaderParser
from functools import lru_cache
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from case_intelligence.modules.discovery_review import load_files
from case_intelligence.modules.discovery_review.fields import (
    LOAD_FILE_PREFIX, Definition, Field, normalize_header,
)
from case_intelligence.modules.discovery_review.production.repository import ProductionStale

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
    metadata_state: str = "complete"


    family: Mapping[str, Any] = field(default_factory=dict)
    production_state: str = "complete"
    production_diagnostics: tuple[str, ...] = ()

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
    production_mark: Any = None
    production_coverage: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    @property
    def total(self):
        return len(self.items)

    @property
    def complete(self):
        return True

    def order(self, definition: Definition, sort: str, descending: bool) -> tuple[int, ...]:
        """Positions in display order; blanks sort last in either direction."""
        key = (sort, descending)
        with self.lock:
            if key in self.orders:
                self.orders.move_to_end(key)
                return self.orders[key]
        parents = {item.document_id: item for item in self.items}
        groups = {}
        for position, item in enumerate(self.items):
            parent = parents.get(str(item.family.get('root', '')))
            components = production_order_components(definition, item, parent, sort)
            group = groups.setdefault(components['root'], {
                'value': components['group'], 'tie': tie_key(parent or item), 'members': []})
            group['members'].append((components['member'], position))
        filled, blank = [], []
        for root, group in groups.items():
            value = group['value']
            (blank if value is None else filled).append((value, group['tie'], root))
        filled.sort(key=lambda entry: entry[:2], reverse=descending)
        blank.sort(key=lambda entry: entry[1])
        result = tuple(position for entry in (*filled, *blank)
                       for _, position in sorted(groups[entry[2]]['members']))
        with self.lock:
            self.orders[key] = result
            while len(self.orders) > MAX_CACHED_ORDERS:
                self.orders.popitem(last=False)
        return result


def decimal_key(text: str) -> tuple:
    digits = "".join(str(unicodedata.decimal(char)) for char in text).lstrip("0") or "0"
    return len(digits), digits


def natural_key(text: str) -> tuple:
    """Order ABC2 before ABC10 while leaving the shown label untouched."""
    return tuple((0, decimal_key(part), "") if part.isdecimal() else (1, (), part.casefold())
                 for part in _DIGITS.split(text) if part)


def sort_key(chosen: Field, item: Item):
    if chosen.sort == "number":
        if chosen.key in item.numbers:
            return (0, decimal_key(str(item.numbers[chosen.key])))
        value = item.value(chosen.key).strip()
        digits = value.replace(",", "")
        return ((0, decimal_key(digits)) if digits.isdecimal() else (1, natural_key(value))) if value else None
    value = item.value(chosen.key)
    if not value.strip():
        return None
    return natural_key(value) if chosen.sort == "natural" else value.casefold()


def tie_key(item: Item) -> tuple:
    return natural_key(item.values.get("path") or item.values.get("name", "")), item.document_id


def project_production_item(definition: Definition, item: Item, projection: Mapping) -> Item:
    """Apply a completed document projection without treating status as evidence."""
    state = str(projection.get('state', 'pending'))
    diagnostics = tuple(str(value) for value in projection.get('diagnostics', ()))
    if state != 'complete':
        return replace(item, production_state=state, production_diagnostics=diagnostics)
    references = projection.get('references', ())
    if references and all(reference.get('source_format') in {'csv', 'tsv'} for reference in references):
        return replace(item, family=dict(projection.get('family') or {}),
                       production_state=state, production_diagnostics=diagnostics)
    values = dict(item.values)
    for key, value in projection.get('values', {}).items():
        chosen = definition.by_key.get(key)
        if chosen is not None and not chosen.computed and isinstance(value, str):
            values[key] = value
    produced = dict(item.load_file)
    raw_fields = tuple(projection.get('raw_fields', ()))
    labels = load_files.unique_headers(tuple(str(pair[0]) for pair in raw_fields))
    for label, pair in zip(labels, raw_fields):
        produced[label] = str(pair[1])
    return replace(item, values=values, load_file=produced,
                   family=dict(projection.get('family') or {}), production_state=state,
                   production_diagnostics=diagnostics)


def production_order_components(definition: Definition, item: Item, parent: Item | None,
                                sort: str) -> dict:
    """Separate parent group sort from member order for persisted direction keys."""
    family = item.family
    root = str(family.get('root', ''))
    if not root:
        return {'root': item.document_id, 'group': sort_key(
            definition.field(sort) or definition.by_key[definition.default_sort], item),
            'member': (0, (), item.document_id), 'context': None}
    if parent is None or parent.document_id != root:
        raise MetadataUnavailable('Family parent projection is not current.')
    chosen = definition.field(sort) or definition.by_key[definition.default_sort]
    order = family.get('order', 0)
    if not isinstance(order, (str, int)) or isinstance(order, bool):
        raise MetadataUnavailable('Family member order is not current.')
    return {'root': root, 'group': sort_key(chosen, parent),
            'member': (0 if item.document_id == root else 1, natural_key(str(order)),
                       item.document_id),
            'context': {'document_id': root, 'token': parent.token,
                        'name': parent.values.get('name', ''),
                        'parent': item.document_id == root}}


def produced_image_binding(repository, *, matter_id: str, selected_document_id: str,
                           selected_version_id: str, window: Mapping) -> Mapping | None:
    """Bind one inspected page to a unique admitted image without opening it.

    ``window`` must be the fresh public ProductionService.record_window result
    for the selected document and page. That service validates the actual source
    guard, parser, definition and publication target; these bounded repository
    reads reject a changed descriptor, foreign scope or ambiguous image. This
    result is metadata, not an authority to open a path. A rendering caller must
    obtain a fresh window and equal binding before publication and serving, and
    capture the image's genuine digest/size from the guarded source store.

    Neither OPT nor the supported LFP grammar supplies a TIFF frame mapping.
    Their page ordinal and LFP offset zero do not imply frame one: explicit_frame
    remains None. A renderer can resolve a sole inventory frame separately.
    """
    if window.get("category") != "page":
        raise ValueError("An image binding requires an inspected page record.")
    inspected_attempt, inspected = window.get("attempt"), window.get("current")
    if not isinstance(inspected_attempt, Mapping) or not isinstance(inspected, Mapping):
        raise ValueError("An image binding requires a current inspected record.")
    attempt_id, ordinal = inspected_attempt.get("attempt_id"), inspected.get("ordinal")
    if not isinstance(attempt_id, str) or type(ordinal) is not int or ordinal < 1:
        raise ValueError("The inspected image record selector is invalid.")
    attempt = repository.attempt(matter_id, attempt_id)
    record = repository.record(matter_id, attempt_id, ordinal, category="page")
    if (attempt is None or record is None
            or selected_document_id not in {attempt["document_id"], record["document_id"]}):
        raise KeyError(ordinal)
    selected = repository.catalog(matter_id, selected_document_id)
    if selected is None or selected["version_id"] != selected_version_id:
        raise KeyError(selected_document_id)
    attempt_keys = ("attempt_id", "document_id", "version_id", "digest", "source_format",
                    "root_key", "generation", "target", "input_revision", "status",
                    "parser_revision", "definition_digest")
    record_keys = ("attempt_id", "ordinal", "raw_fingerprint", "generation",
                   "document_id", "association_status", "kind", "source_id", "version_id")
    if (any(attempt.get(key) != inspected_attempt.get(key) for key in attempt_keys)
            or any(record.get(key) != inspected.get(key) for key in record_keys)):
        raise ProductionStale("The inspected production image changed. Reload to continue.")
    if (attempt["status"] != "complete" or record["kind"] != "accepted"
            or record["generation"] != attempt["generation"]
            or record["association_status"] != "matched"):
        return None
    source = repository.catalog(matter_id, attempt["document_id"])
    if (source is None or source["version_id"] != attempt["version_id"]
            or source["source_state"] != "ready" or source["origin"] != "upload"
            or selected["source_state"] != "ready" or selected["origin"] != "upload"):
        raise ProductionStale("The inspected production source is no longer current.")
    root = attempt["root_key"]
    if (not root or repository.provenance(matter_id, attempt["document_id"], attempt["version_id"]) != root
            or repository.provenance(matter_id, selected_document_id, selected_version_id) != root):
        return None
    page = load_files.record_semantics(record)
    if not page["valid"]:
        return None
    candidates = repository.candidate_lookup(matter_id, root, paths=(page["path"],),
        excluded_document_id=attempt["document_id"], limit=2)
    if len(candidates) != 1:
        return None
    image = candidates[0]
    if image["media_type"] not in {"image/tiff", "image/jpeg", "image/png"}:
        return None
    return MappingProxyType({"matter_id": matter_id, "selected_document_id": selected_document_id,
        "selected_version_id": selected_version_id, "attempt_id": attempt_id,
        "generation": attempt["generation"], "source_id": attempt["document_id"],
        "source_version_id": attempt["version_id"], "source_digest": attempt["digest"],
        "target": attempt["target"], "input_revision": attempt["input_revision"],
        "root_key": root, "ordinal": ordinal, "raw_fingerprint": record["raw_fingerprint"],
        "association_status": record["association_status"], "explicit_frame": None,
        "image": MappingProxyType({"document_id": image["document_id"], "version_id": image["version_id"],
                                   "media_type": image["media_type"]})})


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
            raw = os.read(descriptor, MAX_EMAIL_HEADER_BYTES + 1)
            if len(raw) > MAX_EMAIL_HEADER_BYTES and not re.search(br"\r?\n\r?\n", raw[:MAX_EMAIL_HEADER_BYTES]):
                raise MetadataUnavailable("Email headers exceed the complete-read limit; no partial header values were accepted.")
        finally:
            os.close(descriptor)
        message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    except OSError:
        return {}
    found = {}
    for name in names:
        try:
            value = str(message.get(name, ""))
        except Exception:  # a malformed header is an absent value, never a failed page
            value = ""
        if value:
            found[name] = value
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
        if suffix not in load_files.DELIMITERS or d.origin != "upload":
            continue
        key = ("table", d.document_id, d.version_id, d.state)
        if d.state != "ready":
            parsed = load_files.rejected(d.document_id, d.display_name, "source is not ready for review")
        elif key in earlier:
            parsed = earlier[key]
        elif d.size > load_files.MAX_LOAD_FILE_BYTES:
            parsed = load_files.rejected(d.document_id, d.display_name, "file exceeds the byte limit")
        else:
            try:
                with store.source_path(d.document_id, verify_digest=True).open("rb") as source:
                    raw = source.read(load_files.MAX_LOAD_FILE_BYTES + 1)
            except (OSError, ValueError, KeyError):
                parsed = load_files.rejected(d.document_id, d.display_name, "source could not be read")
            else:
                parsed = load_files.parse(raw, document_id=d.document_id, name=d.display_name, definition=definition)
        reads[key] = parsed
        if parsed is not None:
            found.append(parsed)
    return found


def build(store, revision: int, documents, definition: Definition, previous: Index | None = None,
          assigned: Mapping[str, str] | None = None, assigned_mark: int = 0,
          production_projection=None, production_mark=None) -> Index:
    assigned = assigned or {}
    earlier = previous.reads if previous is not None and previous.definition is definition else {}
    reads: dict[tuple, Any] = {}
    tables = read_load_files(store, documents, definition, earlier, reads)
    table_ids = {table.document_id for table in tables}
    matches, reports = load_files.attach(
        tables, {d.document_id: d.relative_path for d in documents if d.document_id not in table_ids})
    wanted = {f.email_header: f.key for f in definition.fields if f.email_header}
    items, headers, filled = [], {}, set()
    for d in documents:
        stored = {}
        item = project_item(d, definition, assigned, matches.get(d.document_id))
        suffix = PurePosixPath(d.display_name).suffix.casefold()
        if (d.origin == "upload" and (d.media_type in definition.email_media_types or suffix in definition.email_suffixes)
                and any(not item.value(key) for key in wanted.values())):
            key = ("email", d.document_id, d.version_id)
            if key in earlier:
                stored = earlier[key]
            else:
                try:
                    stored = email_headers(store.source_path(d.document_id), tuple(wanted))
                except (OSError, ValueError, KeyError):
                    stored = {}
            reads[key] = stored
        item = project_item(d, definition, assigned, matches.get(d.document_id), stored)
        if production_projection is not None:
            item = project_production_item(definition, item, production_projection(d.document_id, d.version_id))
        items.append(item)
        headers.update(dict.fromkeys(item.load_file))
        filled.update(key for key, value in item.values.items() if value)
    return Index(weakref.ref(store), revision, assigned_mark, definition, tuple(items), {item.token: position for position, item in enumerate(items)},
                 tuple(headers), frozenset(filled), reports, reads, production_mark=production_mark)


_cache: OrderedDict[str, Index | None] = OrderedDict()
_cache_lock = threading.Lock()
_retired: dict[str, int] = {}


class MetadataUnavailable(RuntimeError):
    """A bounded read cannot supply complete produced metadata."""


class IndexUnavailable(RuntimeError):
    """The matter identity was permanently retired in this process."""


def retain(matter_id: str) -> None:
    """Register a live cache scope without materializing the legacy population."""
    with _cache_lock:
        if _retired.get(matter_id, 0):
            raise IndexUnavailable("Matter is no longer available")
        _cache.setdefault(matter_id, None)
        _cache.move_to_end(matter_id)
        while len(_cache) > MAX_CACHED_MATTERS:
            _cache.popitem(last=False)


def index_for(matter_id: str, store, definition: Definition, identifiers=None, *,
              production_projection=None, production_mark=None) -> Index:
    """Return the current index, rebuilding only when sources or team identifiers changed."""
    mark = identifiers.mark(matter_id) if identifiers is not None else 0
    with _cache_lock:
        generation = _retired.get(matter_id, 0)
        if generation:
            raise IndexUnavailable("Matter is no longer available")
        cached = _cache.get(matter_id)
        if (cached is not None and cached.store() is store and cached.revision == store.revision
                and cached.definition is definition and cached.assigned_mark == mark
                and cached.production_mark == production_mark):
            _cache.move_to_end(matter_id)
            return cached
    revision, documents = snapshot(store)
    assigned = identifiers.read(matter_id) if identifiers is not None else {}
    built = build(store, revision, documents, definition, cached, assigned, mark,
                  production_projection, production_mark)
    with _cache_lock:
        if _retired.get(matter_id, 0) != generation:
            raise IndexUnavailable("Matter is no longer available")
        _cache[matter_id] = built
        _cache.move_to_end(matter_id)
        while len(_cache) > MAX_CACHED_MATTERS:
            _cache.popitem(last=False)
    return built


def forget(matter_id: str) -> None:
    """Retire the opaque identity permanently, including pending builders."""
    with _cache_lock:
        _retired[matter_id] = _retired.get(matter_id, 0) + 1
        _cache.pop(matter_id, None)


@dataclass(frozen=True)
class AliasDefinition:
    digest: str
    definition: Definition = field(compare=False, hash=False)


@lru_cache(maxsize=8)
def header_aliases(key):
    return {normalize_header(header): f.key for f in key.definition.fields for header in f.headers}


def project_item(d, definition, assigned, match=None, stored=None, *, definition_digest=None):
    """Project one captured source and its already-read metadata without I/O."""
    stored = stored or {}
    suffix = PurePosixPath(d.display_name).suffix.casefold()
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
    if produced:
        aliases = (header_aliases(AliasDefinition(definition_digest, definition)) if definition_digest is not None
                   else {normalize_header(header): f.key for f in definition.fields for header in f.headers})
        for header, value in produced.items():
            key = aliases.get(normalize_header(header))
            if key and value.strip() and not values.get(key):
                values[key] = value
    is_email = d.media_type in definition.email_media_types or suffix in definition.email_suffixes
    if is_email:
        wanted = {f.email_header: f.key for f in definition.fields if f.email_header}
        for name, value in stored.items():
            values.setdefault(wanted[name], value)
    if not is_email:
        is_email = any(values.get(key) for key in definition.email_evidence_fields)
    return Item(
        token=action_token(d.document_id, d.version_id), document_id=d.document_id,
        version_id=d.version_id, kind="email" if is_email else "document", media_type=d.media_type,
        embedded=("image" if d.media_type.startswith("image/") else "pdf")
        if d.origin == "upload" and d.media_type in EMBEDDED_MEDIA_TYPES else "",
        has_text=d.has_text, values=values, numbers={"size": d.size, "pages": d.page_count} if paged else {"size": d.size}, load_file=produced)
