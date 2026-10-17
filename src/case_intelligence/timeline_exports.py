"""Review exports retaining every original citation and its date basis."""
import csv
import io
import json


def export_timeline(payload, format_name):
    if format_name == 'json':
        return json.dumps(payload, ensure_ascii=False, indent=2)
    entries = payload['entries'] + payload['undated']
    if format_name == 'csv':
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(('date_label', 'precision', 'uncertainty', 'description', 'source', 'locator', 'excerpt', 'conflict_group'))
        def safe(value):
            text = str(value or '')
            return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text
        for entry in entries:
            for citation in entry['citations']:
                writer.writerow([safe(v) for v in (entry['date_label'], entry['precision'], entry['uncertainty'], entry['description'],
                    citation['source_id'] + ' · ' + citation['document_title'], json.dumps(citation['locator']), citation['excerpt'], entry['conflict_group'])])
        return stream.getvalue()
    def escape(text):
        import html
        value = html.escape(str(text))
        for character in ('\\', '`', '*', '_', '[', ']', '#', '|', '!', '~'):
            value = value.replace(character, '\\' + character)
        return value

    lines = ['# Draft timeline', '', payload['notice'], '', 'Capped: ' + str(payload['capped']), '']
    for entry in entries:
        lines += ['## ' + escape(entry['date_label']), '', escape(entry['description']), '',
                  'Date uncertainty: ' + escape(entry['uncertainty']), 'Conflict group: ' + str(entry['conflict_group'] or 'None')]
        for c in entry['citations']:
            lines += ['', 'Source: ' + escape(c['source_id'] + ' · ' + c['document_title']),
                      'Locator: ' + escape(json.dumps(c['locator'])), '', '> ' + escape(c['excerpt']).replace('\n', '\n> ')]
    return '\n'.join(lines) + '\n'
