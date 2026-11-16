"""Validate stored-only load files without interpreting supplied metadata.

The existing streaming parsers own DAT/OPT grammar and encoding. This boundary
adds the disclosed admission subset, inert-content checks and safe diagnostics.
"""
from __future__ import annotations

import os
import hashlib
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from case_intelligence.source_load_files import LoadFileError, iter_load_file_lines, parse_dat, parse_opt

LOAD_FILE_SUFFIXES = frozenset({".dat", ".opt", ".lfp", ".dii"})
LOAD_FILE_MEDIA_TYPE = "text/plain"
LOAD_FILE_METADATA_STATUS = "Production documents shows supported metadata when available."
LOAD_FILE_SCAN_TYPES = "DAT/OPT/LFP/DII"
LOAD_FILE_SUPPORT_TEXT = (
    "DAT support requires at least two nonempty, distinct column headers and one data record, "
    "U+0014 field separators, optional thorn (þ) quoting, and the same number of fields in every record. "
    "OPT support requires no header, exactly seven comma-separated fields per record, and at least "
    "one nonblank record; quoted commas are not supported. Supported encodings are UTF-8 with an "
    "optional BOM, UTF-16 LE or BE with a BOM, and Windows-1252 detected from the first 64 KiB; "
    "later decoding errors are rejected. Each record must fit the 1 MiB record limit."
)
LOAD_FILE_INTAKE_TEXT = (
    "Load files are saved without searchable text. Production documents shows supported metadata when available."
)
LOAD_FILE_COMPLETION_MESSAGE = "Load file stored; no searchable text. " + LOAD_FILE_METADATA_STATUS
LOAD_FILE_COVERAGE_MESSAGE = (
    "Load files are saved without searchable text. " + LOAD_FILE_METADATA_STATUS + " "
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

IMAGE_LOAD_FILE_SUPPORT_TEXT = (
    "LFP supports single-image IM records with a supplied key, D or blank boundary, offset 0, "
    "filename/type or volume/directory/filename/type location, type 2 or 4, and optional rotation 0–3. "
    "DII supports @FULLTEXT DOC, @T document blocks, required @BATESBEG, optional @PGCOUNT or "
    "@C PGCOUNT, and one @D @I directory followed by explicit .tif, .tiff, .png, .jpg or .jpeg filenames. "
    "Quoted delimiters, multiline values, unknown commands, and <<DOC>> are unsupported. "
    "The DAT/OPT encoding policy applies; each physical line and retained document metadata "
    "must fit 1 MiB. Image entries are counted without a whole-document size cap. "
    "Admission preserves originals without certifying associations or page Bates."
)
IMAGE_LOAD_FILE_REJECTION_PREFIX = (
    "This load file was not added because its contents do not match the supported LFP/DII text format."
)
_IMAGE_TEXT_POLICY = MappingProxyType({
    "encodings": ("utf-8", "utf-8-sig", "utf-16-le-bom", "utf-16-be-bom", "cp1252-sample"),
    "newlines": ("\n", "\r\n", "\r"),
    "max_line_bytes": 1024 * 1024,
    "allowed_controls": frozenset({"\t"}),
    "forbidden_format_controls": True,
    "unverified_marker": "<<DOC>>",
    "escaping": "none",
})
LFP_GRAMMAR = MappingProxyType({
    "revision": "ipro-im-single-image-v1-proposed",
    "text": _IMAGE_TEXT_POLICY,
    "command": "IM",
    "delimiter": ",",
    "fields": ("command", "image_key", "boundary", "offset", "location"),
    "optional_fields": ("rotation",),
    "boundaries": frozenset({"D", "", " "}),
    "document_start": "D",
    "offsets": frozenset({"0"}),
    "location_delimiter": ";",
    "location_forms": (("filename", "image_type"), ("volume", "directory", "filename", "image_type")),
    "volume_marker": "@",
    "types": frozenset({"2", "4"}),
    "rotations": frozenset({"0", "1", "2", "3"}),
    "max_key_chars": 70,
    "max_volume_chars": 30,
    "recoverable_line_errors": frozenset({"record_too_large", "mixed_encoding"}),
})
DII_GRAMMAR = MappingProxyType({
    "revision": "summation-document-explicit-bates-v1-proposed",
    "text": _IMAGE_TEXT_POLICY,
    "preamble": "@FULLTEXT DOC",
    "start_tag": "@T",
    "required_tags": frozenset({"@BATESBEG"}),
    "scalar_tags": MappingProxyType({
        "@T": "record_identifier", "@BATESBEG": "document_begin", "@DOCID": "document_identifier",
        "@PGCOUNT": "declared_count", "@C PGCOUNT": "declared_count", "@PARENTID": "parent_identifier",
    }),
    "directory_prefix": "@D @I\\",
    "directory_terminator": "\\",
    "value_separator": " ",
    "max_count": 9_223_372_036_854_775_807,
    "max_count_digits": 19,
    "max_metadata_bytes": 1024 * 1024,
    "min_image_entries": 1,
    "image_suffixes": frozenset({".tif", ".tiff", ".png", ".jpg", ".jpeg"}),
    "multiline": False,
    "repeat_scalar": "reject",
    "unknown_command": "stop",
})
IMAGE_DIAGNOSTICS = MappingProxyType({
    "unsupported_encoding": DIAGNOSTICS["unsupported_encoding"],
    "mixed_encoding": DIAGNOSTICS["mixed_encoding"],
    "read_error": DIAGNOSTICS["read_error"],
    "unsafe_file": DIAGNOSTICS["unsafe_file"],
    "changed_file": DIAGNOSTICS["changed_file"],
    "source_changed": DIAGNOSTICS["changed_file"],
    "binary_signature": "Upload a supported text index, not a renamed executable or archive.",
    "unsupported_control": "Remove NUL, embedded byte-order marks and unsupported control characters.",
    "record_too_large": "Keep each physical line within the 1 MiB decoded-text limit.",
    "metadata_too_large": "Keep each document's retained scalar metadata within 1 MiB.",
    "unsupported_command": "Use only the documented commands; unknown or multiline commands are unsupported.",
    "unverified_syntax": "The <<DOC>> syntax is unsupported; use the documented narrow tagged format.",
    "lfp_field_count": "Use the five required IM fields and at most one optional rotation field.",
    "lfp_key": "Supply a nonblank image key of at most 70 characters without quoted delimiters.",
    "lfp_boundary": "Use D for a document start or a blank boundary for a continuation.",
    "lfp_offset": "Use offset 0; other image representations are outside this supported subset.",
    "lfp_location": "Use filename;type or @volume;directory;filename;type without quoted delimiters.",
    "lfp_type": "Use image type 2 or 4 in the supported single-image subset.",
    "lfp_rotation": "Omit rotation or supply exactly 0, 1, 2 or 3.",
    "unsafe_reference": "Use inert relative image references without traversal, drives, URLs or expansions.",
    "dii_mode": "Begin the file with exactly @FULLTEXT DOC; other DII modes are unsupported.",
    "dii_block": "Use @T, required @BATESBEG, one @D @I directory and explicit image filenames per document.",
    "dii_value": "Supply one nonblank value on the tag's physical line; multiline values are unsupported.",
    "dii_duplicate": "Supply each scalar and directory once; the two count spellings name the same scalar.",
    "dii_order": "Place supported scalar tags before the document's image directory and image entries.",
    "dii_count": "Supply a positive ASCII page count within the signed 64-bit integer range.",
    "missing_records": "Include at least one complete supported IM record or DII document block.",
    "invalid_format": "Use a supported IM index or the documented document-only DII subset.",
})
_IMAGE_REASON = re.compile(
    r"LFP/DII format rejected: (?P<diagnostic>" + "|".join(re.escape(value) for value in IMAGE_DIAGNOSTICS.values())
    + r") Location: (?P<location>file|line [1-9][0-9]{0,19})\."
)


@dataclass(frozen=True)
class ImageLoadFileClassification:
    format: str
    outcome: str
    code: str = ""
    line: int | None = None


@dataclass(frozen=True)
class ImageIndexLine:
    """A physical decoder result, optionally without recoverable source text.

    A decoder may assert boundary_complete only when it consumed the entire
    damaged physical line and retains a trustworthy next-line checkpoint.
    Strict admission continues using the raising public decoder instead.
    """

    text: str | None
    line: int
    diagnostic: str = ""
    boundary_complete: bool = False

    def __post_init__(self):
        if (type(self.line) is not int or self.line < 1 or type(self.boundary_complete) is not bool
                or (self.text is not None and not isinstance(self.text, str))
                or not isinstance(self.diagnostic, str)
                or (self.diagnostic and self.diagnostic not in IMAGE_DIAGNOSTICS)
                or (self.text is None and not self.diagnostic)):
            raise ValueError("A decoder result requires a cited physical line and explicit known failure.")


@dataclass(frozen=True)
class ImageIndexRecord:
    """Bounded raw structure for validation; it makes no document association."""

    source_format: str
    kind: str
    ordinal: int
    line: int
    headers: tuple[str, ...] = ()
    cells: tuple[str, ...] | None = ()
    optional_values: tuple[tuple[str, str], ...] = ()
    observed_image_entries: int = 0
    diagnostics: tuple[str, ...] = ()
    next_line: int | None = None


def _image_text_problem(text, grammar):
    policy = grammar["text"]
    try:
        if len(text.encode("utf-8")) > policy["max_line_bytes"]:
            return "record_too_large"
    except UnicodeError:
        return "mixed_encoding"
    if policy["unverified_marker"] in text:
        return "unverified_syntax"
    if any((unicodedata.category(char) == "Cc" and char not in policy["allowed_controls"])
           or (policy["forbidden_format_controls"] and unicodedata.category(char) == "Cf") for char in text):
        return "unsupported_control"
    return ""


def classify_image_load_file(path) -> ImageLoadFileClassification:
    """Recognize bounded source text, independently of its eligible filename.

    Recognition is not whole-file validation. The caller must validate the same
    authorized version completely before admission or successful interpretation.
    """
    lines = iter_load_file_lines(path)
    try:
        first = next(lines, None)
        if first is None:
            return ImageLoadFileClassification("", "unsupported", "missing_records")
        problem = _image_text_problem(first.text, LFP_GRAMMAR)
        if problem:
            return ImageLoadFileClassification("", "malformed", problem, first.line)
        if first.text.startswith(LFP_GRAMMAR["command"] + LFP_GRAMMAR["delimiter"]):
            return ImageLoadFileClassification("lfp", "recognized", line=first.line)
        if first.text == DII_GRAMMAR["preamble"]:
            return ImageLoadFileClassification("dii", "recognized", line=first.line)
        if first.text.startswith(("@FULLTEXT", DII_GRAMMAR["start_tag"] + " ")):
            return ImageLoadFileClassification("", "unsupported", "dii_mode", first.line)
        return ImageLoadFileClassification("", "unsupported", "invalid_format", first.line)
    except LoadFileError as error:
        return ImageLoadFileClassification("", "malformed", error.code, error.line)
    finally:
        lines.close()


def _safe_reference(value, *, directory=False, empty=False):
    """Compare inert syntax only; never consult the referenced filesystem."""
    if not value:
        return empty
    if not value.strip() or value.startswith(("/", "\\", "~")):
        return False
    if any(char in value for char in (":", '"', "`", "$", "*", "?", "[", "]", "{", "}", "<", ">", "|")):
        return False
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        return False
    parts = re.split(r"[/\\]", value)
    return (directory or len(parts) == 1) and all(part not in {"", ".", ".."} for part in parts)


def _lfp_record(line, ordinal):
    grammar = LFP_GRAMMAR
    fields = tuple(line.text.split(grammar["delimiter"]))
    names = grammar["fields"] + grammar["optional_fields"]
    headers = names[:len(fields)] if len(fields) <= len(names) else ()
    codes, optional = [], []
    problem = _image_text_problem(line.text, grammar)
    if problem:
        codes.append(problem)
    elif len(fields) not in {len(grammar["fields"]), len(names)}:
        codes.append("lfp_field_count")
    else:
        values = dict(zip(names, fields))
        if values["command"] != grammar["command"]:
            codes.append("unsupported_command")
        if (not values["image_key"].strip() or len(values["image_key"]) > grammar["max_key_chars"]
                or any(char in values["image_key"] for char in ('"', grammar["location_delimiter"]))):
            codes.append("lfp_key")
        if values["boundary"] not in grammar["boundaries"]:
            codes.append("lfp_boundary")
        if values["offset"] not in grammar["offsets"]:
            codes.append("lfp_offset")
        if "rotation" in values and values["rotation"] not in grammar["rotations"]:
            codes.append("lfp_rotation")
        location = values["location"].split(grammar["location_delimiter"])
        form = next((candidate for candidate in grammar["location_forms"] if len(candidate) == len(location)), None)
        if form is None or '"' in values["location"]:
            codes.append("lfp_location")
        else:
            parts = dict(zip(form, location))
            optional.extend(("location_" + key, value) for key, value in parts.items())
            if parts["image_type"] not in grammar["types"]:
                codes.append("lfp_type")
            if not _safe_reference(parts["filename"]):
                codes.append("unsafe_reference")
            if "volume" in parts:
                volume = parts["volume"]
                if (not volume.startswith(grammar["volume_marker"]) or not volume[1:].strip()
                        or len(volume[1:]) > grammar["max_volume_chars"] or not _safe_reference(volume[1:])
                        or not _safe_reference(parts["directory"], directory=True)):
                    codes.append("unsafe_reference")
    return ImageIndexRecord("lfp", "rejected" if codes else "accepted", ordinal, line.line,
        headers, fields, tuple(optional), diagnostics=tuple(dict.fromkeys(codes)), next_line=line.line + 1)


class _DiiBlock:
    """Retain only bounded scalars and counters for the current document."""

    def __init__(self, ordinal, line):
        self.ordinal, self.line = ordinal, line
        self.headers, self.cells, self.codes, self.seen = [], [], [], set()
        self.metadata_bytes = self.image_entries = 0
        self.directory_seen = False
        self.recoverable = True

    def problem(self, code):
        if code not in self.codes:
            self.codes.append(code)

    def unavailable_raw(self):
        self.recoverable = False
        self.headers.clear()
        self.cells.clear()

    def retain(self, tag, value):
        self.metadata_bytes += len((tag + DII_GRAMMAR["value_separator"] + value).encode("utf-8"))
        if self.metadata_bytes > DII_GRAMMAR["max_metadata_bytes"]:
            self.problem("metadata_too_large")
            self.unavailable_raw()
        if self.recoverable:
            self.headers.append(tag)
            self.cells.append(value)

    def scalar(self, tag, value):
        grammar = DII_GRAMMAR
        identity = grammar["scalar_tags"][tag]
        if identity in self.seen:
            self.problem("dii_duplicate")
            self.retain(tag, value)
            return
        self.seen.add(identity)
        if self.directory_seen:
            self.problem("dii_order")
        if not value.strip():
            self.problem("dii_value")
        if identity == "declared_count" and (
                not value.isascii() or not value.isdigit() or len(value) > grammar["max_count_digits"]
                or not 1 <= int(value) <= grammar["max_count"]):
            self.problem("dii_count")
        self.retain(tag, value)

    def directory(self, text):
        grammar = DII_GRAMMAR
        if self.directory_seen:
            self.problem("dii_duplicate")
            self.retain("@D", text[len("@D "):])
            return
        self.directory_seen = True
        rest = text[len(grammar["directory_prefix"]):]
        if rest and not rest.endswith(grammar["directory_terminator"]):
            self.problem("unsafe_reference")
        else:
            relative = rest[:-len(grammar["directory_terminator"])] if rest else ""
            if not _safe_reference(relative, directory=True, empty=not rest):
                self.problem("unsafe_reference")
        self.retain("@D", text[len("@D "):])

    def finish(self, next_line=None):
        grammar = DII_GRAMMAR
        required = {grammar["scalar_tags"][tag] for tag in grammar["required_tags"]}
        if (not required.issubset(self.seen) or not self.directory_seen
                or self.image_entries < grammar["min_image_entries"]):
            self.problem("dii_block")
        return ImageIndexRecord("dii", "rejected" if self.codes else "accepted", self.ordinal, self.line,
            tuple(self.headers), tuple(self.cells) if self.recoverable else None,
            (("observed_image_entries", str(self.image_entries)),), self.image_entries,
            tuple(self.codes), next_line)


def _dii_scalar(text):
    separator = DII_GRAMMAR["value_separator"]
    for tag in DII_GRAMMAR["scalar_tags"]:
        if text == tag:
            return tag, ""
        if text.startswith(tag + separator):
            return tag, text[len(tag + separator):]
    return None


def _iter_dii_records(lines, *, first_ordinal, expect_preamble):
    grammar = DII_GRAMMAR
    block, ordinal, first = None, first_ordinal - 1, expect_preamble
    try:
        for line in lines:
            line_error = getattr(line, "diagnostic", "")
            if line_error or line.text is None:
                code = line_error or "invalid_format"
                if block is not None:
                    block.problem(code)
                    block.unavailable_raw()
                    yield block.finish()
                # Missing raw text cannot prove whether an opaque multiline
                # command began, even when the physical boundary is complete.
                yield ImageIndexRecord("dii", "file_failure", ordinal + 1, line.line,
                                       cells=None, diagnostics=(code,))
                return
            problem = _image_text_problem(line.text, grammar)
            if first:
                first = False
                if problem or line.text != grammar["preamble"]:
                    yield ImageIndexRecord("dii", "file_failure", first_ordinal, line.line, cells=None,
                                           diagnostics=(problem or "dii_mode",))
                    return
                continue
            scalar = _dii_scalar(line.text)
            starts = scalar is not None and scalar[0] == grammar["start_tag"] and scalar[1].strip() and not problem
            if starts:
                if block is not None:
                    yield block.finish(line.line)
                ordinal += 1
                block = _DiiBlock(ordinal, line.line)
                block.scalar(*scalar)
                continue
            if block is None:
                yield ImageIndexRecord("dii", "file_failure", first_ordinal, line.line, cells=None,
                                       diagnostics=(problem or "dii_block",))
                return
            opaque_command = ((line.text.lstrip().startswith("@") or (problem and "@" in line.text))
                              and scalar is None and not line.text.startswith(grammar["directory_prefix"]))
            if opaque_command or problem == "unverified_syntax":
                code = "unsupported_command" if opaque_command else problem
                block.problem(code)
                if problem:
                    block.problem(problem)
                yield block.finish()
                yield ImageIndexRecord("dii", "file_failure", ordinal + 1, line.line,
                                       cells=None, diagnostics=(code,))
                return
            if problem:
                block.problem(problem)
                if problem == "record_too_large":
                    block.unavailable_raw()
                elif scalar is not None:
                    block.scalar(*scalar)
                elif line.text.startswith(grammar["directory_prefix"]):
                    block.directory(line.text)
                continue
            if scalar is not None:
                block.scalar(*scalar)
            elif line.text.startswith(grammar["directory_prefix"]):
                block.directory(line.text)
            elif not block.directory_seen:
                block.problem("dii_order")
            elif (not _safe_reference(line.text)
                  or (line.text[line.text.rfind("."):].casefold()
                      if 0 < line.text.rfind(".") < len(line.text) - 1 else "")
                  not in grammar["image_suffixes"]):
                block.problem("unsafe_reference")
            else:
                block.image_entries += 1
        if block is not None:
            yield block.finish()
        elif first_ordinal == 1:
            yield ImageIndexRecord("dii", "file_failure", first_ordinal, 1, cells=None,
                                   diagnostics=("missing_records",))
    except LoadFileError as error:
        if block is not None:
            block.problem(error.code)
            block.unavailable_raw()
            yield block.finish()
        yield ImageIndexRecord("dii", "file_failure", ordinal + 1, error.line,
                               cells=None, diagnostics=(error.code,))


def iter_image_index_records(lines, *, source_format, first_ordinal=1, expect_preamble=True):
    """Consume supplied physical lines into bounded, source-cited raw structure.

    The input is an iterable of public LoadFileLine or ImageIndexLine records. Admission
    supplies its strict decoder; a later parser may supply a resumable stream.
    Rejected known DII blocks recover only at a fresh valid @T line. An unknown
    command ends interpretation with a coverage failure, never a successful tail.
    A resume caller supplies a previously validated logical-boundary stream,
    the next positive ordinal, and expect_preamble=False for DII block resumes.
    An empty stream after a completed record is a completed resume, not a new
    empty-file failure; an initial empty stream remains a missing-record error.
    """
    if type(first_ordinal) is not int or first_ordinal < 1 or type(expect_preamble) is not bool:
        raise ValueError("The structural stream requires a positive ordinal and explicit preamble policy.")
    if source_format == "dii":
        yield from _iter_dii_records(lines, first_ordinal=first_ordinal, expect_preamble=expect_preamble)
    elif source_format == "lfp":
        ordinal = first_ordinal - 1
        try:
            for line in lines:
                line_error = getattr(line, "diagnostic", "")
                if line_error or line.text is None:
                    code = line_error or "invalid_format"
                    if (getattr(line, "boundary_complete", False)
                            and code in LFP_GRAMMAR["recoverable_line_errors"]):
                        ordinal += 1
                        yield ImageIndexRecord("lfp", "rejected", ordinal, line.line, cells=None,
                                               diagnostics=(code,), next_line=line.line + 1)
                        continue
                    yield ImageIndexRecord("lfp", "file_failure", ordinal + 1, line.line,
                                           cells=None, diagnostics=(code,))
                    return
                ordinal += 1
                yield _lfp_record(line, ordinal)
            if first_ordinal == 1 and ordinal < first_ordinal:
                yield ImageIndexRecord("lfp", "file_failure", first_ordinal, 1, cells=None,
                                       diagnostics=("missing_records",))
        except LoadFileError as error:
            yield ImageIndexRecord("lfp", "file_failure", ordinal + 1, error.line,
                                   cells=None, diagnostics=(error.code,))
    else:
        yield ImageIndexRecord("", "file_failure", 1, 1, cells=None, diagnostics=("invalid_format",))


def _check_image_records(path):
    classification = classify_image_load_file(path)
    if classification.outcome != "recognized":
        raise LoadFileAdmissionError(classification.code, classification.line, format_group="LFP/DII")
    lines = iter_load_file_lines(path)
    records = iter_image_index_records(lines, source_format=classification.format)
    try:
        for record in records:
            if record.kind != "accepted":
                raise LoadFileAdmissionError(record.diagnostics[0], record.line, format_group="LFP/DII")
    finally:
        records.close()
        lines.close()


def _explanation(diagnostic: str, location: str, format_group="DAT/OPT") -> str:
    prefix, support = ((IMAGE_LOAD_FILE_REJECTION_PREFIX, IMAGE_LOAD_FILE_SUPPORT_TEXT)
                       if format_group == "LFP/DII" else (LOAD_FILE_REJECTION_PREFIX, LOAD_FILE_SUPPORT_TEXT))
    return f"{prefix} {support} {diagnostic} Location: {location}."


class LoadFileAdmissionError(ValueError):
    """A closed-catalog explanation and a separately constructed durable reason."""

    def __init__(self, code: str, line: int | None = None, *, format_group="DAT/OPT") -> None:
        self.format_group = "LFP/DII" if format_group == "LFP/DII" else "DAT/OPT"
        catalog = IMAGE_DIAGNOSTICS if self.format_group == "LFP/DII" else DIAGNOSTICS
        self.code = code if code in catalog else "invalid_format"
        self.line = line if type(line) is int and 0 < line < 10 ** 20 else None
        diagnostic = catalog[self.code]
        location = f"line {self.line}" if self.line is not None else "file"
        self.durable_reason = f"{self.format_group} format rejected: {diagnostic} Location: {location}."
        super().__init__(_explanation(diagnostic, location, self.format_group))


def display_load_file_reason(reason: str) -> str:
    """Reconstruct only a complete recognized durable reason; preserve all others."""
    if not isinstance(reason, str) or len(reason) > 222:
        return reason
    matched = _REASON.fullmatch(reason)
    if matched:
        return _explanation(matched["diagnostic"], matched["location"])
    matched = _IMAGE_REASON.fullmatch(reason)
    return _explanation(matched["diagnostic"], matched["location"], "LFP/DII") if matched else reason


def is_stored_load_file(origin: str, media_type: str, name: str) -> bool:
    """Classify by immutable catalog identity, never by readiness or message text."""
    return (origin == "upload" and media_type == LOAD_FILE_MEDIA_TYPE
            and isinstance(name, str) and name.casefold().endswith(tuple(LOAD_FILE_SUFFIXES)))


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


class StagedUploadChanged(ValueError):
    """The staged original no longer matches the upload's verified bytes."""


def _verify_staged_descriptor(path, descriptor, expected_identity, expected_digest, maximum_bytes):
    try:
        before = os.fstat(descriptor)
        if (_identity(before) != expected_identity or not stat.S_ISREG(before.st_mode)
                or not 0 < before.st_size <= maximum_bytes
                or _identity(path.stat(follow_symlinks=False)) != expected_identity):
            raise StagedUploadChanged("The staged original changed during security verification.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest, consumed = hashlib.sha256(), 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - consumed))
            if not block:
                break
            consumed += len(block)
            if consumed > maximum_bytes:
                raise StagedUploadChanged("The staged original exceeded its admitted byte limit.")
            digest.update(block)
        if (consumed != before.st_size or digest.hexdigest() != expected_digest
                or _identity(os.fstat(descriptor)) != expected_identity
                or _identity(path.stat(follow_symlinks=False)) != expected_identity):
            raise StagedUploadChanged("The staged original changed during security verification.")
    except OSError as error:
        raise StagedUploadChanged("The staged original could not be verified completely.") from error


def scan_verified_upload(path, *, expected_digest: str, maximum_bytes: int, scan):
    """Scan once while binding before/after integrity to the caller's digest.

    The scanner receives a pathname under a held parent-directory descriptor.
    The app PID in that procfs path also works for scanners in child processes
    which close inherited descriptors. The final component remains a regular
    file, so scanner no-follow opens retain their protection. Returned results
    and exceptions retain their normal meaning. Every successful return requires
    the held regular-file descriptor and its pathname to have the same complete
    bytes and identity before and after scanning. Hashing uses bounded reads.
    The caller holds the staging mutation guard and applies the scan verdict.
    """
    if (not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
            or type(maximum_bytes) is not int or maximum_bytes < 1):
        raise StagedUploadChanged("The upload verification parameters are invalid.")
    path = Path(path)
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise StagedUploadChanged("The staged original is not a regular file.")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise StagedUploadChanged("The staged original could not be opened safely.") from error
    parent_descriptor = None
    try:
        expected_identity = _identity(before)
        _verify_staged_descriptor(path, descriptor, expected_identity, expected_digest, maximum_bytes)
        try:
            parent_before = path.parent.stat(follow_symlinks=False)
            parent_descriptor = os.open(path.parent,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_DIRECTORY)
            parent_opened = os.fstat(parent_descriptor)
            if (not stat.S_ISDIR(parent_before.st_mode) or not stat.S_ISDIR(parent_opened.st_mode)
                    or (parent_before.st_dev, parent_before.st_ino) != (parent_opened.st_dev, parent_opened.st_ino)):
                raise StagedUploadChanged("The staged original's directory changed during verification.")
            scan_path = Path(f"/proc/{os.getpid()}/fd/{parent_descriptor}") / path.name
            if _identity(scan_path.stat(follow_symlinks=False)) != expected_identity:
                raise StagedUploadChanged("The staged original's scan path changed during verification.")
        except OSError as error:
            raise StagedUploadChanged("The staged original's scan path could not be pinned safely.") from error
        result = scan(scan_path)
        _verify_staged_descriptor(path, descriptor, expected_identity, expected_digest, maximum_bytes)
        return result
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        os.close(descriptor)


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
    Decoder reopens require Linux procfs descriptor paths; unavailable procfs
    fails closed with read_error rather than validating an unbound pathname.
    """
    suffix = suffix.casefold()
    if suffix not in LOAD_FILE_SUFFIXES:
        raise LoadFileAdmissionError("invalid_format")
    path = Path(path)
    format_group = "LFP/DII" if suffix in {".lfp", ".dii"} else "DAT/OPT"
    descriptor = None
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise LoadFileAdmissionError("unsafe_file", format_group=format_group)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise LoadFileAdmissionError("changed_file", format_group=format_group)
        if os.read(descriptor, 8).startswith(_BINARY_SIGNATURES):
            raise LoadFileAdmissionError("binary_signature", format_group=format_group)
        # Bind every decoder reopen to the descriptor already checked above.
        # Renaming a parent directory cannot make a different file validate in
        # place of the held staged original. Unavailable proc access fails closed.
        bound = Path(f"/proc/self/fd/{descriptor}")
        if _identity(bound.stat()) != _identity(opened):
            raise LoadFileAdmissionError("changed_file", format_group=format_group)
        if format_group == "LFP/DII":
            _check_image_records(bound)
        else:
            _check_records(bound, suffix)
        if (_identity(os.fstat(descriptor)) != _identity(opened)
                or _identity(path.stat(follow_symlinks=False)) != _identity(opened)):
            raise LoadFileAdmissionError("changed_file", format_group=format_group)
    except OSError:
        raise LoadFileAdmissionError("read_error", format_group=format_group) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
