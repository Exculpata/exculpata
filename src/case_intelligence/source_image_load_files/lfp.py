"""Interpret explicit single-image LFP facts from the shared narrow grammar."""
from case_intelligence.source_admission import LFP_GRAMMAR, iter_image_index_records

from .records import FindingSink, OptPage, RecordEvent, publish_event
from .stream import DecodedLines


def page_facts(record):
    """Project only explicitly supplied components; references remain strings."""
    raw = dict(zip(record.headers, record.cells or ()))
    components = dict(record.optional_values)
    filename = components["location_filename"]
    directory = components.get("location_directory")
    path = filename if directory is None else directory + "/" + filename
    volume = components.get("location_volume", "")
    if volume:
        volume = volume[len(LFP_GRAMMAR["volume_marker"]):]
    boundary = "Y" if raw["boundary"] == LFP_GRAMMAR["document_start"] else ""
    return raw["image_key"], volume, path, boundary, "", "", ""


def optional_values(record, values=None):
    raw = dict(zip(record.headers, record.cells or ()))
    pairs = list(record.optional_values)
    pairs.extend((name, raw[name]) for name in ("boundary", "offset", "location", "rotation") if name in raw)
    components = dict(record.optional_values)
    if "location_image_type" in components:
        pairs.append(("image_type", components["location_image_type"]))
    if values is not None:
        pairs.extend(zip(("page_label", "volume", "path", "document_break", "page_count"),
                         (values[0], values[1], values[2], values[3], values[6])))
    return tuple(pairs)


def parse_lfp(path, *, findings=None, on_event=None, cursor=None):
    """Yield the exact public OPT page type, with lossless uncapped raw events."""
    sink = FindingSink(findings, path)
    lines = DecodedLines(path, source_format="lfp", cursor=cursor)
    records = iter_image_index_records(lines, source_format="lfp", first_ordinal=lines.first_ordinal)
    try:
        for record in records:
            values = page_facts(record) if record.kind == "accepted" else None
            event = RecordEvent(record.kind, "page", "lfp", record.ordinal, record.line,
                record.headers, record.cells, record.diagnostics, optional_values(record, values),
                lines.record_cursor(record))
            publish_event(event, on_event, sink)
            if values is not None:
                yield OptPage(values, str(path), record.line)
    finally:
        records.close()
        lines.close()
