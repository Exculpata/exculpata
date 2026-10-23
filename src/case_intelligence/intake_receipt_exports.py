"""Readable and machine-readable selection receipts without source bytes."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import csv
import io
import json
from collections.abc import Mapping

from .work_product_exports import ExportArtifact, ExportProblem, MAX_WORKFLOW_EXPORT_BYTES, _csv_safe

LABELS = {
    'included': 'Included for upload', 'skipped': 'Not uploaded',
    'not_received': 'Not received', 'partial': 'Partly received', 'received': 'Received',
    'not_uploaded': 'Not uploaded', 'not_started': 'Not started', 'uploading': 'Uploading',
    'processing': 'Processing', 'searchable': 'Searchable', 'playback_only': 'Playback only',
    'needs_review': 'Needs review', 'failed': 'Failed or cancelled', 'unavailable': 'Source unavailable',
    'valid': 'Ready at confirmation', 'needs_attention': 'Needs attention', 'unsupported': 'Unsupported',
    'duplicate_candidate': 'Repeated path', 'over_limit': 'Over limit',
}
BOUNDARY = ('This receipt lists files supplied by the browser. Empty folders are not listed. '
            'Received files may still need processing. Original source files are not included in this download. '
            'Selection review records what the browser reported; server filename checks, received bytes and source availability are separate. '
            'Status reflects the time of export.')


def portable_receipt(receipt: Mapping) -> dict:
    allowed = ('collection_name', 'selected_count', 'recorded_count', 'state', 'created_at', 'updated_at', 'counts', 'model_notice')
    result = {key: receipt[key] for key in allowed if key in receipt}
    result['boundary'] = BOUNDARY
    result['items'] = [{key: value for key, value in row.items()
        if key not in {'document_id', 'catalog_document_id', 'version_id', 'source_url', 'receipt_id'}}
        for row in receipt.get('items', [])]
    return result


def export_intake_receipt(receipt: Mapping, format_name: str, model_notice: str = "") -> ExportArtifact:
    stamped = dict(receipt)
    notice = str(stamped.get("model_notice") or model_notice or "")
    if notice:
        stamped["model_notice"] = notice
    portable = portable_receipt(stamped)
    counts = portable['counts']
    if format_name == 'json':
        body = json.dumps({'schema': f"{LEGACY_STEM}-intake-receipt-v1", **portable}, ensure_ascii=False, indent=2).encode('utf-8')
        media_type = 'application/json; charset=utf-8'
    elif format_name == 'csv':
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(['Selection', 'Selected files', 'Recorded rows', 'Unrecorded rows', 'Row', 'Relative path or name',
            'Expected bytes', 'Selection disposition', 'Filename check', 'Received bytes', 'Transfer', 'Availability', 'Reason', 'Reported selection review', 'Filename check reason', 'Processing message', 'Model notice'])
        for row in portable['items']:
            writer.writerow([_csv_safe(str(value)) for value in [portable['collection_name'], portable['selected_count'],
                portable['recorded_count'], counts['unrecorded'], row['ordinal'] + 1,
                row['relative_path'] or row['display_name'], row['expected_size'] if row['expected_size'] is not None else '',
                LABELS[row['selection_state']], LABELS[row['preflight_state']], row['received_size'],
                LABELS[row['transfer_state']], LABELS[row['availability']], row['reviewed_reason'],
                LABELS[row['reviewed_state']], row['reason'], row.get('upload_message') or '', portable.get('model_notice', '')]])
        if not portable['items']:
            writer.writerow([_csv_safe(str(value)) for value in [portable['collection_name'], portable['selected_count'],
                portable['recorded_count'], counts['unrecorded'], '', '', '', '', '', '', '', '',
                'Selection recording is incomplete. No file rows were recorded. Reselect the same files to finish the receipt.', '', '', '', portable.get('model_notice', '')]])
        body = stream.getvalue().encode('utf-8-sig')
        media_type = 'text/csv; charset=utf-8'
    elif format_name == 'markdown':
        def literal(value):
            return str(value).replace('\\', '\\\\').replace('`', '\\`').replace('*', '\\*').replace('_', '\\_').replace('<', '&lt;').replace('>', '&gt;').replace('[', '\\[').replace(']', '\\]').replace('#', '\\#')
        lines = ['# Selected-file receipt', '']
        if portable.get('model_notice'):
            lines.extend([literal(portable['model_notice']), ''])
        lines.extend([literal(portable['collection_name']), '',
            f"{counts['selected']} selected · {counts['included']} included in recorded rows · {counts['skipped']} skipped · {counts['unrecorded']} not yet recorded", '',
            f"{counts['received']} received · {counts['partial']} partly received · {counts['not_received']} included files not received", '',
            f"{counts['searchable']} searchable · {counts['processing']} uploading/processing · {counts['playback_only']} playback only · {counts['needs_review']} need review · {counts['failed']} failed/cancelled · {counts['unavailable']} unavailable · {counts['not_started']} not started", '',
            BOUNDARY, ''])
        for row in portable['items']:
            lines.extend([f"## {row['ordinal'] + 1}. {literal(row['relative_path'] or row['display_name'])}", '',
                f"{LABELS[row['selection_state']]} · Reported selection review: {LABELS[row['reviewed_state']]} · Filename check: {LABELS[row['preflight_state']]} · {LABELS[row['transfer_state']]} · {LABELS[row['availability']]}", '',
                literal(row['reviewed_reason']), '', literal(row.get('upload_message') or row['reason']), ''])
        body = '\n'.join(lines).encode('utf-8')
        media_type = 'text/markdown; charset=utf-8'
    else:
        raise ExportProblem('Choose CSV, Markdown or JSON for the selection receipt.')
    if len(body) > MAX_WORKFLOW_EXPORT_BYTES:
        raise ExportProblem('This receipt is too large to export in one file.')
    extension = 'md' if format_name == 'markdown' else format_name
    return ExportArtifact(body, media_type, f'selected-file-receipt.{extension}')
