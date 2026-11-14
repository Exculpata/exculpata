"""Validate stored-only load files without interpreting supplied metadata.

The existing streaming parsers own DAT/OPT grammar and encoding. This boundary
adds the disclosed admission subset, inert-content checks and safe diagnostics.
"""
from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path
from types import MappingProxyType

from case_intelligence.source_load_files import LoadFileError, parse_dat, parse_opt

LOAD_FILE_SUFFIXES = frozenset({".dat", ".opt"})
LOAD_FILE_MEDIA_TYPE = "text/plain"
LOAD_FILE_METADATA_STATUS = "Metadata import is not available yet."
LOAD_FILE_SCAN_TYPES = "DAT/OPT"
LOAD_FILE_SUPPORT_TEXT = (
    "DAT support requires at least two nonempty, distinct column headers and one data record, "
    "U+0014 field separators, optional thorn (þ) quoting, and the same number of fields in every record. "
    "OPT support requires no header, exactly seven comma-separated fields per record, and at least "
    "one nonblank record; quoted commas are not supported. Supported encodings are UTF-8 with an "
    "optional BOM, UTF-16 LE or BE with a BOM, and Windows-1252 detected from the first 64 KiB; "
    "later decoding errors are rejected. Each record must fit the 1 MiB record limit."
)
LOAD_FILE_INTAKE_TEXT = (
    "DAT and OPT files are saved without a preview or searchable text; metadata import is not available yet."
)
LOAD_FILE_COMPLETION_MESSAGE = "Load file stored; no searchable text. Metadata import is not available yet."
LOAD_FILE_COVERAGE_MESSAGE = (
    "Load files are saved without searchable text. Metadata import is not available yet. "
    "Add searchable sources to use search and questions."
)
LOAD_FILE_REJECTION_PREFIX = (
    "This load file was not added because its contents do not match the supported DAT/OPT text format."
)
DIAGNOSTICS = MappingProxyType({
    "unsupported_encoding": "Save the whole file in one of the supported encodings, with a BOM for UTF-16.",
    "mixed_encoding": "Use one supported encoding throughout the file and repair the damaged record.",
    "record_too_large": "Keep each complete record within the 1 MiB record limit.",
    "invalid_header": "Use a valid DAT header with at least two nonempty, distinct fields and matching thorn quotes.",
    "empty_header": "Give every DAT column a nonempty header.",
    "duplicate_header": "Give every DAT column a distinct header.",
    "dat_header_count": "Include at least two nonempty, distinct DAT column headers.",
    "invalid_quoting": "Enclose each quoted DAT field in exactly one matching pair of thorn characters.",
    "truncated_row": "Close every quoted DAT field with its matching thorn character.",
    "dat_field_count": "Make every DAT data record match the header field count.",
    "opt_field_count": "Use exactly seven comma-separated fields in every OPT record; quoted commas are not supported.",
    "missing_records": "Include at least one DAT data record or one OPT record containing a nonblank value.",
    "binary_signature": "Upload a supported text load file, rather than an executable or archive renamed as DAT or OPT.",
    "unsupported_control": "Remove NUL and unsupported control characters; only tabs, line breaks and DAT separators are allowed.",
    "unsafe_file": "Choose the file again; its staged copy must be a regular file.",
    "changed_file": "Choose the file again; its staged contents changed during validation.",
    "read_error": "Choose the file again; its staged copy could not be read completely.",
    "invalid_format": "Use the supported DAT or OPT format and repair the malformed record.",
})
_REASON = re.compile(
    r"DAT/OPT format rejected: (?P<diagnostic>" + "|".join(re.escape(value) for value in DIAGNOSTICS.values())
    + r") Location: (?P<location>file|line [1-9][0-9]{0,19})\."
)
_BINARY_SIGNATURES = (b"MZ", b"\x7fELF", b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _explanation(diagnostic: str, location: str) -> str:
    return f"{LOAD_FILE_REJECTION_PREFIX} {LOAD_FILE_SUPPORT_TEXT} {diagnostic} Location: {location}."


class LoadFileAdmissionError(ValueError):
    """A closed-catalog explanation and a separately constructed durable reason."""

    def __init__(self, code: str, line: int | None = None) -> None:
        self.code = code if code in DIAGNOSTICS else "invalid_format"
        self.line = line if type(line) is int and 0 < line < 10 ** 20 else None
        diagnostic = DIAGNOSTICS[self.code]
        location = f"line {self.line}" if self.line is not None else "file"
        self.durable_reason = f"DAT/OPT format rejected: {diagnostic} Location: {location}."
        super().__init__(_explanation(diagnostic, location))


def display_load_file_reason(reason: str) -> str:
    """Reconstruct only a complete recognized durable reason; preserve all others."""
    if not isinstance(reason, str) or len(reason) > 222:
        return reason
    matched = _REASON.fullmatch(reason)
    return _explanation(matched["diagnostic"], matched["location"]) if matched else reason


def is_stored_load_file(origin: str, media_type: str, name: str) -> bool:
    """Classify by immutable catalog identity, never by readiness or message text."""
    return (origin == "upload" and media_type == LOAD_FILE_MEDIA_TYPE
            and isinstance(name, str) and name.casefold().endswith(tuple(LOAD_FILE_SUFFIXES)))


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _check_cells(values, line: int) -> None:
    for value in values:
        if any(unicodedata.category(character) == "Cc" and character not in "\t\r\n\x14"
               for character in value):
            raise LoadFileAdmissionError("unsupported_control", line)


def _check_records(path: Path, suffix: str) -> None:
    headers = []
    rows = parse_dat(path, headers_out=headers) if suffix == ".dat" else parse_opt(path)
    has_record = False
    checked_headers = False
    try:
        for row in rows:
            if suffix == ".dat" and not checked_headers:
                _check_cells(headers, 1)
                if len(headers) < 2:
                    raise LoadFileAdmissionError("dat_header_count", 1)
                checked_headers = True
            _check_cells(row.values, row.line)
            if suffix == ".opt" and any("\x14" in value for value in row.values):
                raise LoadFileAdmissionError("unsupported_control", row.line)
            has_record |= suffix == ".dat" or any(value.strip() for value in row.values)
    except LoadFileError as error:
        code = error.code
        if code == "field_count_mismatch":
            code = "dat_field_count" if suffix == ".dat" else "opt_field_count"
        raise LoadFileAdmissionError(code, error.line) from None
    finally:
        rows.close()
    if suffix == ".dat":
        _check_cells(headers, 1)
        if len(headers) < 2:
            raise LoadFileAdmissionError("dat_header_count", 1)
    if not has_record:
        raise LoadFileAdmissionError("missing_records")


def validate_load_file(path: str | os.PathLike[str], *, suffix: str) -> None:
    """Exhaust the staged file under its owner's mutation guard without changing it.

    The caller enforces configured upload byte limits before this boundary. The
    opened descriptor and pathname must retain the same regular-file identity
    throughout signature and parser reads. Supplied paths in records stay inert.
    """
    suffix = suffix.casefold()
    if suffix not in LOAD_FILE_SUFFIXES:
        raise LoadFileAdmissionError("invalid_format")
    path = Path(path)
    descriptor = None
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise LoadFileAdmissionError("unsafe_file")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise LoadFileAdmissionError("changed_file")
        if os.read(descriptor, 8).startswith(_BINARY_SIGNATURES):
            raise LoadFileAdmissionError("binary_signature")
        _check_records(path, suffix)
        if (_identity(os.fstat(descriptor)) != _identity(opened)
                or _identity(path.stat(follow_symlinks=False)) != _identity(opened)):
            raise LoadFileAdmissionError("changed_file")
    except OSError:
        raise LoadFileAdmissionError("read_error") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
