"""Authenticated generation-bound seeks and literal candidate-window filtering."""
from __future__ import annotations

import base64
from dataclasses import asdict, replace
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlencode

from case_intelligence.modules.discovery_review import presentation, reader
from case_intelligence.modules.discovery_review.stored_index.repository import digest, packed

SECRET = secrets.token_bytes(32)
SIZE = 100


class CursorInvalid(ValueError):
    """The list changed or the carried traversal does not describe this view."""


def encode_cursor(payload):
    body = base64.urlsafe_b64encode(packed(payload).encode()).decode().rstrip('=')
    signature = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return body + '.' + signature


def decode_cursor(value, scope, total):
    try:
        if len(value) > 4096:
            raise ValueError
        body, signature = value.split('.')
        if not hmac.compare_digest(signature, hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if (set(payload) != {'scope', 'anchor', 'previous', 'start', 'seen', 'matched', 'origin'}
                or payload['scope'] != scope or not isinstance(payload['anchor'], str)
                or type(payload['previous']) is not bool or type(payload['origin']) is not bool
                or any(type(payload[key]) is not int or not 0 <= payload[key] <= total for key in ('start', 'seen', 'matched'))
                or payload['matched'] > payload['seen']):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise CursorInvalid('The list changed. Navigation restarted with the current view.') from exc


def items(view, sources):
    stored = view.repository.read_items(view.matter_id, [source.document_id for source in sources])
    found = {}
    for source in sources:
        row = stored.get(source.document_id)
        if (row and row[0] and row[1] == digest(asdict(source)) and row[3] == view.generation
                and view.state.get('generation') == view.generation):
            item = reader.Item(**json.loads(row[0]))
        else:
            item = replace(reader.project_item(source, view.definition, {}),
                           metadata_state='error' if view.error else 'pending')
        found[source.document_id] = item
    return found


def checked_view(view):
    target = view.target_reader()
    state = view.repository.state(view.matter_id) or {}
    if target != view.target or state.get('generation') != view.generation:
        return replace(view, complete=False, generation=digest(target), target=target, summaries={})
    return view


def lookup(view, token):
    with view.store.mutation_guard(), view.repository.db.lock:
        view = checked_view(view)
        source = view.store.source_changes.lookup(token)
        return items(view, (source,))[source.document_id]


def boundary_key(view, field, descending, document_id):
    rows = view.repository.rows('SELECT sort_key FROM workbench_discovery_index_value '
                                'WHERE matter_id=? AND document_id=? AND field_key=? AND direction=?',
                                (view.matter_id, document_id, field, int(descending)))
    if not rows:
        raise CursorInvalid('The list changed. Navigation restarted with the current view.')
    return bytes(rows[0][0])


def list_page(definition, view, choice, **options):
    with view.store.mutation_guard(), view.repository.db.lock:
        return captured_page(definition, checked_view(view), choice, **options)


def captured_page(definition, view, choice, *, base, q, sort, descending, page, doc, edge, show_all, cursor='', selected_view='', unit=1):
    requested_doc = doc
    size = min(SIZE, definition.page_size)
    sort = sort if definition.field(sort) is not None else definition.default_sort
    needle = q.strip().casefold()
    scope = digest((view.matter_id, view.generation, view.definition_digest, choice.columns,
                    choice.item_fields, needle, sort, descending, view.complete, selected_view, unit, show_all))
    field = sort
    if sort.startswith('lf:') and sort[3:] not in view.headers:
        field = ''
    progress = {'scope': scope, 'anchor': '', 'previous': False, 'start': 0, 'seen': 0, 'matched': 0, 'origin': True}
    notice = ''
    if page > 1:
        notice = 'Numbered page links have changed. Navigation restarted; use Next and Previous.'
        cursor, doc = '', ''
    if cursor:
        try:
            progress = decode_cursor(cursor, scope, view.total)
        except CursorInvalid as exc:
            notice, cursor, doc = str(exc), '', ''
    anchor = progress['anchor']
    previous = progress['previous']
    if doc and not cursor and not notice:
        try:
            selected_source = view.store.source_changes.lookup(doc)
            if view.complete:
                key = boundary_key(view, field, descending, selected_source.document_id)
                preceding = view.repository.page(view.matter_id, field, descending, boundary=key, previous=True, limit=1)
                anchor = preceding[0][1] if preceding else ''
            else:
                preceding = view.store.source_changes.seek(selected_source.document_id, limit=1, previous=True)
                anchor = preceding[0].document_id if preceding else ''
            progress.update(anchor=anchor, origin=not bool(anchor))
        except (KeyError, CursorInvalid):
            doc = ''
    try:
        if view.complete:
            key = boundary_key(view, field, descending, anchor) if anchor else b''
            candidates = view.repository.page(view.matter_id, field, descending,
                                               boundary=key, previous=previous, limit=size + 1)
            identities = [document for _, document in candidates[:size]]
            sources = view.store.source_changes.read(identities)
            more = len(candidates) > size
        else:
            candidates = view.store.source_changes.seek(anchor, limit=size + 1, previous=previous)
            identities = [source.document_id for source in candidates[:size]]
            sources, more = candidates[:size], len(candidates) > size
    except CursorInvalid as exc:
        return list_page(definition, view, choice, base=base, q=q, sort=sort, descending=descending,
                         page=2, doc='', edge=edge, show_all=show_all, cursor='') | {'notice': str(exc)}
    if previous:
        identities.reverse()
    resolved = items(view, sources)
    if len(resolved) != len(identities):
        raise CursorInvalid('The source population changed. Reload the list.')
    searched = tuple(dict.fromkeys((*choice.columns, definition.heading_title)))
    visible = [resolved[document] for document in identities if not (needle and view.complete)
               or any(needle in resolved[document].value(key).casefold() for key in searched)]
    if progress['start'] == progress['seen']:
        progress['seen'] += len(identities)
        progress['matched'] += len(visible)
    exhausted = progress['origin'] and progress['seen'] == view.total
    filtered = bool(needle) and view.complete
    state = {'q': q.strip(), 'sort': sort if sort != definition.default_sort or descending else '',
             'dir': 'desc' if descending else '', 'all_columns': '1' if show_all else '', 'view': selected_view, 'unit': str(unit) if unit != 1 else ''}

    def url(**changes):
        query = {key: value for key, value in {**state, **changes}.items() if value}
        return base + ('?' + urlencode(query) if query else '')

    def seek_url(document_id, backwards, start):
        carried = {**progress, 'anchor': document_id, 'previous': backwards, 'start': max(0, start)}
        return url(cursor=encode_cursor(carried), edge='last' if backwards else '')

    has_previous = more if previous else bool(anchor)
    has_next = bool(anchor) if previous else more
    before = seek_url(identities[0], True, progress['start'] - size) if identities and has_previous else ''
    after = seek_url(identities[-1], False, progress['start'] + len(identities)) if identities and has_next else ''
    selected = next((item for item in visible if item.token == doc), None)
    if selected is None and visible:
        selected = visible[-1] if edge == 'last' else visible[0]
    columns = [definition.field(key) for key in choice.columns]
    position = visible.index(selected) if selected is not None else None
    window_cursor = encode_cursor({**progress, 'anchor': anchor, 'previous': previous})
    rows = [{'item': item, 'heading': presentation.heading(definition, item),
             'url': url(cursor=window_cursor, doc=item.token), 'selected': item is selected,
             'cells': [presentation.cell(definition, column, item) for column in columns]} for item in visible]
    return {'columns': [{'field': column, 'sorted': column.key == sort and view.complete, 'descending': descending,
                         'url': url(sort=column.key, dir='desc' if column.key == sort and not descending else '')} for column in columns],
            'rows': rows, 'total': view.total, 'matching': progress['matched'] if filtered else view.total,
            'filtered': bool(needle), 'positions_known': not filtered and progress['origin'], 'filter_complete': filtered and exhausted, 'coverage': progress['seen'],
            'matches_so_far': progress['matched'], 'complete': view.complete, 'notice': notice,
            'index_error': job_error(view), 'indexed': view.state.get('indexed', 0), 'errors': view.state.get('errors', 0),
            'pending': max(0, view.total - view.state.get('indexed', 0) - view.state.get('errors', 0)),
            'last_known': view.state.get('generation') != view.generation,
            'page': progress['start'] // size + 1, 'pages': max(1, (view.total + size - 1) // size),
            'first_row': progress['start'] + 1 if identities else 0,
            'last_row': progress['start'] + len(identities), 'previous_page': before, 'next_page': after,
            'selected': selected, 'position': 0 if filtered or not progress['origin'] or position is None else progress['start'] + position + 1,
            'previous_item': rows[position - 1]['url'] if position else before,
            'next_item': rows[position + 1]['url'] if position is not None and position + 1 < len(rows) else after,
            'refresh': url(doc=requested_doc), 'clear_filter': url(q=''), 'show_all': url(all_columns='1'), 'state': state,
            'hidden': [definition.by_key[key].label for key in choice.hidden]}


def job_error(view):
    return view.error
