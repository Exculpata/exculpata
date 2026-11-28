"""Conservative record comparisons with bounded candidate accumulators."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import unicodedata

from case_intelligence.source_load_files import DAT_INTAKE_ROLES, DAT_TEXT_HEADER_KEYS
from ..fields import MAX_HEADER_CHARS, normalize_header


def label_key(value: str) -> str:
    return value.strip().casefold()


def path_key(value: str) -> str:
    """Compare inert relative names without inspecting a filesystem."""
    value = unicodedata.normalize("NFC", value).replace("\\", "/")
    if (value.startswith("/") or re.match(r"^[A-Za-z]:", value)
            or any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)):
        return ""
    parts = value.split("/")
    if ".." in parts or any(":" in part for part in parts):
        return ""
    return "/".join(part for part in parts if part not in {"", "."}).casefold()


@dataclass(frozen=True)
class Projection:
    values: dict[str, str]
    raw_fields: tuple[tuple[str, str], ...]
    paths: tuple[str, ...]
    keys: dict[str, str]
    fingerprint: str
    diagnostics: tuple[str, ...]


class DatProfileError(ValueError):
    """The confirmed DAT mapping cannot be applied to this admitted original."""


def profile_positions(headers, fields):
    """Resolve exact headings once, against the original DAT header."""
    if not fields:
        return []
    mapped = dict(fields)
    if (len(mapped) != len(fields) or len(set(mapped.values())) != len(mapped)
            or not set(mapped.values()) <= DAT_INTAKE_ROLES):
        raise DatProfileError("The confirmed DAT field mapping is invalid.")
    positions = [[position, mapped[heading]] for position, heading in enumerate(headers or ())
                 if heading in mapped]
    if len(positions) != len(mapped) or len({role for _, role in positions}) != len(mapped):
        raise DatProfileError("A confirmed DAT header is missing or occurs more than once.")
    return positions


def dat_text_paths(headers, cells, mapped_positions=()):
    """An explicit companion role replaces defaults; reassigned headings lose their old role."""
    assigned = dict(mapped_positions)
    for position, role in assigned.items():
        if role == "text_path":
            return (cells[position],)
    return tuple(value for position, (header, value) in enumerate(zip(headers, cells))
                 if position not in assigned and len(header) <= MAX_HEADER_CHARS
                 and normalize_header(header) in DAT_TEXT_HEADER_KEYS)


def project_record(headers, cells, definition, mapped_positions=()) -> Projection:
    """Preserve every field; an ambiguous alias never establishes a value."""
    if headers is None or cells is None or len(headers) != len(cells):
        return Projection({}, (), (), {}, "", ("unrecoverable_record",))
    raw = tuple(zip(headers, cells))
    assigned = dict(mapped_positions)
    overrides = {"document_date" if role == "date" else role: cells[index]
                 for index, role in mapped_positions}
    positions = {}
    for index, header in enumerate(headers):
        if index not in assigned and len(header) <= MAX_HEADER_CHARS:
            positions.setdefault(normalize_header(header), []).append(index)
    values = {key: value for key, value in overrides.items() if key not in {"native_path", "text_path"}}
    diagnostics = set()
    for field in definition.fields:
        if field.computed or not field.headers or field.key in overrides:
            continue
        indexes = {index for header in field.headers
                   for index in positions.get(normalize_header(header), ())}
        if len(indexes) == 1:
            values[field.key] = cells[next(iter(indexes))]
        elif indexes:
            diagnostics.add("ambiguous_header")
    if "native_path" in overrides:
        paths = (overrides["native_path"],) if overrides["native_path"].strip() else ()
    else:
        paths = []
        for normalized, indexes in positions.items():
            if normalized not in definition.path_headers:
                continue
            if len(indexes) != 1:
                diagnostics.add("ambiguous_header")
                continue
            value = cells[indexes[0]]
            if value.strip():
                paths.append(value)
        paths = tuple(paths)
    if any(not path_key(value) for value in paths):
        diagnostics.add("unsafe_path")
    keys = {key: label_key(values.get(key, "")) for key in (
        "begin_bates", "end_bates", "control_number", "begin_attachment", "end_attachment")}
    keys["path"] = path_key(paths[0]) if len(paths) == 1 else ""
    facts = {"values": sorted(values.items()), "raw": sorted(raw)}
    if mapped_positions:
        text_paths = dat_text_paths(headers, cells, mapped_positions)
        if text_paths != dat_text_paths(headers, cells):
            facts["mapped_text_paths"] = text_paths
    fingerprint = hashlib.sha256(json.dumps(facts,
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    return Projection(values, raw, paths, keys, fingerprint, tuple(sorted(diagnostics)))


def compatible_metadata(left: Projection, right: Projection) -> bool:
    """Absent columns make no assertion; shared present fields must be exact."""
    if any(left.values[key] != right.values[key] for key in left.values.keys() & right.values.keys()):
        return False
    first, second = {}, {}
    for header, value in left.raw_fields:
        first.setdefault(header, []).append(value)
    for header, value in right.raw_fields:
        second.setdefault(header, []).append(value)
    return all(first[key] == second[key] for key in first.keys() & second.keys())


def initial_match(projection: Projection) -> dict:
    evidence = {}
    for index, value in enumerate(projection.paths):
        if path_key(value):
            evidence[f"path:{index}"] = {"exact": [], "name": []}
    for key in ("begin_bates", "end_bates", "control_number"):
        if projection.keys.get(key):
            evidence[key] = {"produced": [], "stem": []}
    return {"evidence": evidence, "after": ""}


def remember(values: list[str], document_id: str) -> None:
    """Two distinct candidates prove ambiguity; their total is not a display count."""
    if document_id not in values and len(values) < 2:
        values.append(document_id)


def observe_candidate(state: dict, projection: Projection, candidate: dict) -> None:
    document_id = candidate["document_id"]
    candidate_path = path_key(candidate.get("relative_path", candidate.get("display_name", "")))
    basename = candidate_path.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    values = candidate.get("produced_values", candidate.get("semantic", {}))
    for key, tiers in state["evidence"].items():
        if key.startswith("path:"):
            supplied = path_key(projection.paths[int(key.split(":")[1])])
            if supplied in (candidate_path, candidate_path.partition("/")[2]):
                remember(tiers["exact"], document_id)
            elif (basename == supplied.rsplit("/", 1)[-1]
                    and (candidate_path.endswith("/" + supplied) or supplied.endswith("/" + candidate_path))):
                remember(tiers["name"], document_id)
        else:
            value = projection.keys[key]
            if values.get(key) and label_key(values[key]) == value:
                remember(tiers["produced"], document_id)
            if key == "begin_bates" and stem == value and document_id != state.get("excluded_stem_id"):
                remember(tiers["stem"], document_id)
                if state.get("unresolved_text") and candidate.get("media_type") == "text/plain":
                    state["unproved_text_stem"] = True
    state["after"] = document_id


def finish_match(state: dict, projection: Projection) -> tuple[str, str, str]:
    if "unsafe_path" in projection.diagnostics:
        return "", "", "conflicting_match"
    choices, bases = set(), []
    for key, tiers in state["evidence"].items():
        selected = next((values for values in tiers.values() if values), [])
        if len(selected) > 1:
            return "", "", "ambiguous_match"
        if selected:
            if key.startswith("path:") and selected[0] == state.get("text_companion_id"):
                return "", "", "conflicting_match"
            choices.add(selected[0])
            bases.append("path" if key.startswith("path:") else key)
    if len(choices) > 1:
        return "", "", "conflicting_match"
    if state.get("unproved_text_stem") and not any(
            tiers.get("exact") or tiers.get("produced") for tiers in state["evidence"].values()):
        return "", "", "unmatched"
    if not choices or choices == {state.get("text_companion_id")}:
        return "", "", "unmatched"
    return next(iter(choices)), ", ".join(bases), "matched"


def candidate_agrees(projection: Projection, candidate: dict) -> bool:
    values = candidate.get("produced_values", candidate.get("semantic", {}))
    for key in ("begin_bates", "end_bates", "control_number"):
        if projection.keys.get(key) and values.get(key) and projection.keys[key] != label_key(values[key]):
            return False
    return True


def literal_identifiers(projection: Projection) -> tuple[str, ...]:
    return tuple(projection.values[key] for key in ("begin_bates", "end_bates", "control_number")
                 if projection.values.get(key, "").strip())


def source_input_identity(item) -> tuple[object, ...]:
    """Canonical candidate snapshot tuple shared by ProductionService and freshness checks."""
    return (item.version_id, item.digest, item.display_name, item.relative_path,
            item.media_type, item.state, item.origin)


_FRESHNESS_CACHE_LIMIT = 256
_FRESHNESS_CACHE_LOCK = threading.Lock()
_FRESHNESS_CACHE: OrderedDict[tuple[object, ...], bool] = OrderedDict()


def _remember_freshness(cache_key: tuple[object, ...], fresh: bool) -> bool:
    with _FRESHNESS_CACHE_LOCK:
        _FRESHNESS_CACHE[cache_key] = fresh
        _FRESHNESS_CACHE.move_to_end(cache_key)
        while len(_FRESHNESS_CACHE) > _FRESHNESS_CACHE_LIMIT:
            _FRESHNESS_CACHE.popitem(last=False)
    return fresh


def attempt_dependencies_fresh(
    repository, matter_id: str, *, load_document_id: str, load_version_id: str,
    root_key: str, input_revision: str, updated_at: str = "", store=None,
) -> bool:
    """Verify a persisted complete production attempt still matches live root dependencies."""
    try:
        parsed = json.loads(input_revision) if input_revision and input_revision.startswith("{") else {}
    except (TypeError, ValueError):
        return False
    deps = parsed.get("dependencies") if isinstance(parsed, dict) else None
    if not isinstance(deps, dict) or deps.get("paused") or deps.get("state") != "complete":
        return False
    if store is None:
        if repository.provenance(matter_id, load_document_id, load_version_id) != root_key:
            return False
        if updated_at:
            after = ""
            while True:
                rows = repository.candidates(matter_id, root_key, after=after, limit=100)
                if not rows:
                    break
                if any((row.get("updated_at") or "") > updated_at for row in rows):
                    return False
                after = rows[-1]["document_id"]
        return True
    expected_epoch = f"production-v2-{store.source_changes.epoch}"
    if deps.get("epoch") != expected_epoch:
        return False
    expected_hash = deps.get("hash")
    if not isinstance(expected_hash, str):
        return False
    data_version_row = repository.execute("PRAGMA data_version").fetchone()
    data_version = data_version_row[0] if data_version_row else 0
    cache_key = (
        store.source_changes.epoch,
        store.revision,
        repository.db.connection.total_changes,
        data_version,
        matter_id,
        load_document_id,
        load_version_id,
        root_key,
        input_revision,
        updated_at,
    )
    with _FRESHNESS_CACHE_LOCK:
        cached = _FRESHNESS_CACHE.get(cache_key)
        if cached is not None:
            _FRESHNESS_CACHE.move_to_end(cache_key)
            return cached
    if repository.provenance(matter_id, load_document_id, load_version_id) != root_key:
        return _remember_freshness(cache_key, False)
    digest_hash, after = "", ""
    while True:
        rows = repository.candidates(matter_id, root_key, after=after, limit=100)
        if not rows:
            break
        by_doc: dict[str, list] = {}
        for item in store.source_changes.read([row["document_id"] for row in rows]):
            by_doc.setdefault(item.document_id, []).append(item)
        for row in rows:
            doc_id = row["document_id"]
            data = [(item.document_id, source_input_identity(item)) for item in by_doc.get(doc_id, ())]
            digest_hash = hashlib.sha256((digest_hash + json.dumps(data, sort_keys=True)).encode()).hexdigest()
            after = doc_id
    return _remember_freshness(cache_key, digest_hash == expected_hash)

