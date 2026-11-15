"""Read-only DAT and OPT parsing with bounded, source-cited diagnostics."""
import codecs
import hashlib
import json
import os
import re
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

DatRow = namedtuple("DatRow", "values headers file line")
OptPage = namedtuple("OptPage", "values file line")
LoadFileLine = namedtuple("LoadFileLine", "text file line")
_MAX_RECORD = 1024 * 1024
_FIELD_ALIASES = {
    "BEGDOC": "begin_bates", "BEGBATES": "begin_bates", "ENDDOC": "end_bates",
    "ENDBATES": "end_bates", "BEGATTACH": "begin_attachment", "ENDATTACH": "end_attachment",
    "TEXT": "text_path", "TEXTPATH": "text_path", "NATIVE": "native_path",
    "NATIVEPATH": "native_path", "DATE": "date", "MD5": "md5", "SHA1": "sha1", "SHA256": "sha256",
}


@dataclass(frozen=True)
class RecordEvent:
    """One lossless source record, observed before legacy diagnostic capping."""

    kind: str
    category: str
    source_format: str
    ordinal: int
    line: int
    headers: tuple[str, ...] | None = None
    cells: tuple[str, ...] | None = None
    diagnostics: tuple[str, ...] = ()
    optional_values: tuple[tuple[str, str], ...] = ()
    next_cursor: Mapping | None = None

class LoadFileError(ValueError):
    def __init__(self, code, file, line):
        super().__init__(f"{file}:{line}: {code}")
        self.code, self.file, self.line = code, str(file), line

@dataclass
class Finding:
    code: str
    file: str
    line: int
    truncated_code: str = ""
    count: int = 0

class _Findings:
    def __init__(self, target, path):
        self.target, self.file, self.counts, self.summaries = target, str(path), {}, {}

    def add(self, code, line):
        if self.target is None:
            raise LoadFileError(code, self.file, line)
        count = self.counts.get(code, 0)
        if count < 1000:
            self.target.append(Finding(code, self.file, line))
            self.counts[code] = count + 1
        else:
            if code not in self.summaries:
                self.summaries[code] = Finding("findings_truncated", self.file, line, code)
                self.target.append(self.summaries[code])
            self.summaries[code].count += 1

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

def _encoding(path):
    with Path(path).open("rb") as stream:
        sample = stream.read(65536)
        if sample.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            raise LoadFileError("unsupported_encoding", path, 1)
        if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16"
        if sample.startswith(codecs.BOM_UTF8):
            return "utf-8-sig"
        if b"\0" in sample or any(b"\0" in chunk for chunk in iter(lambda: stream.read(65536), b"")):
            raise LoadFileError("unsupported_encoding", path, 1)
        try:
            codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
            return "utf-8"
        except UnicodeDecodeError:
            return "cp1252"

def _decode_chunk(decoder, raw, final):
    """Handle malformed bytes locally, retaining decodable text and quote parity."""
    parts = []
    while True:
        state = decoder.getstate()
        try:
            parts.append(decoder.decode(raw, final=final))
            return "".join(parts)
        except UnicodeDecodeError as error:
            decoder.setstate(state)
            prefix_length = error.start - len(state[0])
            parts.append(decoder.decode(raw[:max(0, prefix_length)], final=False))
            parts.append("\udcff")
            decoder.setstate((b"", decoder.getstate()[1]))
            raw = error.object[error.end:]

def _decode_error(error):
    """Mark damaged bytes while retaining following quotes and physical lines."""
    return "\udcff", error.end


codecs.register_error("load_file_damaged_record", _decode_error)


def _source_identity(metadata):
    return [metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns]


def _cursor_digest(cursor):
    return hashlib.sha256(json.dumps(cursor, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode("ascii")).hexdigest()


def _checkpoint(state, source_format, ordinal, headers):
    if state is None:
        return None
    result = dict(state, source_format=source_format, ordinal=ordinal,
                  headers=list(headers) if headers is not None else None, version=1)
    result["checksum"] = _cursor_digest(result)
    return result


def _resume_cursor(path, cursor, source_format, identity):
    """Accept only intact, version-bound internal completed-record checkpoints."""
    keys = {"version", "source_format", "encoding", "cookie", "line", "ordinal", "headers", "source", "checksum"}
    invalid = (not isinstance(cursor, Mapping) or set(cursor) != keys)
    if not invalid:
        data = dict(cursor)
        checksum = data.pop("checksum")
        invalid = (
            type(data["version"]) is not int or data["version"] != 1 or data["source_format"] != source_format
            or not isinstance(data["encoding"], str)
            or data["encoding"] not in {"utf-8", "utf-8-sig", "utf-16", "cp1252"}
            or any(type(data[key]) is not int or data[key] < minimum
                   for key, minimum in (("cookie", 0), ("line", 1), ("ordinal", 1)))
            or (isinstance(data["cookie"], int) and data["cookie"].bit_length() > 256)
            or not isinstance(data["source"], list) or len(data["source"]) != 5
            or any(type(value) is not int or value < 0 for value in data["source"])
            or (source_format == "opt" and data["headers"] is not None)
            or (source_format == "dat" and (not isinstance(data["headers"], list)
                or any(not isinstance(value, str) for value in data["headers"])))
        )
        if not invalid:
            invalid = (sum(len(value) for value in data["headers"] or ()) > _MAX_RECORD
                       or checksum != _cursor_digest(data))
    if invalid:
        raise LoadFileError("invalid_cursor", path, 1)
    if data["source"] != identity:
        raise LoadFileError("source_changed", path, data["line"])
    return data


def _physical_chunks(stream):
    while raw := stream.readline(8192):
        if raw.endswith("\r"):
            boundary = stream.tell()
            following = stream.read(1)
            if following == "\n":
                raw += following
            else:
                stream.seek(boundary)
        yield raw


def _records(path, dat=False, cursor=None):
    """Resume at a decoded record boundary; bound even damaged physical lines."""
    line = start = cursor["line"] if isinstance(cursor, Mapping) and type(cursor.get("line")) is int else 1
    parts, size, quoted, error, seen = [], 0, False, "", False
    try:
        identity = _source_identity(Path(path).stat())
        resumed = _resume_cursor(path, cursor, "dat" if dat else "opt", identity) if cursor is not None else None
        encoding = resumed["encoding"] if resumed else _encoding(path)
        byte_encoding = {"utf-16": "utf-16-le", "utf-8-sig": "utf-8"}.get(encoding, encoding)
        with Path(path).open("r", encoding=encoding, errors="load_file_damaged_record", newline="") as stream:
            if _source_identity(os.fstat(stream.fileno())) != identity:
                raise LoadFileError("source_changed", path, max(1, line))
            if resumed:
                try:
                    stream.seek(resumed["cookie"])
                except (ValueError, OverflowError, OSError) as failure:
                    raise LoadFileError("invalid_cursor", path, line) from failure
                if stream.tell() != resumed["cookie"]:
                    raise LoadFileError("invalid_cursor", path, line)
            def checkpoint():
                return {"encoding": encoding, "cookie": stream.tell(), "line": line, "source": identity}
            for raw in _physical_chunks(stream):
                seen = True
                quoted ^= dat and bool(raw.count("þ") % 2)
                if not error:
                    error = "mixed_encoding" if "\udcff" in raw else ""
                    size += len(raw.encode(byte_encoding)) if not error else 0
                    if size > _MAX_RECORD and not error:
                        error = "record_too_large"
                    if error:
                        parts.clear()
                    else:
                        parts.append(raw)
                if raw.endswith(("\r", "\n")):
                    line += 1
                    if not quoted:
                        yield start, None if error else "".join(parts).removesuffix("\n").removesuffix("\r"), error, checkpoint()
                        parts, size, error, start = [], 0, "", line
            if dat and not seen and cursor is None:
                yield 1, None, "invalid_header", None
            if parts or error:
                error = error or ("truncated_row" if quoted else "")
                yield start, None if error else "".join(parts), error, None if quoted else checkpoint()
            if (_source_identity(os.fstat(stream.fileno())) != identity
                    or _source_identity(Path(path).stat()) != identity):
                raise LoadFileError("source_changed", path, max(1, line))
    except LoadFileError as failure:
        if cursor is not None and failure.code in {"invalid_cursor", "source_changed"}:
            raise
        yield failure.line, None, failure.code, None
    except (OSError, UnicodeError):
        yield max(1, start), None, "read_error", None

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


def _publish_event(event, on_event, sink):
    if on_event is not None:
        on_event(event)
    if sink.target is not None or on_event is None:
        for code in event.diagnostics:
            sink.add(code, event.line)


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
