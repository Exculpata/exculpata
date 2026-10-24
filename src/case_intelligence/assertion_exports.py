"""Portable readable exports of reviewer-owned assertions and competing accounts."""
import html
import json
import re


def _text(value):
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', html.escape(str(value), quote=False))


def markdown_export(payload):
    lines = ['# Events and assertions', '',
             'Reviewer-authored records with attributed original accounts. Human review is separate from source claims.', '']
    notice = str(payload.get('model_notice') or '')
    if notice:
        lines.extend([_text(notice), ''])
    lines.extend([_text(payload.get('ordering', '')), ''])
    records = payload.get('records', [payload])
    for entry in records:
        record = entry['record']
        lines.extend(['## ' + _text(record['title']), '', _text(record['statement']), '',
                      'Type: ' + _text(record['record_type']) + ' · Human review: ' + _text(record['status']),
                      'Date as stated: ' + _text(record['raw_date'] or 'Not stated'),
                      'Uncertainty: ' + _text(record['date_uncertainty'] or 'Not recorded'),
                      'Reviewer-entered ordering date: ' + _text(record['sort_date'] or 'Unresolved / not dated'),
                      'Revision: ' + str(record['revision']), '', '### Entity roles', ''])
        for role in entry['roles']:
            lines.extend(['- ' + _text(role['role'] + ': ' + role['display_name'] + ' (' + role['entity_type'] + ')'),
                          '  Identity: ' + _text(role['entity_id']) + ', saved entity revision ' + str(role['entity_revision']) +
                          ', current state: ' + _text(role.get('identity_state', 'not checked'))])
        lines.extend(['', '### Original source accounts', ''])
        for account in entry['accounts']:
            _account(lines, account)
        lines.extend(['### Correction history', ''])
        for history in entry['history']:
            prior = json.loads(history['snapshot_json'])
            lines.extend(['#### Revision ' + str(history['revision']) + ': ' + _text(history['action']), '',
                          _text(history['created_at'] + ' · ' + history['actor_id']), '',
                          _text(prior['title']), '', _text(prior['statement']), '',
                          'Human review: ' + _text(prior['status']),
                          'Date as stated: ' + _text(prior['raw_date']),
                          'Uncertainty: ' + _text(prior['date_uncertainty']),
                          'Ordering date: ' + _text(prior['sort_date']), ''])
            for key in ('added_accounts', 'removed_accounts'):
                if prior.get(key):
                    lines.extend([key.replace('_', ' ').title(), ''])
                for account in prior.get(key, []):
                    _account(lines, account)
            for key in ('added_roles', 'removed_roles', 'corrected_roles', 'corrected_accounts'):
                for correction in prior.get(key, []):
                    lines.extend([_text(key.replace('_', ' ').title()) + ': ' + _text(json.dumps(correction, ensure_ascii=False)), ''])
    return '\n'.join(lines) + '\n'


def qualified_payload(payload, model_notice=''):
    notice = str(payload.get('model_notice') or model_notice or '')
    if not notice or payload.get('model_notice') == notice:
        return payload
    qualified = dict(payload)
    qualified['model_notice'] = notice
    return qualified


def _account(lines, account):
    lines.extend(['#### ' + _text(account['stance'].title() + ' account: ' + account['attributed_to']), '',
                  _text(account['source_name'] + ' · ' + account['location']), '',
                  '> ' + _text(account['excerpt']).replace('\n', '\n> '), '',
                  'Source: ' + _text(account['document_id']) + ' · version: ' + _text(account['source_version_id']),
                  'Unit: ' + str(account['unit_number']) + ' · chunk: ' + _text(account['chunk_id']),
                  'Excerpt digest: ' + _text(account['excerpt_digest']),
                  'Original availability: ' + ('current at export' if account.get('available') is True else
                  'changed or unavailable' if account.get('available') is False else 'not revalidated for this retained history'), ''])
