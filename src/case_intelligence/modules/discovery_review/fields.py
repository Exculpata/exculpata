"""Read the one field definition behind default columns and item fields.

The definition is data (``config/discovery_review_fields.json``). Changing what
shows by default, adding a field or teaching a field a new load-file header is
an edit to that file. View and route code never name a field.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Mapping

LOAD_FILE_PREFIX = "lf:"
LOAD_FILE_GROUP = "load_file"
COMPUTED = frozenset({
    "review_codes", "assigned_id", "name", "type", "pages", "folder", "load_file", "size", "text_status",
    "status", "stored_from", "sha256", "path",
})
_SORTS = frozenset({"text", "natural", "number"})
_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_HEADER_CHARS = 200
MAX_KEY_CHARS = MAX_HEADER_CHARS + 3 + 12


class DefinitionError(ValueError):
    """The field definition is malformed; the review page fails closed."""


def normalize_header(header: str) -> str:
    """Compare load-file headers without case, spacing or punctuation."""
    return "".join(character for character in header.casefold() if character.isalnum())


@dataclass(frozen=True, slots=True)
class Group:
    key: str
    label: str
    applies_to: str = ""


@dataclass(frozen=True, slots=True)
class Field:
    key: str
    label: str
    group: str
    applies_to: str = ""
    computed: str = ""
    email_header: str = ""
    headers: tuple[str, ...] = ()
    sort: str = "text"
    align: str = "start"

    @property
    def from_load_file(self) -> bool:
        return self.key.startswith(LOAD_FILE_PREFIX)


@dataclass(frozen=True, slots=True)
class Definition:
    groups: tuple[Group, ...]
    fields: tuple[Field, ...]
    by_key: Mapping[str, Field]
    default_columns: tuple[str, ...]
    default_item_fields: tuple[str, ...]
    default_sort: str
    page_size: int
    max_columns: int
    hide_empty_default_columns: bool
    show_load_file_fields_by_default: bool
    path_headers: frozenset[str]
    email_media_types: frozenset[str]
    email_suffixes: frozenset[str]
    email_evidence_fields: tuple[str, ...]
    heading_range: tuple[str, ...]
    heading_fallback: str
    heading_missing: str
    heading_title: str

    def field(self, key: str) -> Field | None:
        """Resolve a defined field or a load-file header kept as produced."""
        if key.startswith(LOAD_FILE_PREFIX):
            header = key[len(LOAD_FILE_PREFIX):]
            if not header or len(key) > MAX_KEY_CHARS:
                return None
            return Field(key, header, LOAD_FILE_GROUP, sort="natural")
        return self.by_key.get(key)

    def clean(self, keys, *, limit: int | None = None) -> tuple[str, ...]:
        """Keep known keys once each, in the order given."""
        kept: list[str] = []
        for key in keys:
            if isinstance(key, str) and key not in kept and self.field(key) is not None:
                kept.append(key)
        return tuple(kept[:limit] if limit is not None else kept)


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise DefinitionError(f"{name} must be a list of text values")
    return tuple(value)


def parse_definition(data: Any) -> Definition:
    """Validate the definition so a bad edit is reported, never half-applied."""
    if not isinstance(data, dict) or data.get("version") != 1:
        raise DefinitionError("field definition version must be 1")
    groups = []
    for raw in data.get("groups") or ():
        if not isinstance(raw, dict) or not _KEY.fullmatch(str(raw.get("key", ""))) or not raw.get("label"):
            raise DefinitionError("each group needs a key and a label")
        groups.append(Group(raw["key"], str(raw["label"]), str(raw.get("applies_to", ""))))
    group_keys = {group.key for group in groups}
    if len(group_keys) != len(groups) or LOAD_FILE_GROUP in group_keys:
        raise DefinitionError("group keys must be unique")
    fields, claimed = [], {}
    for raw in data.get("fields") or ():
        if not isinstance(raw, dict) or not _KEY.fullmatch(str(raw.get("key", ""))) or not raw.get("label"):
            raise DefinitionError("each field needs a key and a label")
        key = raw["key"]
        if raw.get("group") not in group_keys:
            raise DefinitionError(f"field {key} names an unknown group")
        computed = str(raw.get("computed", ""))
        headers = _strings(raw.get("load_file_headers", []), f"{key}.load_file_headers")
        if computed and (computed not in COMPUTED or headers):
            raise DefinitionError(f"field {key} has an unknown computed value")
        if not computed and not headers and not raw.get("email_header"):
            raise DefinitionError(f"field {key} has no value source")
        for header in headers:
            if claimed.setdefault(normalize_header(header), key) != key:
                raise DefinitionError(f"load-file header {header} is claimed by two fields")
        sort = str(raw.get("sort", "text"))
        if sort not in _SORTS or raw.get("align", "start") not in {"start", "end"}:
            raise DefinitionError(f"field {key} has an unknown sort or alignment")
        fields.append(Field(key, str(raw["label"]), raw["group"], str(raw.get("applies_to", "")),
                            computed, str(raw.get("email_header", "")), headers, sort,
                            str(raw.get("align", "start"))))
    by_key = {field.key: field for field in fields}
    if len(by_key) != len(fields) or not fields:
        raise DefinitionError("field keys must be unique")

    def known(name: str) -> tuple[str, ...]:
        keys = _strings(data.get(name, []), name)
        if len(set(keys)) != len(keys) or any(key not in by_key for key in keys):
            raise DefinitionError(f"{name} must list defined fields once each")
        return keys

    page_size, max_columns = data.get("page_size", 100), data.get("max_columns", 20)
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (page_size, max_columns)) \
            or not 10 <= page_size <= 500 or not 1 <= max_columns <= 60:
        raise DefinitionError("page_size or max_columns is out of range")
    heading = data.get("item_heading")
    if (not isinstance(heading, dict) or heading.get("title") not in by_key
            or heading.get("fallback", "") not in {"", *by_key}
            or not isinstance(heading.get("missing"), str)):
        raise DefinitionError("item_heading needs a title field and a missing label")
    heading_range = _strings(heading.get("range", []), "item_heading.range")
    if len(heading_range) != 2 or any(key not in by_key for key in heading_range):
        raise DefinitionError("item_heading.range must name a begin and an end field")
    columns = known("default_columns")
    default_sort = str(data.get("default_sort", ""))
    if not columns or len(columns) > max_columns or default_sort not in by_key:
        raise DefinitionError("default_columns or default_sort is invalid")
    return Definition(
        groups=tuple(groups), fields=tuple(fields), by_key=by_key, default_columns=columns,
        default_item_fields=known("default_item_fields"), default_sort=default_sort,
        page_size=page_size, max_columns=max_columns,
        hide_empty_default_columns=data.get("hide_empty_default_columns") is True,
        show_load_file_fields_by_default=data.get("show_load_file_fields_by_default") is True,
        path_headers=frozenset(normalize_header(h) for h in _strings(
            data.get("load_file_path_headers", []), "load_file_path_headers")),
        email_media_types=frozenset(_strings(data.get("email_media_types", []), "email_media_types")),
        email_suffixes=frozenset(s.casefold() for s in _strings(data.get("email_suffixes", []), "email_suffixes")),
        email_evidence_fields=known("email_evidence_fields"),
        heading_range=heading_range, heading_fallback=heading.get("fallback", ""), heading_missing=heading["missing"], heading_title=heading["title"],
    )


@lru_cache(maxsize=1)
def load_definition() -> Definition:
    text = resources.files("case_intelligence").joinpath(
        "config/discovery_review_fields.json").read_text(encoding="utf-8")
    return parse_definition(json.loads(text))
