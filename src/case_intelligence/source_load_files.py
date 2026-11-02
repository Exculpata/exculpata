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

def propose_field_map(headers):
    return {h: _FIELD_ALIASES[h] for h in headers if h in _FIELD_ALIASES}

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

codecs.register_error("load_file_invalid", lambda error: ("\udcff", error.end))

def _records(path, dat=False):
    """Yield physical start line, record and error; discard oversized physical tails."""
    line, start, parts, size, quoted, ended_cr = 0, 1, [], 0, False, False
    try:
        encoding = _encoding(path)
        byte_encoding = {"utf-16": "utf-16-le", "utf-8-sig": "utf-8"}.get(encoding, encoding)
        with Path(path).open(encoding=encoding, errors="load_file_invalid", newline="") as stream:
            while raw := stream.readline(_MAX_RECORD + 1):
                if ended_cr and raw == "\n":
                    ended_cr = False
                    continue
                ended_cr = raw.endswith("\r")
                line += 1
                bad = "mixed_encoding" if "\udcff" in raw else ""
                size += len(raw.encode(byte_encoding, errors="surrogatepass")) if not bad else 0
                if size > _MAX_RECORD or len(raw) > _MAX_RECORD:
                    bad = "record_too_large"
                    while raw and not raw.endswith(("\n", "\r")):
                        raw = stream.readline(_MAX_RECORD + 1)
                    ended_cr = raw.endswith("\r")
                if not bad:
                    parts.append(raw)
                    quoted ^= dat and bool(raw.count("þ") % 2)
                    if quoted:
                        continue
                yield start, None if bad else "".join(parts).removesuffix("\n").removesuffix("\r"), bad
                parts, size, quoted, start = [], 0, False, line + 1
            if dat and not line:
                yield 1, None, "invalid_header"
            if parts:
                yield start, None, "truncated_row"
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
        elif token == "\x14" and not quoted:
            values.append("".join(current))
            current = []
        else:
            current.append(token)
    return (*values, "".join(current))

def parse_dat(path, *, findings=None, field_map=None):
    """Stream unchanged rows. Mapping is reserved for QC and never rewrites values."""
    sink, headers = _Findings(findings, path), None
    for line, raw, error in _records(path, dat=True):
        if error:
            sink.add("invalid_header" if headers is None and error != "unsupported_encoding" else error, line)
            headers = () if headers is None else headers
            continue
        values = _dat_values(raw)
        if headers is None:
            headers = values
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
