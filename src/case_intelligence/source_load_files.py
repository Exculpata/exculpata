"""Read-only DAT and OPT parsing with bounded, source-cited diagnostics."""
import codecs
import re
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

DatRow = namedtuple("DatRow", "values headers file line")
OptPage = namedtuple("OptPage", "values file line")
_MAX_RECORD = 1024 * 1024
_FIELD_ALIASES = {
    "BEGDOC": "begin_bates", "BEGBATES": "begin_bates", "ENDDOC": "end_bates",
    "ENDBATES": "end_bates", "BEGATTACH": "begin_attachment", "ENDATTACH": "end_attachment",
    "TEXT": "text_path", "TEXTPATH": "text_path", "NATIVE": "native_path",
    "NATIVEPATH": "native_path", "DATE": "date", "MD5": "md5", "SHA1": "sha1", "SHA256": "sha256",
}

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

def _physical_chunks(stream, encoding):
    if encoding == "utf-8-sig":
        stream.read(len(codecs.BOM_UTF8))
        encoding = "utf-8"
    decoder = codecs.getincrementaldecoder(encoding)()
    pending_cr = ""
    while True:
        raw = stream.read(8192)
        text = pending_cr + _decode_chunk(decoder, raw, final=not raw)
        pending_cr = "\r" if text.endswith("\r") and raw else ""
        if pending_cr:
            text = text[:-1]
        yield from re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+", text)
        if not raw:
            return

def _records(path, dat=False):
    """Discard damaged record text until its quote parity closes, citing it once."""
    line, start, parts, size, quoted, skipping, seen = 1, 1, [], 0, False, False, False
    try:
        encoding = _encoding(path)
        byte_encoding = {"utf-16": "utf-16-le", "utf-8-sig": "utf-8"}.get(encoding, encoding)
        with Path(path).open("rb") as stream:
            for raw in _physical_chunks(stream, encoding):
                seen = True
                quoted ^= dat and bool(raw.count("þ") % 2)
                if not skipping:
                    bad = "mixed_encoding" if "\udcff" in raw else ""
                    size += len(raw.encode(byte_encoding)) if not bad else 0
                    if size > _MAX_RECORD and not bad:
                        bad = "record_too_large"
                    if bad:
                        parts.clear()
                        skipping = True
                        yield start, None, bad
                    else:
                        parts.append(raw)
                if raw.endswith(("\r", "\n")):
                    line += 1
                    if not quoted:
                        if not skipping:
                            yield start, "".join(parts).removesuffix("\n").removesuffix("\r"), ""
                        parts, size, skipping, start = [], 0, False, line
            if dat and not seen:
                yield 1, None, "invalid_header"
            if parts:
                yield start, None if quoted else "".join(parts), "truncated_row" if quoted else ""
    except LoadFileError as error:
        yield error.line, None, error.code
    except OSError:
        yield max(1, start), None, "read_error"

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

def parse_dat(path, *, findings=None, headers_out=None):
    """Stream unchanged rows; append header cells to headers_out; exhaust for findings."""
    sink, headers = _Findings(findings, path), None
    for line, raw, error in _records(path, dat=True):
        if error:
            sink.add("invalid_header" if headers is None and error not in {"unsupported_encoding", "read_error"} else error, line)
            headers = () if headers is None else headers
            continue
        values = _dat_values(raw)
        if values is None:
            sink.add("invalid_header" if headers is None else "invalid_quoting", line)
            headers = () if headers is None else headers
        elif headers is None:
            headers = values
            if headers_out is not None:
                headers_out.extend(headers)
            for code, bad in (("duplicate_header", len(set(headers)) != len(headers)),
                              ("empty_header", not all(headers))):
                if bad:
                    sink.add(code, line)
        elif headers and len(values) != len(headers):
            sink.add("field_count_mismatch", line)
        else:
            yield DatRow(values, headers, str(path), line)

def parse_opt(path, *, findings=None):
    """Stream seven unchanged fields; exhaust the iterator to finalize findings."""
    sink = _Findings(findings, path)
    for line, raw, error in _records(path):
        if error:
            sink.add(error, line)
        elif len(values := tuple(raw.split(","))) != 7:
            sink.add("field_count_mismatch", line)
        else:
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
