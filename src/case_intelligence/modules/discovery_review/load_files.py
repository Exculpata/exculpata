"""Read delimited load files already stored in the matter, values as produced.

Nothing is written, normalized or guessed. A row attaches to an item only by
an exact path, a unique file name, or a unique Bates-named file, and the item
shows which of those matched so a reviewer can check it.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import PurePosixPath

from case_intelligence.modules.discovery_review.fields import (
    MAX_HEADER_CHARS, Definition, normalize_header,
)

MAX_LOAD_FILE_BYTES = 64 * 1024 * 1024
MAX_LOAD_FILE_ROWS = 2_000_000
DELIMITERS = {".csv": ",", ".tsv": "\t"}


@dataclass(frozen=True, slots=True)
class Row:
    line: int
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class LoadFile:
    document_id: str
    name: str
    headers: tuple[str, ...]
    rows: tuple[Row, ...]
    path_header: str
    bates_header: str
    problem: str = ""


@dataclass(frozen=True, slots=True)
class Match:
    name: str
    line: int
    basis: str
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class Report:
    document_id: str
    name: str
    rows: int
    matched: int
    extra: int
    problem: str


def unique_headers(headers) -> tuple[str, ...]:
    """Keep headers as produced; a repeated header gains a visible ordinal."""
    reserved, allocated = set(headers), set()
    named = []
    for header in headers:
        label, ordinal = header, 2
        while label in allocated or (label != header and label in reserved):
            label = f"{header} ({ordinal})"
            ordinal += 1
        allocated.add(label)
        named.append(label)
    return tuple(named)


def rejected(document_id: str, name: str, reason: str) -> LoadFile:
    """Retain an unread candidate in reports and collision safety checks."""
    return LoadFile(document_id, name, (), (), "", "", f"Load file was not read: {reason}.")


def parse(raw: bytes, *, document_id: str, name: str, definition: Definition) -> LoadFile | None:
    """Return a table or explicit refusal; None means a recognized non-load table."""
    delimiter = DELIMITERS.get(PurePosixPath(name).suffix.casefold())
    if delimiter is None:
        return None
    if len(raw) > MAX_LOAD_FILE_BYTES:
        return rejected(document_id, name, "file exceeds the byte limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return rejected(document_id, name, "invalid UTF-8 encoding")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        produced = next(reader, None)
    except csv.Error:
        return rejected(document_id, name, "header could not be parsed")
    if not produced or any(not header or len(header) > MAX_HEADER_CHARS for header in produced):
        return rejected(document_id, name, "header is empty or exceeds the length limit")
    headers = unique_headers(produced)
    bates = {normalize_header(h) for h in definition.by_key[definition.default_sort].headers}
    path_header = next((h for h in headers if normalize_header(h) in definition.path_headers), "")
    bates_header = next((h for h in headers if normalize_header(h) in bates), "")
    if not path_header and not bates_header:
        return None
    rows, problem = [], ""
    try:
        for values in reader:
            if not any(values):
                continue
            if len(values) != len(headers):
                problem = f"Line {reader.line_num} has {len(values)} values for {len(headers)} headers; later rows were not read."
                break
            if len(rows) >= MAX_LOAD_FILE_ROWS:
                problem = f"Only the first {MAX_LOAD_FILE_ROWS} rows were read."
                break
            rows.append(Row(reader.line_num, {
                header: value for header, value in zip(headers, values)}))
    except csv.Error:
        problem = f"Line {reader.line_num} could not be parsed; later rows were not read."
    return LoadFile(document_id, name, headers, tuple(rows), path_header, bates_header, problem)


def path_key(value: str) -> str:
    """Compare volume paths across separators, leading dots and letter case."""
    parts = [part for part in value.replace("\\", "/").split("/") if part not in {"", "."}]
    return "/".join(parts).casefold()


class Matcher:
    """Attach each row to at most one item; ambiguity attaches nothing."""

    def __init__(self, paths: dict[str, str]):
        """``paths`` maps document id to its matter-relative path."""
        self.exact, self.by_name, self.by_stem = {}, {}, {}
        for document_id, path in paths.items():
            key = path_key(path)
            if not key:
                continue
            self.exact.setdefault(key, []).append(document_id)
            name = key.rsplit("/", 1)[-1]
            self.by_name.setdefault(name, []).append((key, document_id))
            self.by_stem.setdefault(name.rsplit(".", 1)[0], []).append(document_id)

    def find(self, load_file: LoadFile, row: Row) -> tuple[str, str] | None:
        if load_file.path_header and (key := path_key(row.values[load_file.path_header])):
            if len(found := self.exact.get(key, ())) == 1:
                return found[0], "path"
            named = [document_id for candidate, document_id in self.by_name.get(key.rsplit("/", 1)[-1], ())
                     if candidate.endswith("/" + key) or key.endswith("/" + candidate) or candidate == key]
            if len(named) == 1:
                return named[0], "file name"
            if found or named:
                return None
        if load_file.bates_header and (label := row.values[load_file.bates_header].strip().casefold()):
            if len(found := self.by_stem.get(label, ())) == 1:
                return found[0], "Bates-named file"
        return None


def attach(load_files, paths: dict[str, str]) -> tuple[dict[str, Match], tuple[Report, ...]]:
    """Join rows to items. The first matching row wins and extras are counted."""
    matcher, matches, reports = Matcher(paths), {}, []
    for load_file in load_files:
        matched = extra = 0
        for row in load_file.rows:
            found = matcher.find(load_file, row)
            if found is None:
                continue
            if found[0] in matches:
                extra += 1
                continue
            matched += 1
            matches[found[0]] = Match(load_file.name, row.line, found[1], row.values)
        reports.append(Report(load_file.document_id, load_file.name, len(load_file.rows),
                              matched, extra, load_file.problem))
    return matches, tuple(reports)


def resume_record(text, name, definition, state=None):
    """Consume exactly one physical CSV record boundary, without replaying records."""
    state = dict(state or {'offset': 0, 'line': 0, 'count': 0, 'headers': [],
                           'path_header': '', 'bates_header': '', 'problem': '', 'eof': False,
                           'recognized': False})
    if state['eof']:
        return state, None, 0
    content = io.StringIO(text, newline='')
    content.seek(state['offset'])

    def lines():
        while line := content.readline():
            yield line

    reader = csv.reader(lines(), delimiter=DELIMITERS[PurePosixPath(name).suffix.casefold()], strict=True)
    try:
        values = next(reader, None)
    except csv.Error:
        state.update(offset=content.tell(), line=state['line'] + reader.line_num, eof=True)
        state['problem'] = (f"Line {state['line']} could not be parsed; later rows were not read."
                            if state['headers'] else 'Load file was not read: header could not be parsed.')
        return state, None, 1
    state.update(offset=content.tell(), line=state['line'] + reader.line_num)
    if not state['headers']:
        if not values or any(not value or len(value) > MAX_HEADER_CHARS for value in values):
            state.update(eof=True, problem='Load file was not read: header is empty or exceeds the length limit.')
        else:
            headers = unique_headers(values)
            bates = {normalize_header(h) for h in definition.by_key[definition.default_sort].headers}
            state['headers'] = list(headers)
            state['path_header'] = next((h for h in headers if normalize_header(h) in definition.path_headers), '')
            state['bates_header'] = next((h for h in headers if normalize_header(h) in bates), '')
            state['recognized'] = bool(state['path_header'] or state['bates_header'])
            state['eof'] = not state['recognized']
        return state, None, 1
    if values is None:
        state['eof'] = True
        return state, None, 0
    if not any(values):
        return state, None, 1
    if len(values) != len(state['headers']):
        state.update(eof=True, problem=f"Line {state['line']} has {len(values)} values for {len(state['headers'])} headers; later rows were not read.")
        return state, None, 1
    if state['count'] >= MAX_LOAD_FILE_ROWS:
        state.update(eof=True, problem=f'Only the first {MAX_LOAD_FILE_ROWS} rows were read.')
        return state, None, 1
    state['count'] += 1
    return state, Row(state['line'], dict(zip(state['headers'], values))), 1
