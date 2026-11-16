"""Shared bounded decoding and source-bound completed-record checkpoints."""
import codecs
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from .records import DecodedLine, LoadFileError

MAX_RECORD_BYTES = 1024 * 1024

def detect_encoding(path):
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

def _decode_error(error):
    """Mark damaged bytes while retaining following quotes and physical lines."""
    return "\udcff", error.end


codecs.register_error("load_file_damaged_record", _decode_error)


def source_identity(metadata):
    return [metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns]


def cursor_digest(cursor):
    return hashlib.sha256(json.dumps(cursor, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode("ascii")).hexdigest()


def make_cursor(state, source_format, ordinal, headers):
    if state is None:
        return None
    result = dict(state, source_format=source_format, ordinal=ordinal,
                  headers=list(headers) if headers is not None else None, version=1)
    result["checksum"] = cursor_digest(result)
    return result


def validate_cursor(path, cursor, source_format, identity):
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
            or (source_format in {"opt", "lfp", "dii"} and data["headers"] is not None)
            or (source_format == "dat" and (not isinstance(data["headers"], list)
                or any(not isinstance(value, str) for value in data["headers"])))
        )
        if not invalid:
            invalid = (sum(len(value) for value in data["headers"] or ()) > MAX_RECORD_BYTES
                       or checksum != cursor_digest(data))
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


def iter_records(path, dat=False, cursor=None, source_format=None):
    """Resume at a decoded record boundary; bound even damaged physical lines."""
    line = start = cursor["line"] if isinstance(cursor, Mapping) and type(cursor.get("line")) is int else 1
    parts, size, quoted, error, seen = [], 0, False, "", False
    try:
        identity = source_identity(Path(path).stat())
        format_name = source_format or ("dat" if dat else "opt")
        resumed = validate_cursor(path, cursor, format_name, identity) if cursor is not None else None
        encoding = resumed["encoding"] if resumed else detect_encoding(path)
        byte_encoding = {"utf-16": "utf-16-le", "utf-8-sig": "utf-8"}.get(encoding, encoding)
        with Path(path).open("r", encoding=encoding, errors="load_file_damaged_record", newline="") as stream:
            if source_identity(os.fstat(stream.fileno())) != identity:
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
                    if size > MAX_RECORD_BYTES and not error:
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
            if (source_identity(os.fstat(stream.fileno())) != identity
                    or source_identity(Path(path).stat()) != identity):
                raise LoadFileError("source_changed", path, max(1, line))
    except LoadFileError as failure:
        if cursor is not None and failure.code in {"invalid_cursor", "source_changed"}:
            raise
        yield failure.line, None, failure.code, None
    except (OSError, UnicodeError):
        yield max(1, start), None, "read_error", None


class DecodedLines:
    """Physical lines with only the current before/after decoder checkpoints."""

    def __init__(self, path, *, source_format, cursor=None):
        self.path, self.source_format = path, source_format
        self.first_ordinal, self.previous = 1, None
        if cursor is not None:
            try:
                checked = validate_cursor(path, cursor, source_format, source_identity(Path(path).stat()))
            except OSError as error:
                raise LoadFileError("read_error", path, 1) from error
            self.first_ordinal = checked["ordinal"]
            self.previous = {key: checked[key] for key in ("encoding", "cookie", "line", "source")}
        self.records = iter_records(path, cursor=cursor, source_format=source_format)
        self.current, self.exhausted = None, False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            line, raw, error, state = next(self.records)
        except StopIteration:
            self.exhausted = True
            raise
        before = self.previous
        if before is None and state is not None:
            before = dict(state, cookie=0, line=1)
        self.current = DecodedLine(raw, str(self.path), line, error, state is not None, before, state)
        self.previous = state
        return self.current

    def record_cursor(self, record):
        """Locate the next logical boundary without replaying prior records."""
        current = self.current
        if current is None or record.kind == "file_failure":
            return None
        if record.next_line is None:
            state = current.after if self.exhausted else None
        elif record.next_line == current.line:
            state = current.before
        elif record.next_line == current.line + 1:
            state = current.after
        else:
            raise LoadFileError("invalid_cursor", self.path, record.line)
        return make_cursor(state, self.source_format, record.ordinal + 1, None)

    def close(self):
        self.records.close()
