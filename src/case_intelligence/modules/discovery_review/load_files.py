"""Read delimited load files already stored in the matter, values as produced.

Nothing is written, normalized or guessed. A row attaches to an item only by
an exact path, a unique file name, or a unique Bates-named file, and the item
shows which of those matched so a reviewer can check it.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterable, Mapping

from case_intelligence.source_load_files import LoadFileError, parse_dat, parse_opt
from case_intelligence.modules.discovery_review.fields import (
    MAX_HEADER_CHARS, Definition, normalize_header,
)

MAX_LOAD_FILE_BYTES = 64 * 1024 * 1024
MAX_LOAD_FILE_ROWS = 2_000_000
DELIMITERS = {".csv": ",", ".tsv": "\t"}


@dataclass(frozen=True, slots=True)
class _SourceFormat:
    category: str
    page_positions: tuple[int, ...] | None = None
    comparison: str = "compatible"
    projection_priority: int = 20
    matching_dependencies: tuple[str, ...] = ()


SOURCE_FORMATS = MappingProxyType({
    "dat": _SourceFormat("document", comparison="exact", projection_priority=0,
                         matching_dependencies=("csv", "tsv")),
    "opt": _SourceFormat("page", (0, 1, 2, 3, 6)),
    "csv": _SourceFormat("document"),
    "tsv": _SourceFormat("document"),
})


def source_format(name: str) -> str:
    """Identify registered inputs without granting admission to future formats."""
    suffix = PurePosixPath(name).suffix.casefold().removeprefix(".")
    return suffix if suffix in SOURCE_FORMATS else ""


def classify_source(path, *, name: str) -> str:
    """Resolve the format of an authorized source before its attempt is created.

    The current DAT/OPT/CSV/TSV readers retain their established name-based
    dispatch. This separate public boundary lets content-based adapters inspect
    the supplied protected path without changing the service's ownership rules.
    Empty means unsupported or ambiguous; the caller records failed coverage.
    """
    return source_format(name)


def record_category(source_format: str) -> str:
    """Share storage categories independently of a source's display format."""
    definition = SOURCE_FORMATS.get(source_format)
    return definition.category if definition is not None else ""


def comparison_policy(left_format: str, right_format: str) -> str:
    """Require exact DAT classes while comparing other supplied facts in common."""
    left, right = SOURCE_FORMATS.get(left_format), SOURCE_FORMATS.get(right_format)
    if left is None or right is None or left.category != "document" or right.category != "document":
        return ""
    return "exact" if left.comparison == right.comparison == "exact" else "compatible"


def projection_priority(source_format: str) -> int:
    """Prefer the primary document format without hiding other raw references."""
    definition = SOURCE_FORMATS.get(source_format)
    if definition is None:
        return 1000
    return definition.projection_priority if definition.category == "document" else 100


def document_format_order() -> tuple[str, ...]:
    return tuple(sorted((name for name in SOURCE_FORMATS if record_category(name) == "document"),
                        key=lambda name: (projection_priority(name), name)))


def matching_dependencies(source_format: str) -> tuple[str, ...]:
    """Return declared evidence prerequisites, rejecting invalid dependency graphs."""
    if source_format not in SOURCE_FORMATS:
        return ()
    dependencies = []

    def visit(name, trail):
        definition = SOURCE_FORMATS.get(name)
        if name in trail:
            raise ValueError("Load-file matching dependencies contain a cycle.")
        if definition is None or definition.category != "document":
            raise ValueError("Load-file matching dependencies require a registered document format.")
        for dependency in definition.matching_dependencies:
            visit(dependency, trail | {name})
            if dependency not in dependencies:
                dependencies.append(dependency)

    definition = SOURCE_FORMATS[source_format]
    if definition.category == "document":
        visit(source_format, frozenset())
    return tuple(dependencies)


def record_semantics(record) -> dict:
    """Read explicit page facts without requiring every raw format to be OPT."""
    if not isinstance(record, Mapping):
        record = {'source_format': record.source_format, 'cells': record.cells,
                  'optional_values': record.optional_values}
    supplied = record.get

    format_name = supplied("source_format", "")
    definition = SOURCE_FORMATS.get(format_name)
    category = record_category(format_name)
    names = ("page_label", "volume", "path", "document_break", "page_count")
    values, shape_valid = dict.fromkeys(names, ""), False
    if definition is not None and category == "page":
        if definition.page_positions is not None:
            cells = supplied("cells")
            if (isinstance(cells, (tuple, list)) and len(cells) == 7
                    and all(isinstance(value, str) for value in cells)):
                values = dict(zip(names, (cells[index] for index in definition.page_positions)))
                shape_valid = True
        else:
            # Later formats preserve their complete raw cells and supply these
            # explicit semantic pairs; no value is inferred from ordinal/path.
            pairs = supplied("optional_values", ())
            if isinstance(pairs, (tuple, list)):
                selected, duplicate = {}, False
                for pair in pairs:
                    if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                            or any(not isinstance(value, str) for value in pair)):
                        duplicate = True
                        break
                    key, value = pair
                    if key in names:
                        duplicate = duplicate or key in selected
                        selected[key] = value
                shape_valid = not duplicate and set(selected) == set(names)
                if shape_valid:
                    values = selected
    boundary = values["document_break"]
    break_valid = shape_valid and boundary in {"", "Y"}
    return {**values, "source_format": format_name, "category": category,
            "boundary": boundary, "break_valid": break_valid,
            "document_break": boundary == "Y",
            "valid": bool(shape_valid and break_valid
                          and values["page_label"].strip() and values["path"].strip())}


@dataclass(frozen=True, slots=True)
class Row:
    line: int
    values: dict[str, str]
    raw_headers: tuple[str, ...] = ()
    raw_cells: tuple[str, ...] = ()
    ordinal: int = 0


@dataclass(slots=True)
class LoadFile:
    document_id: str
    name: str
    headers: tuple[str, ...]
    rows: Iterable[Row]
    path_header: str
    bates_header: str
    problem: str = ""
    control_header: str = ""
    raw_headers: tuple[str, ...] = ()
    ambiguous_headers: tuple[str, ...] = ()
    record_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    complete: bool = True
    file_failure: str = ""
    source_format: str = ""


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
    return LoadFile(document_id, name, (), (), "", "", f"Load file was not read: {reason}.",
                    complete=False)


def _recognition(headers, definition):
    normalized = [normalize_header(header) for header in headers]
    occurrences = Counter(normalized)
    ambiguous = set(header for header, key in zip(headers, normalized) if occurrences[key] > 1)
    def select(aliases):
        selected = [header for header, key in zip(headers, normalized) if key in aliases]
        if len(selected) != 1:
            ambiguous.update(selected if len(selected) > 1 else ())
            return ""
        return selected[0]
    path = select(definition.path_headers)
    semantic = {field.key: select({normalize_header(header) for header in field.headers})
                for field in definition.fields if field.headers}
    return (path, semantic.get("begin_bates", ""), semantic.get("control_number", ""),
            tuple(header for header in headers if header in ambiguous))


def parse_events(path, *, source_format, on_event, cursor=None):
    """Dispatch source events through one parser; later formats extend this seam."""
    parser = {"dat": parse_dat, "opt": parse_opt}.get(source_format)
    if parser is None:
        raise LoadFileError("unsupported_format", path, 1)
    yield from parser(path, on_event=on_event, cursor=cursor)


def _parse_dat_table(path, *, document_id, name, definition, on_event, cursor):
    table = LoadFile(document_id, name, (), (), "", "", complete=False, source_format="dat")
    def set_headers(headers):
        table.raw_headers = tuple(headers or ())
        table.headers = unique_headers(table.raw_headers)
        (table.path_header, table.bates_header, table.control_header,
         table.ambiguous_headers) = _recognition(table.raw_headers, definition)
    def observe(event):
        if on_event is not None:
            on_event(event)
        if event.kind == "header":
            set_headers(event.headers)
        elif event.kind == "file_failure":
            table.file_failure = event.diagnostics[0]
        else:
            table.record_count += 1
            if event.kind == "accepted":
                table.accepted_count += 1
            else:
                table.rejected_count += 1
        if event.diagnostics and not table.problem:
            table.problem = f"Line {event.line}: {', '.join(event.diagnostics)}. All record findings remain available from the import."
    if cursor is not None:
        set_headers(cursor.get("headers"))
    records = parse_dat(path, on_event=observe, cursor=cursor)
    first = next(records, None)
    ordinal = cursor.get("ordinal", 1) if cursor is not None else 1
    last_ordinal = ordinal - 1
    def adapt(row):
        return Row(row.line, dict(zip(table.headers, row.values)),
                   tuple(row.headers), tuple(row.values), last_ordinal)
    def rows():
        nonlocal last_ordinal
        try:
            if first is not None:
                last_ordinal = ordinal + table.record_count - 1
                yield adapt(first)
            for row in records:
                last_ordinal = ordinal + table.record_count - 1
                yield adapt(row)
            table.complete = not bool(table.file_failure)
        finally:
            records.close()
    table.rows = rows()
    if first is None:
        table.complete = not bool(table.file_failure)
    return table


def parse(raw=None, *, document_id: str, name: str, definition: Definition,
          path=None, on_event=None, cursor=None) -> LoadFile | None:
    """Return a table or explicit refusal; None means a recognized non-load table."""
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix == ".dat":
        admitted_path = path if path is not None else raw
        if not isinstance(admitted_path, (str, Path)):
            return rejected(document_id, name, "DAT requires an admitted source path")
        return _parse_dat_table(admitted_path, document_id=document_id, name=name,
                               definition=definition, on_event=on_event, cursor=cursor)
    delimiter = DELIMITERS.get(suffix)
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
    path_header, bates_header, control_header, ambiguous = _recognition(produced, definition)
    if not path_header and not bates_header and not control_header:
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
    return LoadFile(document_id, name, headers, tuple(rows), path_header, bates_header, problem,
                    control_header, tuple(produced), ambiguous, len(rows), len(rows),
                    complete=not bool(problem), source_format=suffix[1:])


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
        matched = extra = row_count = 0
        for row in load_file.rows:
            row_count += 1
            found = matcher.find(load_file, row)
            if found is None:
                continue
            if found[0] in matches:
                extra += 1
                continue
            matched += 1
            matches[found[0]] = Match(load_file.name, row.line, found[1], row.values)
        reports.append(Report(load_file.document_id, load_file.name, max(row_count, load_file.record_count),
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
            path_header, bates_header, control_header, ambiguous = _recognition(headers, definition)
            state['headers'] = list(headers)
            state['path_header'] = path_header
            state['bates_header'] = bates_header
            state['recognized'] = bool(path_header or bates_header or control_header)
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
