"""Interpret supplied DII document facts without constructing page records."""
from case_intelligence.source_admission import DII_GRAMMAR, iter_image_index_records

from .records import DiiDocument, FindingSink, RecordEvent, publish_event
from .stream import DecodedLines


def document_facts(record):
    """Use the shared tag meanings, preserving omission and exact literals."""
    tags = DII_GRAMMAR["scalar_tags"]
    return {tags[tag]: value for tag, value in zip(record.headers, record.cells or ()) if tag in tags}


def parse_dii(path, *, findings=None, on_event=None, cursor=None):
    """Yield document-only metadata and distinct declared/observed counts.

    Count disagreement and unsupported parent assertions are cited advisory
    findings on structurally accepted source facts. They never create page
    labels, synthetic document endpoints, or a second family authority.
    """
    sink = FindingSink(findings, path)
    lines = DecodedLines(path, source_format="dii", cursor=cursor)
    records = iter_image_index_records(lines, source_format="dii", first_ordinal=lines.first_ordinal,
                                       expect_preamble=cursor is None)
    try:
        for record in records:
            facts = document_facts(record) if record.kind == "accepted" else {}
            diagnostics = list(record.diagnostics)
            declared = facts.get("declared_count")
            if declared is not None and int(declared) != record.observed_image_entries:
                diagnostics.append("page_count_mismatch")
            if "parent_identifier" in facts:
                diagnostics.append("unsupported_parent_relationship")
            optional = record.optional_values + tuple(facts.items())
            event = RecordEvent(record.kind, "document", "dii", record.ordinal, record.line,
                record.headers, record.cells, tuple(diagnostics), optional, lines.record_cursor(record))
            publish_event(event, on_event, sink)
            if record.kind == "accepted":
                yield DiiDocument(facts["document_begin"], declared, record.observed_image_entries,
                    str(path), record.line, record.ordinal, record.headers, record.cells,
                    optional, tuple(diagnostics))
    finally:
        records.close()
        lines.close()
