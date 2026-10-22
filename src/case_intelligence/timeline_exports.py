"""Review exports retaining every original citation and its date basis."""
import csv
import html
import io
import json
import re
import unicodedata

from .derived_text import presentation_text
from .work_product_exports import _csv_safe


def _export_presentation_text(text: str) -> str:
    text = presentation_text(text)
    return ''.join(' ' if unicodedata.category(c) == 'Cf' or c in '\u2028\u2029' else c for c in text)


def safe(value):
    # Use the notebook presentation/control policy before inspecting formula
    # prefixes. Format characters and Unicode line separators cannot hide them.
    text = _export_presentation_text(str(value or ''))
    text = _csv_safe(text)
    probe = unicodedata.normalize('NFKC', text).replace('\u2212', '-').lstrip()
    return "'" + text if probe.startswith(('=', '+', '-', '@', '|', '%')) else text


def _md_escape(text: str) -> str:
    value = html.escape(_export_presentation_text(text), quote=False)
    for character in ('\\', '`', '*', '_', '[', ']', '#', '|', '!', '~', '='):
        value = value.replace(character, '\\' + character)
    value = re.sub(r'^([-+]|[0-9]{1,9}[.)])',
                   lambda match: match[0][:-1] + '\\' + match[0][-1], value)
    return re.sub(r'^[ \t]+', lambda match: ''.join(f'&#{ord(c)};' for c in match[0]), value)


def md_inline(text: object) -> str:
    value = re.sub(r'\r\n|[\n\r\u2028\u2029]', ' ', '' if text is None else str(text))
    return _md_escape(_export_presentation_text(value).rstrip())


def md_block_quoted(text: object) -> str:
    value = re.sub(r'\r\n|[\r\u2028\u2029]', '\n', '' if text is None else str(text))
    return '\n'.join('> ' + _md_escape(line) for line in value.split('\n'))


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
    lines = ['# Draft timeline', '', md_inline(payload['notice']), '', md_inline(payload['coverage_notice']),
             '', 'Capped: ' + md_inline(str(payload['capped'])), '']
    for record in payload['coverage_records']:
        lines += ['Source coverage: ' + md_inline(record['source_name']) + ' (' + md_inline(record['source_id']) +
                  ') — ' + md_inline(record['status']), '']
    for entry in entries:
        lines += ['## ' + md_inline(entry['date_label']), '', md_block_quoted(entry['description']), '',
                  'Date uncertainty: ' + md_inline(entry['uncertainty']),
                  'Conflict group: ' + md_inline(str(entry['conflict_group'] or 'None'))]
        for c in entry['citations']:
            lines += ['', 'Source: ' + md_inline(c['source_id']) + ' · ' + md_inline(c['document_title']),
                      'Locator: ' + md_inline(json.dumps(c['locator'])), '', md_block_quoted(c['excerpt'])]
    return '\n'.join(lines) + '\n'
