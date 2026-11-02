"""Pure, read-only parsing and quality checks for DAT and OPT volumes."""

from __future__ import annotations

from dataclasses import dataclass
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
        return dict(zip(self.headers, self.values))


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


@dataclass(frozen=True)
class FieldProfile:
    producer: str
    fields: Mapping[str, str]

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


def _encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    if sample.startswith(b"\xff\xfe"):
        encoding = "utf-16"
    elif sample.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    elif sample.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif b"\x00" in sample:
        raise LoadFileError("broken or mixed encoding", path, 1)
    else:
        try:
            sample.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "cp1252"
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if sample.find(bom, len(bom) if sample.startswith(bom) else 0) >= 0:
            raise LoadFileError("BOM in middle of file; mixed encoding", path, 1)
    return encoding


def parse_dat(path: str | Path) -> Iterator[DatRow]:
    """Stream Concordance-delimited rows, retaining header spelling and locations."""
    path = Path(path)
    encoding = _encoding(path)
    try:
        stream = path.open("r", encoding=encoding, errors="strict", newline="")
        with stream:
            row, field, quoted, start, physical = [], [], False, 1, 1
            headers = None
            while (char := stream.read(1)):
                if char == "\ufeff":
                    if stream.tell() != 1:
                        raise LoadFileError("BOM in middle of file", path, physical)
                    continue
                if char == "þ":
                    quoted = not quoted
                elif char == "\x14" and not quoted:
                    row.append("".join(field)); field = []
                elif char in "\r\n" and not quoted:
                    if char == "\r":
                        next_char = stream.read(1)
                        if next_char != "\n" and next_char:
                            stream.seek(stream.tell() - 1)
                    row.append("".join(field)); field = []
                    if any(row):
                        if headers is None:
                            headers = tuple(row)
                        elif len(row) != len(headers):
                            raise LoadFileError(
                                f"field count {len(row)} does not match header count {len(headers)}",
                                path, start)
                        else:
                            yield DatRow(tuple(row), headers, str(path), start)
                    row, start = [], physical + 1
                    physical += 1
                else:
                    field.append("\n" if char == "®" else char)
                    if char == "\n":
                        physical += 1
            if quoted:
                raise LoadFileError("truncated quoted row", path, start)
            if row or field:
                raise LoadFileError("truncated last row", path, start)
    except UnicodeError as exc:
        raise LoadFileError("broken or mixed encoding", path, physical) from exc


def parse_opt(path: str | Path) -> Iterator[OptPage]:
    """Stream seven-column Opticon page rows without normalizing page keys."""
    path = Path(path)
    try:
        with path.open("r", encoding=_encoding(path), errors="strict", newline="") as stream:
            for line, raw in enumerate(stream, 1):
                values = raw.rstrip("\r\n").split(",")
                if len(values) != 7:
                    raise LoadFileError(f"OPT row has {len(values)} columns, expected 7", path, line)
                yield OptPage(*values, str(path), line)
    except UnicodeError as exc:
        raise LoadFileError("broken or mixed encoding", path, line) from exc


def resolve_volume_path(volume: str | Path, value: str, file: str, line: int) -> Path:
    """Resolve a relative path and reject lexical and symlink escapes."""
    root, candidate = Path(volume).resolve(), Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise LoadFileError("path must be contained in volume", file, line)
    resolved = (root / candidate).resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise LoadFileError("path must be contained in volume", file, line)
    return resolved


def _digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def qc_volume(dat_path: str | Path, opt_path: str | Path, *,
              field_map: Mapping[str, str] | None = None) -> QCReport:
    """Parse and join one volume, returning cited findings without writes."""
    dat_path, opt_path = Path(dat_path), Path(opt_path)
    root, findings = dat_path.parent, []
    rows, pages = list(parse_dat(dat_path)), list(parse_opt(opt_path))
    mapping = dict(field_map or (propose_field_map(rows[0].headers) if rows else {}))

    def value(row: DatRow, target: str) -> str:
        return row.as_dict().get(next((h for h, t in mapping.items() if t == target), ""), "")

    def add(code: str, message: str, file: str, line: int) -> None:
        findings.append(Finding(code, message, file, line))

    page_keys = {page.image_key for page in pages}
    doc_keys = {value(row, "begin_bates") for row in rows}
    for key in sorted(doc_keys & {key for key in doc_keys if sum(value(r, "begin_bates") == key for r in rows) > 1}):
        row = next(r for r in rows if value(r, "begin_bates") == key)
        add("bates_duplicate", f"Bates key {key!r} is duplicated", row.file, row.line)
    for row in rows:
        begin = value(row, "begin_bates")
        if begin not in page_keys:
            add("dat_only_key", f"{begin!r} appears only in DAT", row.file, row.line)
        for target in ("text_path", "native_path"):
            if raw := value(row, target):
                try:
                    resolved = resolve_volume_path(root, raw, row.file, row.line)
                    if not resolved.exists():
                        add("missing_file", f"{raw!r} does not exist", row.file, row.line)
                except LoadFileError as exc:
                    add("unsafe_path", str(exc).split(": ", 1)[-1], row.file, row.line)
        for algorithm in ("md5", "sha1", "sha256"):
            if expected := value(row, algorithm):
                raw = value(row, "native_path") or value(row, "text_path")
                try:
                    resolved = resolve_volume_path(root, raw, row.file, row.line)
                    if raw and resolved.is_file() and _digest(resolved, algorithm).lower() != expected.lower():
                        add("hash_mismatch", f"{algorithm} does not match {raw!r}", row.file, row.line)
                except LoadFileError:
                    pass
        if raw_date := value(row, "date"):
            try:
                datetime.fromisoformat(raw_date)
            except ValueError:
                add("unparseable_date", f"date {raw_date!r} is not ISO format", row.file, row.line)
        parent, end = value(row, "begin_attachment"), value(row, "end_attachment")
        if (parent or end) and (not parent or parent not in doc_keys):
            add("orphan_attachment", f"attachment parent {parent!r} is absent", row.file, row.line)
    for page in pages:
        if page.document_break.upper() == "Y" and page.image_key not in doc_keys:
            add("opt_only_key", f"{page.image_key!r} appears only in OPT", page.file, page.line)
        try:
            resolved = resolve_volume_path(root, page.path, page.file, page.line)
            if not resolved.exists():
                add("missing_file", f"{page.path!r} does not exist", page.file, page.line)
        except LoadFileError as exc:
            add("unsafe_path", str(exc).split(": ", 1)[-1], page.file, page.line)
    spans = []
    for row in rows:
        begin, end = value(row, "begin_bates"), value(row, "end_bates")
        left, right = re.fullmatch(r"([A-Za-z]+)(\d+)", begin), re.fullmatch(r"([A-Za-z]+)(\d+)", end)
        if left and right and left[1] == right[1]:
            spans.append((left[1], int(left[2]), int(right[2]), row))
    spans.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(spans, spans[1:]):
        if previous[0] == current[0] and current[1] > previous[2] + 1:
            add("bates_missing", f"Missing Bates between {previous[3].as_dict()} and {current[3].as_dict()}",
                current[3].file, current[3].line)
        elif previous[0] == current[0] and current[1] <= previous[2]:
            add("bates_overlap", f"Bates range beginning {value(current[3], 'begin_bates')!r} overlaps",
                current[3].file, current[3].line)
    referenced = set()
    for row in rows:
        for target in ("text_path", "native_path"):
            if raw := value(row, target):
                try:
                    referenced.add(resolve_volume_path(root, raw, row.file, row.line))
                except LoadFileError:
                    pass
    for page in pages:
        try:
            referenced.add(resolve_volume_path(root, page.path, page.file, page.line))
        except LoadFileError:
            pass
    for path in root.rglob("*"):
        if path.is_file() and path not in referenced and path not in {dat_path, opt_path}:
            add("orphan_file", f"{path.relative_to(root)} is not referenced", str(path), 1)
    return QCReport(len(rows), len(pages), tuple(findings))
