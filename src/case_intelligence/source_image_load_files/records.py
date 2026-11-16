"""Shared source records and diagnostics, independent of format recognition."""
from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from typing import Mapping

OptPage = namedtuple("OptPage", "values file line", module="case_intelligence.source_load_files")
LoadFileLine = namedtuple("LoadFileLine", "text file line", module="case_intelligence.source_load_files")


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


class FindingSink:
    """Retain the legacy strict-or-capped findings contract."""

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


def publish_event(event, on_event, sink):
    if on_event is not None:
        on_event(event)
    if sink.target is not None or on_event is None:
        for code in event.diagnostics:
            sink.add(code, event.line)


@dataclass(frozen=True)
class DiiDocument:
    """Supplied document facts and an observation, never a page-Bates record."""

    begin_bates: str
    page_count: str | None
    observed_image_entries: int
    file: str
    line: int
    ordinal: int
    headers: tuple[str, ...]
    cells: tuple[str, ...]
    optional_values: tuple[tuple[str, str], ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecodedLine:
    """Bounded physical text or an explicit failure with trustworthy boundaries."""

    text: str | None
    file: str
    line: int
    diagnostic: str = ""
    boundary_complete: bool = False
    before: Mapping | None = None
    after: Mapping | None = None
