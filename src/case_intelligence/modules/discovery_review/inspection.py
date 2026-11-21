"""Bounded record previews of a single scanned, structurally validated load file."""
from collections import Counter
import hashlib
from itertools import islice
import unicodedata

from case_intelligence.source_image_load_files import detect_image_load_file, parse_dii, parse_lfp
from case_intelligence.source_load_files import DAT_INTAKE_ROLES, DAT_TEXT_HEADER_KEYS, LoadFileError, parse_dat, parse_opt

from .fields import MAX_HEADER_CHARS, load_definition, normalize_header
from .production.matching import DatProfileError, profile_positions
from .reference_qc import InvalidInventory, ReferenceQC, SelectionInventory


SAMPLE_LIMIT = 20
DISPLAY_WIDTH = 24
DISPLAY_LENGTH = 200
REFERENCE_LIMIT = 8
PATH_LENGTH = 1024


def _mapping_choices(headers, definition):
    offered = [header for header in headers if header.strip() and len(header) <= MAX_HEADER_CHARS
               and not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in header)]
    aliases = {"native_path": definition.path_headers, "text_path": DAT_TEXT_HEADER_KEYS}
    for role in DAT_INTAKE_ROLES - aliases.keys():
        field = definition.by_key.get("document_date" if role == "date" else role)
        if field:
            aliases[role] = {normalize_header(alias) for alias in field.headers}
    choices = {}
    for header in offered:
        key = normalize_header(header)
        destinations = [role for role, names in aliases.items() if key in names]
        if len(destinations) == 1:
            choices[header] = destinations[0]
    counts = Counter(choices.values())
    return {"headers": offered, "header_count": len(headers),
            "suggestions": {h: role for h, role in choices.items() if counts[role] == 1}}


def _reference_roles(headers, definition, fields):
    try:
        assigned = dict(profile_positions(headers, fields))
    except DatProfileError as error:
        raise InvalidInventory("A selected DAT heading is missing or ambiguous in this index.") from error
    overrides = set(assigned.values())
    path_roles = {"native_path": "native", "text_path": "text"}
    for position, header in enumerate(headers):
        role = assigned.get(position)
        if role is None and len(header) <= MAX_HEADER_CHARS:
            key = normalize_header(header)
            if "native_path" not in overrides and key in definition.path_headers:
                role = "native_path"
            elif "text_path" not in overrides and key in DAT_TEXT_HEADER_KEYS:
                role = "text_path"
        if role in path_roles:
            yield position, path_roles[role]


def inspect_load_file(path, suffix, *, inventory=None, verified_sha256=None):
    """Inspect one authorized staged file; embedded path strings stay inert."""
    source_format = suffix.casefold().removeprefix(".")
    qc, profile = None, None
    if inventory is not None:
        selected = inventory if isinstance(inventory, SelectionInventory) else SelectionInventory(inventory)
        if not selected.index_path.casefold().endswith("." + source_format):
            raise InvalidInventory("The selected index does not have the requested filename extension.")
        profile = selected.dat_profile
        if profile:
            if verified_sha256 is None:
                with open(path, "rb") as staged:
                    verified_sha256 = hashlib.file_digest(staged, "sha256").hexdigest()
            if verified_sha256 != profile["sha256"]:
                raise InvalidInventory("The inspected DAT no longer matches the proposed mapping.")
        qc = ReferenceQC(selected)
    if source_format in {"dii", "lfp"}:
        detected = detect_image_load_file(path)
        if detected.outcome != "recognized":
            raise LoadFileError(detected.code or "invalid_format", path, detected.line or 1)
        source_format = detected.format
    parsers = {"dat": parse_dat, "opt": parse_opt, "lfp": parse_lfp, "dii": parse_dii}
    if source_format not in parsers:
        raise LoadFileError("invalid_format", path, 1)

    definition = load_definition()
    opt_labels = tuple(column.label for column in definition.opt_columns)
    roles = []
    rows, references, diagnostics = [], [], Counter()
    record_count, references_truncated, encoding = 0, False, ""

    mapping = {}

    def reference(role, value, ordinal, line, directory=""):
        nonlocal references_truncated
        status = qc.observe(role, directory, value, ordinal, line) if qc else None
        if ordinal > SAMPLE_LIMIT:
            return
        if not value:
            return
        length = len(value) + (len(directory) + 1 if directory else 0)
        if len(references) >= REFERENCE_LIMIT or length > PATH_LENGTH:
            references_truncated = True
        else:
            sample = {"role": role, "path": directory + "/" + value if directory else value}
            if qc:
                sample["status"] = status
            references.append(sample)

    def dii_image(ordinal, directory, filename):
        reference("image", filename, ordinal, None, directory)

    def receive(event):
        nonlocal record_count, encoding, references, references_truncated
        if event.kind in {"file_failure", "rejected"} or (event.kind == "header" and event.diagnostics):
            raise LoadFileError(event.diagnostics[0] if event.diagnostics else "invalid_format", path, event.line)
        if not encoding and event.next_cursor:
            encoding = event.next_cursor["encoding"]
        if event.kind == "header":
            mapping.update(_mapping_choices(event.headers or (), definition))
            roles.extend(_reference_roles(event.headers or (), definition, profile["fields"] if profile else {}))
            return
        record_count += 1
        diagnostics.update(event.diagnostics)
        if not qc and len(rows) >= SAMPLE_LIMIT:
            return
        cells = event.cells or ()
        if source_format == "dat":
            for position, role in roles:
                if cells[position]:
                    reference(role, cells[position], event.ordinal, event.line)
        elif source_format == "opt":
            reference("image", cells[2], event.ordinal, event.line)
        elif source_format == "lfp":
            reference("image", dict(event.optional_values)["path"], event.ordinal, event.line)
        elif qc:
            qc.cite_dii_block(event.line)
        if len(rows) >= SAMPLE_LIMIT:
            return
        labels = opt_labels if source_format == "opt" else event.headers or ()
        pairs = list(islice(zip(labels, cells), DISPLAY_WIDTH))
        fields_truncated = len(cells) > len(pairs) or any(
            len(label) > DISPLAY_LENGTH or len(value) > DISPLAY_LENGTH for label, value in pairs)
        rows.append({"ordinal": event.ordinal, "line": event.line,
                     "fields": [{"label": label[:DISPLAY_LENGTH], "value": value[:DISPLAY_LENGTH]}
                                for label, value in pairs],
                     "fields_truncated": fields_truncated, "references": references,
                     "references_truncated": references_truncated})
        references, references_truncated = [], False

    options = {"on_image_name": dii_image} if source_format == "dii" else {}
    for _ in parsers[source_format](path, on_event=receive, **options):
        pass
    if not record_count:
        raise LoadFileError("missing_records", path, 1)
    report = {**({"mapping": mapping} if source_format == "dat" else {}),
            "format": source_format, "encoding": encoding,
            "record_kind": "document" if source_format in {"dat", "dii"} else "page",
            "record_count": record_count, "diagnostic_counts": dict(diagnostics),
            "rows": rows, "sample_limit": SAMPLE_LIMIT}
    if qc:
        report["reference_qc"] = qc.report()
        if source_format == "dat":
            report["reference_qc"]["dat_mapping"] = "custom" if profile else "standard"
    return report
