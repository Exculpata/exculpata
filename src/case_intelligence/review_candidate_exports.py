"""Cited exports with a leading notice and export-specific escaping."""
import csv
import io
import json

from .assertion_exports import _text
from .review_candidates import CATEGORIES, FORMAT, NOTICE


def _cell(value):
    text = str(value or '')
    unsafe = (text.lstrip().startswith(('=', '+', '-', '@'))
              or text.lstrip(' ').startswith(('\t', '\r', '\n')))
    return "'" + text if unsafe else text


def export_payload(payload, format_name):
    if format_name == 'json':
        return json.dumps(dict(notice=NOTICE, **{k:v for k,v in payload.items() if k!='notice'}), ensure_ascii=False, indent=2)
    if format_name == 'csv':
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow([NOTICE])
        writer.writerow(['format', FORMAT, 'bounded_scan', 'yes' if payload['capped'] else 'no', 'dropped', payload['dropped']])
        writer.writerow(['signature','category','reason','additional_reason','status','reviewed_by','reviewed_at',
                         'source','locator','excerpt','document_id','source_version_id','chunk_id','citation_json','related_citation_json'])
        for row in payload['candidates']:
            c=row['citation']
            values=[row.get(k,'') for k in ('signature','category','reason','additional_reason','status','reviewed_by','reviewed_at')]
            values += [c[k] for k in ('source_name','location','excerpt','document_id','source_version_id','chunk_id')]
            values += [json.dumps(c,ensure_ascii=False),json.dumps(row.get('related_citation'),ensure_ascii=False)]
            writer.writerow([_cell(v) for v in values])
        return output.getvalue()
    if format_name != 'markdown':
        raise ValueError('Choose markdown, csv or json.')
    lines=[NOTICE, '', '# Candidates for review', '', 'Format: '+FORMAT, '',
           f"Extracted units scanned: {payload['unit_count']}. Citation omissions: {payload['dropped']}. Bounded scan: {'yes' if payload['capped'] else 'no'}.", '']
    for row in payload['candidates']:
        lines += ['## Candidate for review', '', _text(CATEGORIES[row['category']]), '', _text(row['reason']),
                  _text(row.get('additional_reason','')), '', 'Status: '+_text(row['status']),
                  'Signature: '+_text(row['signature']),
                  'Reviewed by: '+_text(row.get('reviewed_by') or 'Not reviewed'),
                  'Reviewed at: '+_text(row.get('reviewed_at') or 'Not reviewed'), '']
        for c in (row['citation'], row.get('related_citation')):
            if not c:
                continue
            lines += ['Original passage: '+_text(c['source_name'])+' · '+_text(c['location']), '',
                      *('> '+_text(line) for line in c['excerpt'].splitlines()), '',
                      'Source: '+_text(c['document_id'])+' · version: '+_text(c['source_version_id'])+' · unit: '+_text(c['chunk_id']), '']
    return '\n'.join(lines)
