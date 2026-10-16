"""Bounded exports of exactly two current passages and human review state."""
import csv
import io
import json

from .review_wording import REVIEW_NOTICE
from .assertion_exports import _text

EXPORT_HEADERS = ('signature', 'scope', 'difference_type', 'note', 'status', 'reviewed_by', 'reviewed_at',
                  'left_source', 'left_locator', 'left_excerpt', 'right_source', 'right_locator', 'right_excerpt')


def export_payload(payload, format_name):
    if format_name == 'json':
        return json.dumps(dict(payload, notice=REVIEW_NOTICE), ensure_ascii=False, indent=2)
    if format_name == 'csv':
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow((*EXPORT_HEADERS, 'format', 'notice', 'dropped_candidates', 'bounded_scan',
                         'left_document_id', 'left_source_version_id', 'right_document_id', 'right_source_version_id'))
        for row in payload['candidates']:
            values = [row[key] for key in EXPORT_HEADERS[:7]]
            for side in ('left', 'right'):
                values.extend(row[side][key] for key in ('source_name', 'location', 'excerpt'))
            values.extend([payload.get('format', 'exculpata-inconsistency-candidates-v1'), REVIEW_NOTICE, payload.get('dropped', 0), payload.get('capped', False)])
            values.extend(row[side].get(key, '') for side in ('left', 'right') for key in ('document_id', 'source_version_id'))
            # Spreadsheet formula injection protection applies to every text cell.
            writer.writerow(["'" + str(v) if str(v or '').lstrip().startswith(('=', '+', '-', '@')) else v for v in values])
        return output.getvalue()
    lines = ['# Possible inconsistencies for review', '', REVIEW_NOTICE, '',
             'Format: ' + payload['format'],
             f"Dropped candidates: {payload['dropped']}. Bounded scan limit reached: {payload['capped']}.", '']
    for row in payload['candidates']:
        lines.extend(['## ' + row['difference_type'].replace('_', ' '), '', _text(row['note']), '',
                      'Status: ' + row['status'], 'Signature: ' + row['signature'],
                      'Reviewed by: ' + _text(row['reviewed_by'] or 'Not reviewed'),
                      'Reviewed at: ' + (row['reviewed_at'] or 'Not reviewed'), ''])
        for side in ('left', 'right'):
            citation = row[side]
            lines += [side.upper() + ': ' + _text(citation['source_name']) + ' · ' + _text(citation['location']),
                      '', *('> ' + _text(line) for line in citation['excerpt'].splitlines()), '',
                      'Source: ' + _text(citation['document_id']) + ' · version: ' + _text(citation['source_version_id']), '']
    return '\n'.join(lines)
