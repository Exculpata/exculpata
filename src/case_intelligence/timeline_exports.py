"""Review exports retaining every original citation and its date basis."""
import csv
import io
import json
import unicodedata

from .derived_text import presentation_text
from .work_product_exports import _csv_safe


def safe(value):
    # Use the notebook presentation/control policy before inspecting formula
    # prefixes. Format characters and Unicode line separators cannot hide them.
    text = presentation_text(str(value or ''))
    text = ''.join(' ' if unicodedata.category(c) == 'Cf' or c in '\u2028\u2029' else c for c in text)
    text = _csv_safe(text)
    probe = unicodedata.normalize('NFKC', text).replace('\u2212', '-').lstrip()
    return "'" + text if probe.startswith(('=', '+', '-', '@', '|', '%')) else text


def export_timeline(payload, format_name):
    if format_name == 'json':
        return json.dumps(payload, ensure_ascii=False, indent=2)
    entries = payload['entries'] + payload['undated']
    if format_name == 'csv':
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(('date_label', 'precision', 'uncertainty', 'description', 'source', 'locator', 'excerpt', 'conflict_group',
                         'row_type', 'notice', 'coverage_notice', 'capped', 'unit_count', 'total_unit_count',
                         'coverage_status', 'format'))
        # A metadata row preserves disclosure even when no date entries exist.
        writer.writerow([''] * 8 + ['metadata', safe(payload['notice']), safe(payload['coverage_notice']),
                                   str(payload['capped']), payload['unit_count'], payload['total_unit_count'], '', payload['format']])
        for record in payload['coverage_records']:
            writer.writerow([''] * 4 + [safe(record['source_id'] + ' · ' + record['source_name'])] + [''] * 3 +
                            ['coverage', '', safe(record['source_name']), '', '', '', record['status'], payload['format']])
        for entry in entries:
            for citation in entry['citations']:
                writer.writerow([safe(v) for v in (entry['date_label'], entry['precision'], entry['uncertainty'], entry['description'],
                    citation['source_id'] + ' · ' + citation['document_title'], json.dumps(citation['locator']), citation['excerpt'], entry['conflict_group'])] + ['citation'] + [''] * 6 + [payload['format']])
        return stream.getvalue()
    def escape(text):
        import html
        value = html.escape(presentation_text(str(text)))
        for character in ('\\', '`', '*', '_', '[', ']', '#', '|', '!', '~', '='):
            value = value.replace(character, '\\' + character)
        return value

    lines = ['# Draft timeline', '', escape(payload['notice']), '', escape(payload['coverage_notice']), '', 'Capped: ' + str(payload['capped']), '']
    for record in payload['coverage_records']:
        lines += ['Source coverage: ' + escape(record['source_name']) + ' (' + escape(record['source_id']) +
                  ') — ' + escape(record['status']), '']
    for entry in entries:
        lines += ['## ' + escape(entry['date_label']), '', escape(entry['description']), '',
                  'Date uncertainty: ' + escape(entry['uncertainty']), 'Conflict group: ' + str(entry['conflict_group'] or 'None')]
        for c in entry['citations']:
            lines += ['', 'Source: ' + escape(c['source_id'] + ' · ' + c['document_title']),
                      'Locator: ' + escape(json.dumps(c['locator'])), '', '> ' + escape(c['excerpt']).replace('\n', '\n> ')]
    return '\n'.join(lines) + '\n'
