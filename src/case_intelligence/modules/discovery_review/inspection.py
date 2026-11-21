"""Bounded record previews of a single scanned, structurally validated load file."""
from collections import Counter
from itertools import islice

from case_intelligence.source_image_load_files import detect_image_load_file, parse_dii, parse_lfp
from case_intelligence.source_load_files import LoadFileError, parse_dat, parse_opt, propose_field_map

from .fields import load_definition, normalize_header


SAMPLE_LIMIT = 20
DISPLAY_WIDTH = 24
DISPLAY_LENGTH = 200
REFERENCE_LIMIT = 8
PATH_LENGTH = 1024


def inspect_load_file(path, suffix):
    """Inspect one authorized staged file; embedded path strings stay inert."""
    source_format = suffix.casefold().removeprefix(".")
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

    def reference(role, value, directory=""):
        nonlocal references_truncated
        if not value:
            return
        length = len(value) + (len(directory) + 1 if directory else 0)
        if len(references) >= REFERENCE_LIMIT or length > PATH_LENGTH:
            references_truncated = True
        else:
            references.append({"role": role, "path": directory + "/" + value if directory else value})

    def dii_image(ordinal, directory, filename):
        if ordinal <= SAMPLE_LIMIT:
            reference("image", filename, directory)

    def receive(event):
        nonlocal record_count, encoding, references, references_truncated
        if event.kind in {"file_failure", "rejected"} or (event.kind == "header" and event.diagnostics):
            raise LoadFileError(event.diagnostics[0] if event.diagnostics else "invalid_format", path, event.line)
        if not encoding and event.next_cursor:
            encoding = event.next_cursor["encoding"]
        if event.kind == "header":
            for position, header in enumerate(event.headers or ()):
                key = normalize_header(header)
                if key in definition.path_headers:
                    roles.append((position, "native"))
                elif "text_path" in propose_field_map((key.upper(),)).values():
                    roles.append((position, "text"))
            return
        record_count += 1
        diagnostics.update(event.diagnostics)
        if len(rows) >= SAMPLE_LIMIT:
            return
        cells = event.cells or ()
        if source_format == "dat":
            for position, role in roles:
                reference(role, cells[position])
        elif source_format == "opt":
            reference("image", cells[2])
        elif source_format == "lfp":
            reference("image", dict(event.optional_values)["path"])
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
    return {"format": source_format, "encoding": encoding,
            "record_kind": "document" if source_format in {"dat", "dii"} else "page",
            "record_count": record_count, "diagnostic_counts": dict(diagnostics),
            "rows": rows, "sample_limit": SAMPLE_LIMIT}
