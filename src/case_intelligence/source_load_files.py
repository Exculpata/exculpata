"""Read-only DAT and OPT parsing with bounded, source-cited diagnostics."""
import re
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from case_intelligence.source_image_load_files.records import (
    Finding as Finding, FindingSink as _Findings, LoadFileError, LoadFileLine, OptPage, RecordEvent,
    publish_event as _publish_event,
)
from case_intelligence.source_image_load_files.stream import (
    detect_encoding, iter_records as _records, make_cursor as _checkpoint,
)

DatRow = namedtuple("DatRow", "values headers file line")


def _encoding(path):
    """Retain the legacy local helper while the public decoder has shared ownership."""
    return detect_encoding(path)


_FIELD_ALIASES = {
    "BEGDOC": "begin_bates", "BEGBATES": "begin_bates", "ENDDOC": "end_bates",
    "ENDBATES": "end_bates", "BEGATTACH": "begin_attachment", "ENDATTACH": "end_attachment",
    "TEXT": "text_path", "TEXTPATH": "text_path", "NATIVE": "native_path",
    "NATIVEPATH": "native_path", "DATE": "date", "MD5": "md5", "SHA1": "sha1", "SHA256": "sha256",
}


@dataclass(frozen=True)
class FieldProfile:
    producer: str
    fields: Mapping[str, str]

    def __post_init__(self):
        if (not isinstance(self.producer, str) or not self.producer
                or not isinstance(self.fields, Mapping)
                or any(not isinstance(h, str) or not h or not isinstance(t, str)
                       or t not in _FIELD_ALIASES.values() for h, t in self.fields.items())
                or len(set(self.fields.values())) != len(self.fields)):
            raise ValueError("invalid field profile")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict) or set(data) != {"producer", "fields"}:
            raise ValueError("invalid field profile")
        return cls(data["producer"], data["fields"])

    def to_data(self):
        return {"producer": self.producer, "fields": dict(self.fields)}

    def __hash__(self):
        return hash((self.producer, frozenset(self.fields.items())))

def propose_field_map(headers):
    """Propose the first recognized header for each target in input order."""
    targets = {}
    for header in headers:
        if header in _FIELD_ALIASES:
            targets.setdefault(_FIELD_ALIASES[header], header)
    return {header: target for target, header in targets.items()}

def _dat_values(raw):
    values = raw.split("\x14")
    if all("þ" not in v or (v.startswith("þ") and v.endswith("þ") and v.count("þ") == 2) for v in values):
        return tuple(v[1:-1] if v.startswith("þ") else v for v in values)
    values, current, quoted = [], [], False
    for token in re.split(r"(þ|\x14)", raw):
        if token == "þ":
            quoted = not quoted
            current.append(token)
        elif token == "\x14" and not quoted:
            values.append("".join(current))
            current = []
        else:
            current.append(token)
    values.append("".join(current))
    if any("þ" in v and not (v.startswith("þ") and v.endswith("þ") and v.count("þ") == 2)
           for v in values):
        return None
    return tuple(v[1:-1] if v.startswith("þ") else v for v in values)


def iter_load_file_lines(path):
    """Yield unchanged physical-line text using the shared bounded decoder.

    Only physical terminators are removed. Thorn, commas, and other text have no
    field meaning here. Unsupported encoding, a damaged later line, changed
    source, or a line beyond the record bound raises its source-cited error;
    consumers never receive a lossy or clipped successful line.
    """
    records = _records(path)
    try:
        for line, raw, error, state in records:
            if error:
                raise LoadFileError(error, path, line)
            yield LoadFileLine(raw, str(path), line)
    finally:
        records.close()


def parse_dat(path, *, findings=None, headers_out=None, on_event=None, cursor=None):
    """Stream legacy rows and optional complete events from one bounded pass."""
    sink = _Findings(findings, path)
    headers = tuple(cursor["headers"]) if isinstance(cursor, Mapping) and isinstance(cursor.get("headers"), list) else None
    ordinal = cursor.get("ordinal", 1) if isinstance(cursor, Mapping) else 1
    if headers_out is not None and headers is not None:
        headers_out.extend(headers)
    for line, raw, error, state in _records(path, dat=True, cursor=cursor):
        file_failure = error in {"unsupported_encoding", "read_error", "source_changed"}
        values = _dat_values(raw) if raw is not None else None
        if file_failure:
            _publish_event(RecordEvent("file_failure", "document", "dat", ordinal, line,
                headers, None, (error,)), on_event, sink)
            continue
        if headers is None:
            diagnostics = ("invalid_header",) if error or values is None else tuple(
                code for code, bad in (("duplicate_header", len(set(values)) != len(values)),
                                      ("empty_header", not all(values))) if bad)
            headers = values if values is not None else ()
            if headers_out is not None:
                headers_out.extend(headers)
            event = RecordEvent("header", "document", "dat", 0, line,
                values, values, diagnostics, next_cursor=_checkpoint(state, "dat", ordinal, headers))
            _publish_event(event, on_event, sink)
            continue
        diagnostics = ((error,) if error else ("invalid_quoting",) if values is None
                       else ("field_count_mismatch",) if headers and len(values) != len(headers) else ())
        event = RecordEvent("rejected" if diagnostics else "accepted", "document", "dat", ordinal,
            line, headers, values, diagnostics, next_cursor=_checkpoint(state, "dat", ordinal + 1, headers))
        _publish_event(event, on_event, sink)
        ordinal += 1
        if not diagnostics:
            yield DatRow(values, headers, str(path), line)


def parse_opt(path, *, findings=None, on_event=None, cursor=None):
    """Stream unchanged positional rows and optional uncapped record events."""
    sink = _Findings(findings, path)
    ordinal = cursor.get("ordinal", 1) if isinstance(cursor, Mapping) else 1
    for line, raw, error, state in _records(path, cursor=cursor):
        values = tuple(raw.split(",")) if raw is not None else None
        diagnostics = (error,) if error else ("field_count_mismatch",) if len(values) != 7 else ()
        kind = ("file_failure" if error in {"unsupported_encoding", "read_error", "source_changed"}
                else "rejected" if diagnostics else "accepted")
        event = RecordEvent(kind, "page", "opt", ordinal, line, None, values, diagnostics,
            next_cursor=_checkpoint(state, "opt", ordinal + 1, None))
        _publish_event(event, on_event, sink)
        if kind != "file_failure":
            ordinal += 1
        if not diagnostics:
            yield OptPage(values, str(path), line)

def resolve_volume_path(volume, value, file, line):
    """Resolve explicit volume-relative paths with either separator."""
    try:
        parts = re.split(r"[/\\]", value)
        if any(p in {"", ".", ".."} or ":" in p or "\0" in p for p in parts):
            raise ValueError
        root = Path(volume).resolve()
        resolved = root.joinpath(*parts).resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError
        return resolved
    except (ValueError, OSError, RuntimeError) as error:
        raise LoadFileError("unsafe_path", file, line) from error
