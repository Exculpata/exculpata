"""Pure, read-only parsing and quality checks for DAT and OPT volumes."""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import groupby
from types import MappingProxyType
import unicodedata
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Iterator, Mapping

class LoadFileError(ValueError):
    """A load-file error with source location."""

    def __init__(self, message: str, file: str | Path, line: int):
        super().__init__(f"{file}:{line}: {message}")
        self.file, self.line = str(file), line


@dataclass(frozen=True)
class DatRow:
    values: tuple[str, ...]
    headers: tuple[str, ...]
    file: str
    line: int

    def as_dict(self) -> dict[str, str]:
        if not all(self.headers) or len(set(self.headers)) != len(self.headers):
            raise LoadFileError("ambiguous headers", self.file, 1)
        return dict(zip(self.headers, self.values, strict=True))


@dataclass(frozen=True)
class OptPage:
    image_key: str
    volume: str
    path: str
    document_break: str
    box: str
    folder: str
    page_count: str
    file: str
    line: int


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    file: str
    line: int


@dataclass(frozen=True)
class QCReport:
    document_count: int
    page_count: int
    findings: tuple[Finding, ...]
    joined_pages: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    families: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)


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

    def to_data(self) -> dict[str, object]:
        return {"producer": self.producer, "fields": dict(self.fields)}


_FIELD_ALIASES = {
    "BEGDOC": "begin_bates", "BEGBATES": "begin_bates", "ENDDOC": "end_bates",
    "ENDBATES": "end_bates", "BEGATTACH": "begin_attachment", "ENDATTACH": "end_attachment",
    "TEXT": "text_path", "TEXTPATH": "text_path", "NATIVE": "native_path",
    "NATIVEPATH": "native_path", "DATE": "date", "MD5": "md5", "SHA1": "sha1",
    "SHA256": "sha256",
}


def propose_field_map(headers: tuple[str, ...]) -> dict[str, str]:
    """Return conservative suggestions without changing original headers."""
    return {header: _FIELD_ALIASES[key] for header in headers
            if (key := re.sub(r"[^A-Z0-9]", "", header.upper())) in _FIELD_ALIASES}


_MAX_RECORD = 1024 * 1024
_PLAIN = re.compile(r"([A-Za-z]+)([0-9]{1,18})\Z")


def _encoding(path: Path) -> str:
    with path.open("rb") as stream:
        sample = stream.read(65536)
    if sample.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise LoadFileError("unsupported UTF-32; mixed encoding", path, 1)
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return "utf-8" if b"\xc3\xbe" in sample else "cp1252"


def _problem(findings, code, path, line):
    message = code.replace("_", " ")
    if findings is None:
        raise LoadFileError(message, path, line)
    findings.append(Finding(code, message, str(path), line))


def _records(path, findings, dat=False):
    """Bound records; universal newline decoding peeks across CR without seeks."""
    line, start, parts, size, quoted, ended_cr = 0, 1, [], 0, False, False
    try:
        with Path(path).open(encoding=_encoding(Path(path)), errors="replace", newline="") as stream:
            while raw := stream.readline(_MAX_RECORD + 1):
                if ended_cr and raw == "\n":
                    ended_cr = False
                    continue
                ended_cr = raw.endswith("\r")
                line += 1
                if size + len(raw) > _MAX_RECORD:
                    _problem(findings, "record_too_large", path, start)
                    while not raw.endswith(("\n", "\r")) and raw:
                        raw = stream.readline(_MAX_RECORD + 1)
                    ended_cr = raw.endswith("\r")
                    yield start, None
                    parts, size, quoted, start = [], 0, False, line + 1
                    continue
                if any(marker in raw for marker in ("\ufffd", "\ufeff", "\ufffe", "ï»¿", "Ã¾")):
                    _problem(findings, "mixed_encoding", path, line)
                    ended_cr = raw.endswith("\r")
                    yield start, None
                    parts, size, quoted, start = [], 0, False, line + 1
                    continue
                parts.append(raw)
                size += len(raw)
                quoted ^= dat and bool(raw.count("þ") % 2)
                if quoted:
                    continue
                if dat and not raw.endswith(("\n", "\r")):
                    _problem(findings, "truncated_row", path, start)
                elif raw not in ("\n", "\r", "\r\n") or len(parts) > 1:
                    yield start, "".join(parts).removesuffix("\n").removesuffix("\r")
                parts, size, start = [], 0, line + 1
            if parts:
                _problem(findings, "truncated_row", path, start)
    except LoadFileError:
        if findings is None:
            raise
        _problem(findings, "mixed_encoding", path, max(1, line))
    except OSError:
        _problem(findings, "read_error", path, max(1, line))


def _dat_values(raw):
    values = raw.split("\x14")
    if all("þ" not in v or (v.startswith("þ") and v.endswith("þ") and v.count("þ") == 2) for v in values):
        return tuple(v[1:-1] if v.startswith("þ") else v for v in values)
    values, current, quoted = [], [], False
    for token in re.split(r"(þ|\x14)", raw):
        if token == "þ":
            quoted = not quoted
        elif token == "\x14" and not quoted:
            values.append("".join(current)); current = []
        else:
            current.append(token)
    return (*values, "".join(current))


def parse_dat(path: str | Path, *, findings=None, field_map=None) -> Iterator[DatRow]:
    """Stream rows; a findings sink enables recovery instead of strict errors."""
    headers, protected = None, set()
    for line, raw in _records(path, findings, dat=True):
        if raw is None:
            if headers is None:
                headers = ()
                _problem(findings, "invalid_header", path, line)
            continue
        if "\x00" in raw:
            _problem(findings, "nul_byte", path, line)
            if headers is None:
                headers = ()
                _problem(findings, "invalid_header", path, line)
                continue
        values = _dat_values(raw)
        if headers is None:
            headers = values
            mapping = propose_field_map(headers) if field_map is None else field_map
            protected = {i for i, h in enumerate(headers) if mapping.get(h) in
                         {"begin_bates", "end_bates", "begin_attachment", "end_attachment"}}
            for code, bad in (("duplicate_header", len(set(headers)) != len(headers)),
                              ("empty_header", not all(headers))):
                if bad:
                    _problem(findings, code, path, line)
        elif len(values) != len(headers):
            _problem(findings, "field_count_mismatch", path, line)
        else:
            stored = values if len(protected) == len(headers) else tuple(
                v if i in protected else v.replace("®", "\n") for i, v in enumerate(values))
            yield DatRow(stored, headers, str(path), line)


def parse_opt(path: str | Path, *, findings=None) -> Iterator[OptPage]:
    """Stream seven-column Opticon rows without changing keys or path strings."""
    for line, raw in _records(path, findings):
        if raw is None:
            continue
        values = raw.split(",")
        if len(values) != 7:
            _problem(findings, "field_count_mismatch", path, line)
            continue
        if "\x00" in raw:
            _problem(findings, "nul_byte", path, line)
        yield OptPage(*values, str(path), line)


def resolve_volume_path(volume: str | Path, value: str, file: str, line: int) -> Path:
    """Resolve only nonempty, contained POSIX volume paths."""
    candidate = Path(value)
    try:
        root = Path(volume).resolve()
        if (not value or value == "." or "\\" in value or "\x00" in value
                or re.match(r"^[A-Za-z]:", value) or candidate.is_absolute() or ".." in candidate.parts):
            raise ValueError
        resolved = (root / candidate).resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError
        return resolved
    except (ValueError, OSError, RuntimeError) as exc:
        raise LoadFileError("path must be contained in volume", file, line) from exc


def _digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _plain(label):
    match = _PLAIN.fullmatch(label)
    return (match[1], int(match[2]), len(match[2])) if match else None


def _range_findings(records, joined, add, file):
    members, blocked, unusual = {}, set(), []
    incomplete, unreadable = False, False
    for begin, end, line in records:
        a = _plain(begin)
        b = a if not end or end == begin else _plain(end)
        count = len(joined[begin]) if begin in joined else 1
        if not a or not b or not end:
            add("unparseable_bates", file, line)
        elif a[0] != b[0] or a[1] > b[1]:
            add("prefix_mismatch" if a[0] != b[0] else "reversed_range", file, line)
            blocked.update((a[0], b[0]))
        if a and b and a[0] == b[0] and a[1] <= b[1]:
            members.setdefault(a[0], []).append((a[1], b[1], a[2], b[2], line, count))
        else:
            for p in (a, b):
                if p:
                    members.setdefault(p[0], []).append((p[1], p[1], p[2], p[2], line, 1))
        if not a or not b or not end:
            unusual.append((begin, end, count))
        for label in (() if a and b and end else (begin, end)):
            if not label.isascii() or not label.isprintable():
                folded = unicodedata.normalize("NFKC", label)
                unreadable |= not folded.isascii() or any(c != " " and (
                    unicodedata.category(c) in {"Cc", "Cf", "Mn", "Me"} or
                    unicodedata.category(c).startswith("Z")) for c in label)
            if unicodedata.normalize("NFKC", label).strip().casefold() in {"", "n/a", "na", "slipsheet"}:
                incomplete = True
    for prefix, spans in sorted(members.items()):
        spans.sort()
        last = max(s[1] for s in spans)
        intervals = []
        for begin, end, count in unusual:
            labels = (begin, end or begin)
            folded = [unicodedata.normalize("NFKC", label).casefold() for label in labels]
            if not any(not ((p := _plain(label)) and p[0] == prefix) and
                       (prefix.casefold() in re.sub("[^a-z]", "", norm) or
                        (re.search("[0-9]", norm) and not re.search("[a-z]", norm)))
                       for label, norm in zip(labels, folded)):
                continue
            numbers = set()
            for label, norm in zip(labels, folded):
                if p := _plain(label):
                    numbers.add(p[1])
                    continue
                digits = re.sub("[^0-9]", "", norm)
                if not digits or len(digits) > 24:
                    numbers = set(); break
                numbers.update(int(digits[i:j]) for i in range(len(digits))
                               for j in range(i + 1, min(len(digits), i + 18) + 1))
            intervals.append((min(numbers), max(numbers) + count - 1) if numbers else (-1, last))
        right, widths = -1, set()
        touches = [(s[0], s[0] + s[5] - 1) for s in spans if s[5] > s[1] - s[0] + 1]
        for lo, group in groupby(spans, key=lambda s: s[0]):
            group = tuple(group)
            if lo <= right:
                for span in group:
                    add("bates_overlap", file, span[4])
            if right >= 0 and lo > right + 1 and prefix not in blocked and not unreadable:
                uncertain = incomplete or len(widths | {s[2] for s in group}) != 1 or any(
                    a < lo and b > right for a, b in touches)
                cuts = sorted({right + 1, lo} | {n for a, b in intervals if a < lo and b > right
                                                for n in (max(a, right + 1), min(b + 1, lo))})
                for lower, upper in zip(cuts, cuts[1:]):
                    kind = "possible" if uncertain or any(a <= lower <= b for a, b in intervals) else "missing"
                    add("bates_" + kind, file, group[0][4], f"Bates {prefix}: {lower} {upper - 1}")
            for _, hi, _, width, _, _ in group:
                if hi > right:
                    right, widths = hi, {width}
                elif hi == right:
                    widths.add(width)


def qc_volume(dat_path: str | Path, opt_path: str | Path, *,
              field_map: Mapping[str, str] | None = None) -> QCReport:
    """Read one volume; retain compact keys, relationships and cited findings."""
    dat_path, opt_path = Path(dat_path), Path(opt_path)
    root, findings = dat_path.parent.resolve(), []
    docs, records, joined, families, referenced = {}, [], {}, {}, set()
    document_count = page_count = 0

    def add(code, file, line, message=None):
        findings.append(Finding(code, message or code.replace("_", " "), str(file), line))

    @lru_cache(maxsize=4096)
    def inspect_path(raw):
        try:
            path = resolve_volume_path(root, raw, str(dat_path), 1)
            return path, None if path.is_file() else "missing_file"
        except LoadFileError:
            return None, "unsafe_path"
        except OSError:
            return None, "read_error"

    def check_path(raw, file, line):
        path, code = inspect_path(raw)
        if path:
            referenced.add(path)
        if code:
            add(code, file, line)
        return path if code is None else None

    if field_map is not None:
        try:
            FieldProfile("volume", field_map)
        except ValueError:
            add("invalid_field_map", dat_path, 1)
            field_map = {}
    digest = lru_cache(maxsize=128)(_digest)
    indexes, duplicates, algorithms = None, set(), ()
    for row in parse_dat(dat_path, findings=findings, field_map=field_map):
        document_count += 1
        if indexes is None:
            mapping = propose_field_map(row.headers) if field_map is None else field_map
            try:
                FieldProfile("volume", mapping)
                row.as_dict()
                if not set(mapping) <= set(row.headers):
                    raise ValueError
                indexes = {t: row.headers.index(h) for h, t in mapping.items()}
                algorithms = tuple(a for a in ("md5", "sha1", "sha256") if a in indexes)
            except ValueError:
                add("invalid_field_map", dat_path, 1)
                indexes = {}
        if not indexes:
            continue
        values = {t: row.values[i] for t, i in indexes.items()}
        begin, end = values.get("begin_bates", ""), values.get("end_bates", "")
        record = (begin, end, row.line)
        records.append(record)
        if begin in docs:
            if begin not in duplicates:
                add("bates_duplicate", dat_path, docs[begin][2]); duplicates.add(begin)
            add("bates_duplicate", dat_path, row.line)
        else:
            docs[begin] = record
        paths = {t: check_path(raw, dat_path, row.line) for t in ("text_path", "native_path")
                 if (raw := values.get(t))}
        for algorithm in algorithms:
            path = paths.get("native_path" if values.get("native_path") else "text_path")
            if path and (expected := values.get(algorithm)):
                try:
                    if digest(path, algorithm).lower() != expected.lower():
                        add("hash_mismatch", dat_path, row.line)
                except OSError:
                    add("read_error", dat_path, row.line)
        if raw_date := values.get("date"):
            try:
                datetime.fromisoformat(raw_date)
            except ValueError:
                add("unparseable_date", dat_path, row.line)
        parent, tail = values.get("begin_attachment", ""), values.get("end_attachment", "")
        if parent or tail:
            families.setdefault((parent, tail), []).append(begin)
    page_seen, duplicates, current, declared, first_line = {}, set(), None, "", 1

    def finish_group():
        a, b = bounds
        span_mismatch = (a and b and a[0] == b[0] and a[1] <= b[1]
                         and group_count != b[1] - a[1] + 1)
        if current is not None and (not re.fullmatch("[0-9]{1,18}", declared)
                                   or int(declared) != group_count or span_mismatch):
            add("page_count_mismatch", opt_path, first_line)

    group_count, bounds = 0, (None, None)
    for page in parse_opt(opt_path, findings=findings):
        page_count += 1
        key = page.image_key
        if key in page_seen:
            if key not in duplicates:
                add("opt_duplicate", opt_path, page_seen[key]); duplicates.add(key)
            add("opt_duplicate", opt_path, page.line)
        else:
            page_seen[key] = page.line
        if page.document_break.upper() == "Y":
            finish_group()
            current, declared, first_line, group_count = key, page.page_count, page.line, 0
            a = _plain(current)
            end = docs[current][1] if current in docs else ""
            bounds = (a, a if end == current else _plain(end))
            if current not in docs:
                add("opt_only_key", opt_path, page.line)
        if current is None:
            add("opt_only_key", opt_path, page.line)
        else:
            joined.setdefault(current, []).append(key)
            group_count += 1
            if current in docs:
                a, b = bounds
                p = a if key == current else _plain(key)
                if not (a and b and p and a[0] == b[0] == p[0] and a[1] <= p[1] <= b[1]):
                    add("page_outside_range", opt_path, page.line)
        check_path(page.path, opt_path, page.line)
    finish_group()
    del page_seen
    for key in joined:
        joined[key] = tuple(joined[key])
    for begin, end, line in records:
        if begin not in joined:
            add("dat_only_key", dat_path, line)
    if families:
        ends = {r[1] for r in records}
        for (parent, tail), children in families.items():
            if parent not in docs or tail not in ends:
                for child in children:
                    add("orphan_attachment", dat_path, docs[child][2])
            families[(parent, tail)] = tuple(children)
    _range_findings(records, joined, add, dat_path)
    excluded = {dat_path.resolve(), opt_path.resolve()}
    for path in root.rglob("*"):
        try:
            resolved = path.resolve()
            if path.is_file() and resolved not in referenced and resolved not in excluded:
                add("orphan_file", path, 1)
        except (OSError, RuntimeError):
            add("read_error", path, 1)
    return QCReport(document_count, page_count, tuple(findings), joined, families)
