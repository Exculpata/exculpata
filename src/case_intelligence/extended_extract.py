"""Deterministic, bounded extractors for images, email, and spreadsheets."""
from __future__ import annotations

import csv
import os
import re
import subprocess
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

MAX_EXTRACTED_CHARS = 5_000_000
MAX_IMAGE_FRAMES = 100
MAX_IMAGE_DIMENSION = 30_000
MAX_IMAGE_PIXELS = 100_000_000
MAX_EMAIL_PARTS = 500
MAX_EMAIL_BODY_BYTES = 10 * 1024 * 1024
EMAIL_COVERAGE_NOTICE = (
    "Email searches use extracted message text. Attachment contents are not fully "
    "searched; review any attachments separately."
)
MAX_TEXT_CONTAINER_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_EXPANDED_BYTES = 150 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 40 * 1024 * 1024
MAX_SHEETS = 200
MAX_ROWS = 100_000
MAX_CELLS = 2_000_000
MAX_COLUMNS = 2_000
ROWS_PER_SECTION = 100


@dataclass(frozen=True)
class ExtractedSection:
    number: int
    label: str
    text: str


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag.casefold() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def text(self) -> str:
        return " ".join(self.parts).replace(" \n ", "\n").strip()


def _bounded_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > MAX_EXTRACTED_CHARS:
        raise ValueError("The extracted content exceeds the supported review limit.")
    return normalized


def extract_image(path: Path, media_type: str) -> tuple[tuple[ExtractedSection, ...], int]:
    expected = {
        "image/jpeg": {"JPEG", "JPG"},
        "image/png": {"PNG"},
        "image/tiff": {"TIFF", "TIF"},
    }.get(media_type)
    if expected is None:
        raise ValueError("That image format is not supported.")
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MAGICK_MEMORY_LIMIT": "256MiB",
        "MAGICK_MAP_LIMIT": "512MiB",
        "MAGICK_DISK_LIMIT": "1GiB",
        "MAGICK_THREAD_LIMIT": "1",
    }
    try:
        identified = subprocess.run(
            ["/usr/bin/identify", "-ping", "-format", "%m\t%w\t%h\\n", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=20,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("The image could not be inspected safely.") from exc
    if identified.returncode != 0 or len(identified.stdout) > 64 * 1024:
        raise ValueError("That image is damaged or malformed.")
    frames = []
    pixels = 0
    try:
        for line in identified.stdout.decode("ascii").splitlines():
            image_format, width_text, height_text = line.split("\t")
            width, height = int(width_text), int(height_text)
            if image_format.upper() not in expected:
                raise ValueError
            if not 0 < width <= MAX_IMAGE_DIMENSION or not 0 < height <= MAX_IMAGE_DIMENSION:
                raise ValueError
            pixels += width * height
            frames.append((width, height))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("That image does not match its filename or is too large.") from exc
    if not frames or len(frames) > MAX_IMAGE_FRAMES or pixels > MAX_IMAGE_PIXELS:
        raise ValueError("That image contains too many pixels or frames for safe review.")
    language = os.getenv("CASE_INTELLIGENCE_OCR_LANGUAGE", "eng").strip()
    if not re.fullmatch(r"[A-Za-z0-9_+-]{1,40}", language):
        language = "eng"
    try:
        recognized = subprocess.run(
            ["/usr/bin/tesseract", str(path), "stdout", "-l", language, "--psm", "3"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=120,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "OMP_THREAD_LIMIT": "1",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Image text recognition did not finish safely.") from exc
    if recognized.returncode != 0 or len(recognized.stdout) > MAX_EXTRACTED_CHARS * 4:
        raise ValueError("Image text recognition could not read this image.")
    text = _bounded_text(recognized.stdout.decode("utf-8", errors="replace"))
    sections = (ExtractedSection(1, "Recognized image text", text),) if text else ()
    return sections, len(frames)


def _decoded_email_part(part) -> str:
    raw = part.get_payload(decode=True) or b""
    if part.defects:
        raise ValueError("That email is damaged or malformed.")
    if len(raw) > MAX_EMAIL_BODY_BYTES:
        raise ValueError("The email body exceeds the supported review limit.")
    charset = (part.get_content_charset() or "utf-8").casefold()
    if charset not in {"utf-8", "us-ascii", "ascii", "iso-8859-1", "windows-1252"}:
        charset = "utf-8"
    return raw.decode(charset, errors="replace")


def _email_content_id(value: object) -> str:
    """Remove surrounding RFC comment/folding whitespace without changing an ID."""
    output: list[str] = []
    depth = 0
    escaped = False
    inside = quoted = literal = False
    for char in str(value):
        if depth:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            continue
        if not inside:
            if char.isspace():
                continue
            if char == "(":
                depth = 1
                continue
            if char == "<":
                inside = True
            output.append(char)
            continue
        output.append(char)
        if escaped:
            escaped = False
        elif char == "\\" and (quoted or literal):
            escaped = True
        elif char == '"' and not literal:
            quoted = not quoted
        elif char == "[" and not quoted:
            literal = True
        elif char == "]" and not quoted:
            literal = False
        elif char == ">" and not quoted and not literal:
            inside = False
    if depth or inside:
        raise ValueError("That email has a missing or ambiguous message body.")
    return "".join(output)


def extract_email(path: Path) -> tuple[ExtractedSection, ...]:
    if path.stat().st_size > MAX_TEXT_CONTAINER_BYTES:
        raise ValueError("The email exceeds the supported review limit.")
    raw = path.read_bytes()
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise ValueError("That email is damaged or malformed.") from exc
    pending = [message]
    part_count = 0
    while pending:
        part = pending.pop()
        part_count += 1
        if part_count > MAX_EMAIL_PARTS:
            raise ValueError("That email contains too many MIME parts.")
        if part.defects:
            raise ValueError("That email is damaged or malformed.")
        # Structured MIME headers are parsed lazily and keep their own defects.
        # A malformed Content-Type can otherwise fall back to text/plain and
        # turn an attached message into apparent parent body text.
        for name in ("Content-Type", "Content-Disposition", "Content-Transfer-Encoding", "Content-ID"):
            headers = part.get_all(name, [])
            if len(headers) > 1 or any(header.defects for header in headers):
                raise ValueError("That email is damaged or malformed.")
        if part.is_multipart():
            pending.extend(part.get_payload())
    header_lines = []
    for label in ("From", "To", "Cc", "Date", "Subject", "Message-ID"):
        value = " ".join(str(message.get(label, "")).split())
        if value:
            header_lines.append(f"{label}: {value[:20_000]}")
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[str] = []
    used = 0
    # Walk the body tree explicitly. A generic MIME walk enters attached
    # messages and multipart attachments, mixing their text into parent evidence.
    pending = [(message, "body")]
    while pending:
        part, role = pending.pop()
        disposition = (part.get_content_disposition() or "").casefold()
        supplied_filename = part.get_filename()
        filename = " ".join((supplied_filename or "").split())[:240]
        media_type = part.get_content_type()
        report_data = role == "report_data"
        attachment = role == "resource" or (part is not message and (
            ((disposition == "attachment" or supplied_filename is not None) and role != "related_root") or
            (part.get_content_maintype() == "message" and not report_data) or
            (not report_data and not part.is_multipart()
                and media_type not in {"text/plain", "text/html"})
        ))
        if attachment:
            attachments.append(
                f"Attachment: {filename or 'unnamed'} ({media_type})"
            )
            continue
        if report_data:
            # The matching second multipart/report part is machine metadata,
            # not an unnamed attachment. Keep the human-readable report body;
            # machine fields remain outside the current text extraction scope.
            continue
        if part.is_multipart():
            children = part.get_payload()
            if media_type == "multipart/related":
                start_id = part.get_param("start")
                normalized_start = _email_content_id(start_id) if start_id is not None else None
                if normalized_start is not None and not normalized_start:
                    raise ValueError("That email has a missing or ambiguous message body.")
                roots = [child for child in children
                    if _email_content_id(child.get("Content-ID", "")) == normalized_start] if normalized_start is not None else children[:1]
                if len(roots) != 1:
                    raise ValueError("That email has a missing or ambiguous message body.")
                pending.extend((child, "related_root" if child is roots[0] else "resource")
                    for child in reversed(children))
            elif media_type == "multipart/report":
                report_type = str(part.get_param("report-type") or "").strip().casefold()
                pending.extend((child, "report_data" if index == 1
                    and child.get_content_maintype() == "message"
                    and child.get_content_subtype().casefold() == report_type else "body")
                    for index, child in reversed(tuple(enumerate(children))))
            else:
                pending.extend((child, "body") for child in reversed(children))
            continue
        if media_type not in {"text/plain", "text/html"}:
            continue
        value = _decoded_email_part(part)
        used += len(value.encode("utf-8"))
        if used > MAX_EMAIL_BODY_BYTES:
            raise ValueError("The email body exceeds the supported review limit.")
        if media_type == "text/plain":
            plain.append(value)
        else:
            parser = _HtmlText()
            parser.feed(value)
            rich.append(parser.text())
    body = "\n\n".join(plain or rich)
    sections: list[ExtractedSection] = []
    if attachments:
        attachments.append("Attachment contents were not processed or searched. Review attachments separately.")
    metadata = "\n".join((*header_lines, *attachments)).strip()
    if metadata:
        sections.append(ExtractedSection(1, "Email headers", _bounded_text(metadata)))
    lines = body.splitlines()
    for start in range(0, len(lines), 100):
        text = _bounded_text("\n".join(lines[start : start + 100]))
        if text:
            sections.append(
                ExtractedSection(
                    len(sections) + 1,
                    f"Email body lines {start + 1}–{min(start + 100, len(lines))}",
                    text,
                )
            )
    if not sections:
        raise ValueError("The email contains no reviewable headers or body text.")
    return tuple(sections)


def _cell_text(value: object) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) > 100_000:
        raise ValueError("A spreadsheet cell exceeds the supported review limit.")
    return text


def _tabular_sections(rows, label: str) -> tuple[ExtractedSection, ...]:
    sections: list[ExtractedSection] = []
    block: list[str] = []
    start = 1
    row_count = 0
    cell_count = 0
    total_chars = 0
    for row in rows:
        row_count += 1
        if row_count > MAX_ROWS:
            raise ValueError("The spreadsheet contains too many rows.")
        values = tuple(_cell_text(value) for value in row)
        if len(values) > MAX_COLUMNS:
            raise ValueError("The spreadsheet contains too many columns.")
        cell_count += len(values)
        if cell_count > MAX_CELLS:
            raise ValueError("The spreadsheet contains too many cells.")
        line = " | ".join(f"{index + 1}: {value}" for index, value in enumerate(values) if value)
        if line:
            total_chars += len(line)
            if total_chars > MAX_EXTRACTED_CHARS:
                raise ValueError("The spreadsheet text exceeds the supported review limit.")
            block.append(f"Row {row_count}: {line}")
        if row_count % ROWS_PER_SECTION == 0:
            if block:
                sections.append(
                    ExtractedSection(
                        len(sections) + 1,
                        f"{label}, rows {start}–{row_count}",
                        "\n".join(block),
                    )
                )
            block = []
            start = row_count + 1
    if block:
        sections.append(
            ExtractedSection(
                len(sections) + 1,
                f"{label}, rows {start}–{row_count}",
                "\n".join(block),
            )
        )
    if not sections:
        raise ValueError("The spreadsheet contains no reviewable cell values.")
    return tuple(sections)


def extract_delimited(path: Path, delimiter: str) -> tuple[ExtractedSection, ...]:
    if path.stat().st_size > MAX_TEXT_CONTAINER_BYTES:
        raise ValueError("The spreadsheet exceeds the supported review limit.")
    raw = path.read_bytes()
    if b"\x00" in raw or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("Delimited spreadsheets must be valid UTF-8 text.")
    try:
        value = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Delimited spreadsheets must be valid UTF-8 text.") from exc
    try:
        rows = csv.reader(StringIO(value), delimiter=delimiter, strict=True)
        return _tabular_sections(rows, "Delimited table")
    except csv.Error as exc:
        raise ValueError("That delimited spreadsheet is malformed.") from exc


def _safe_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("The spreadsheet archive contains too many entries.")
    total = 0
    result: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("The spreadsheet archive contains an unsafe path.")
        if info.flag_bits & 0x1 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError("The spreadsheet archive is encrypted or too large.")
        total += info.file_size
        if total > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError("The spreadsheet archive expands beyond the safe review limit.")
        result[info.filename] = info
    return result


def _xml(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], name: str):
    info = members.get(name)
    if info is None:
        raise ValueError("The spreadsheet is missing required workbook data.")
    try:
        return ElementTree.fromstring(archive.read(info))
    except (ElementTree.ParseError, OSError, RuntimeError) as exc:
        raise ValueError("That spreadsheet is damaged or malformed.") from exc


def _open_xlsx_sheets(path: Path):
    """Yield (sheet_name, [(row_cells_dict, sheet_row), ...]) directly from XLSX XML."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("That XLSX is damaged or malformed.") from exc
    with archive:
        members = _safe_archive(archive)
        if "xl/vbaProject.bin" in members:
            raise ValueError("Macro-enabled spreadsheets are not accepted.")
        namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        relationships_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        package_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
        shared: list[str] = []
        if "xl/sharedStrings.xml" in members:
            root = _xml(archive, members, "xl/sharedStrings.xml")
            for item in root.findall(f"{namespace}si"):
                shared.append(_cell_text("".join(item.itertext())))
                if len(shared) > MAX_CELLS:
                    raise ValueError("The spreadsheet contains too many shared values.")
        workbook = _xml(archive, members, "xl/workbook.xml")
        relations = _xml(archive, members, "xl/_rels/workbook.xml.rels")
        targets = {
            relation.attrib.get("Id", ""): relation.attrib.get("Target", "")
            for relation in relations.findall(f"{package_ns}Relationship")
        }
        sheets = workbook.findall(f".//{namespace}sheet")
        if not sheets or len(sheets) > MAX_SHEETS:
            raise ValueError("The spreadsheet contains no sheets or too many sheets.")
        total_rows = 0
        total_cells = 0
        parsed_sheets: list[tuple[str, list[tuple[list[tuple[str, str]], int]]]] = []
        for sheet in sheets:
            label = _cell_text(sheet.attrib.get("name", "Sheet")) or "Sheet"
            relation_id = sheet.attrib.get(f"{relationships_ns}id", "")
            target = targets.get(relation_id, "")
            target_path = PurePosixPath("xl") / target
            normalized = target.lstrip("/") if target.startswith("/") else str(target_path)
            if ".." in PurePosixPath(normalized).parts or normalized not in members:
                raise ValueError("The spreadsheet references an unsafe worksheet.")
            root = _xml(archive, members, normalized)
            sheet_rows: list[tuple[list[tuple[str, str]], int]] = []
            sheet_row = 0
            for row in root.findall(f".//{namespace}row"):
                sheet_row += 1
                total_rows += 1
                if total_rows > MAX_ROWS:
                    raise ValueError("The spreadsheet contains too many rows.")
                cells: list[tuple[str, str]] = []
                for cell in row.findall(f"{namespace}c"):
                    total_cells += 1
                    if total_cells > MAX_CELLS:
                        raise ValueError("The spreadsheet contains too many cells.")
                    coordinate = cell.attrib.get("r", f"cell-{total_cells}")
                    cell_type = cell.attrib.get("t", "")
                    formula_node = cell.find(f"{namespace}f")
                    value_node = cell.find(f"{namespace}v")
                    inline = cell.find(f"{namespace}is")
                    value = ""
                    if formula_node is not None:
                        value = "=" + _cell_text(formula_node.text)
                        if value_node is not None and value_node.text:
                            value += " [cached: " + _cell_text(value_node.text) + "]"
                    elif cell_type == "s" and value_node is not None:
                        try:
                            index_text = (value_node.text or "").strip(" \t\r\n")
                            if re.fullmatch(r"[+-]?[0-9]+", index_text) is None:
                                raise ValueError("shared index is not an ASCII decimal integer")
                            shared_index = int(index_text)
                            if not 0 <= shared_index < len(shared):
                                raise ValueError("shared index outside table")
                            value = shared[shared_index]
                        except (ValueError, IndexError) as exc:
                            raise ValueError("The spreadsheet has an invalid shared value.") from exc
                    elif cell_type == "inlineStr" and inline is not None:
                        value = _cell_text("".join(inline.itertext()))
                    elif value_node is not None:
                        value = _cell_text(value_node.text)
                    if value:
                        cells.append((coordinate, value))
                sheet_rows.append((cells, sheet_row))
            parsed_sheets.append((label, sheet_rows))
        return parsed_sheets


def extract_xlsx(path: Path) -> tuple[ExtractedSection, ...]:
    parsed_sheets = _open_xlsx_sheets(path)
    all_sections: list[ExtractedSection] = []
    total_chars = 0
    for label, sheet_rows in parsed_sheets:
        block: list[str] = []
        block_start = 1
        last_row = 0
        for cells, sheet_row in sheet_rows:
            last_row = sheet_row
            if cells:
                line = f"Row {sheet_row}: " + " | ".join(f"{coord}: {val}" for coord, val in cells)
                total_chars += len(line)
                if total_chars > MAX_EXTRACTED_CHARS:
                    raise ValueError("The spreadsheet text exceeds the supported review limit.")
                block.append(line)
            if sheet_row % ROWS_PER_SECTION == 0:
                if block:
                    all_sections.append(
                        ExtractedSection(
                            len(all_sections) + 1,
                            f"{label}, rows {block_start}–{sheet_row}",
                            "\n".join(block),
                        )
                    )
                block = []
                block_start = sheet_row + 1
        if block:
            all_sections.append(
                ExtractedSection(
                    len(all_sections) + 1,
                    f"{label}, rows {block_start}–{last_row}",
                    "\n".join(block),
                )
            )
    if not all_sections:
        raise ValueError("The spreadsheet contains no reviewable cell values.")
    return tuple(all_sections)


def extract_xlsx_structured(path: Path, *, sheet: str = "") -> tuple[str, list[str], list[tuple[dict[str, str], int]], int]:
    """Return (sheet_name, headers, [(row_dict, sheet_row), ...], header_row) directly from XLSX XML without delimiter re-parsing."""
    parsed_sheets = _open_xlsx_sheets(path)
    names = [label for label, _ in parsed_sheets]
    requested = sheet.strip()
    if requested:
        chosen = [(label, srows) for label, srows in parsed_sheets if label.casefold() == requested.casefold()]
        if not chosen:
            raise ValueError(f"Worksheet {requested!r} not found (available: {', '.join(names)}).")
        chosen_name, sheet_rows = chosen[0]
    elif len(parsed_sheets) > 1:
        raise ValueError(f"Workbook contains multiple worksheets ({', '.join(names)}); specify 'sheet' to query one worksheet.")
    else:
        chosen_name, sheet_rows = parsed_sheets[0]
    col_re = re.compile(r"^([A-Za-z]+)\d+$")
    coord_headers: list[tuple[str, str]] | None = None
    header_row = 1
    rows: list[tuple[dict[str, str], int]] = []
    for cells, sheet_row in sheet_rows:
        if not cells:
            continue
        row_cells: dict[str, str] = {}
        for raw_coord, val in cells:
            cm = col_re.match(raw_coord)
            coord = cm.group(1).upper() if cm else raw_coord
            row_cells[coord] = val
        if coord_headers is None:
            header_row = sheet_row
            coord_headers = [(c, h) for c, h in row_cells.items() if h]
            seen_h: set[str] = set()
            dupes = [h for _, h in coord_headers if h.casefold() in seen_h or seen_h.add(h.casefold())]
            if dupes:
                raise ValueError(
                    f"Table contains duplicate column header(s): {', '.join(dict.fromkeys(dupes))}; disambiguate headers before querying."
                )
            continue
        rows.append(({col: row_cells.get(c, "") for c, col in coord_headers}, sheet_row))
    return chosen_name, [h for _, h in (coord_headers or [])], rows, header_row

