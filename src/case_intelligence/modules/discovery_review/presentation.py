"""Shape the review list, the open item and the view chooser for templates."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import zip_longest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from case_intelligence.modules.discovery_review.fields import (
    LOAD_FILE_GROUP, LOAD_FILE_PREFIX, Definition, Field,
)
from case_intelligence.modules.discovery_review.reader import Index, Item, action_token

ITEM_VIEWS = (("document", "Document"), ("text", "Text"), ("fields", "Fields"))
MAX_QUERY_CHARS = 200


@dataclass(frozen=True, slots=True)
class Choice:
    columns: tuple[str, ...]
    item_fields: tuple[str, ...]
    origin: str
    hidden: tuple[str, ...] = ()


def choose(definition: Definition, saved: dict, index: Index, *, show_all: bool = False) -> Choice:
    """Prefer the reviewer's view, then the matter's shared view, then the definition."""
    for origin in ("user", "matter"):
        view = saved.get(origin)
        columns = definition.clean(view.columns, limit=definition.max_columns) if view else ()
        if columns:
            return Choice(columns, definition.clean(view.item_fields) or definition.default_item_fields, origin)
    columns, hidden = definition.default_columns, ()
    if definition.hide_empty_default_columns and index.total and index.complete and not show_all:
        hidden = tuple(key for key in columns if key not in index.filled)
        columns = tuple(key for key in columns if key in index.filled) or columns
    return Choice(columns, definition.default_item_fields, "default", hidden if len(hidden) < len(definition.default_columns) else ())


def applies(definition: Definition, chosen: Field, item: Item) -> bool:
    """Type-specific fields exist only on items of that type."""
    group = next((g for g in definition.groups if g.key == chosen.group), None)
    needs = chosen.applies_to or (group.applies_to if group else "")
    return not needs or needs == item.kind


def unresolved(item, chosen=None):
    if item.metadata_state == 'complete':
        return ''
    if (chosen is not None and chosen.computed in {'name', 'type', 'pages', 'folder', 'size', 'text_status', 'status', 'stored_from', 'sha256', 'path'}
            and item.value(chosen.key)):
        return ''
    return 'Metadata pending' if item.metadata_state == 'pending' else 'Metadata unavailable: indexing error'


def cell(definition: Definition, chosen: Field, item: Item) -> dict:
    if note := unresolved(item, chosen):
        return {"value": note, "state": item.metadata_state, "note": note}
    if not applies(definition, chosen, item):
        return {"value": "", "state": "na", "note": f"Shown only for {chosen.applies_to or 'matching'} items"}
    if (item.production_state != 'complete' and not chosen.computed
            and not item.value(chosen.key)):
        return {'value': '', 'state': item.production_state,
                'note': 'Production metadata is not currently available.'}
    if not item.has(chosen.key):
        return {"value": "", "state": "absent", "note": "This item's load file has no such field"}
    value = item.value(chosen.key)
    return {"value": value, "state": "value" if value else "empty", "note": ""}


def heading(definition: Definition, item: Item) -> dict:
    if note := unresolved(item):
        return {"label": note, "missing": True, "title": item.value(definition.heading_title)}
    begin, end = (item.value(key) for key in definition.heading_range)
    label = f"{begin} – {end}" if begin and end and end != begin else begin
    label = label or (item.value(definition.heading_fallback) if definition.heading_fallback else "")
    return {"label": label or definition.heading_missing, "missing": not label,
            "title": item.value(definition.heading_title)}


def ordered(definition: Definition, index: Index, choice: Choice, *, q: str, sort: str,
            descending: bool) -> tuple[int, ...]:
    """Item positions in the order and filter the reviewer is looking at."""
    sort = sort if definition.field(sort) is not None else definition.default_sort
    positions = index.order(definition, sort, descending)
    needle = q.strip().casefold()
    if not needle:
        return positions
    searched = tuple(dict.fromkeys((*choice.columns, definition.heading_title)))
    return tuple(p for p in positions
                 if any(needle in index.items[p].value(key).casefold() for key in searched))


def list_page(definition: Definition, index: Index, choice: Choice, *, base: str, q: str, sort: str,
              descending: bool, page: int, doc: str, edge: str, show_all: bool, view: str = "") -> dict:
    sort = sort if definition.field(sort) is not None else definition.default_sort
    columns = [definition.field(key) for key in choice.columns]
    needle = q.strip().casefold()
    positions = ordered(definition, index, choice, q=q, sort=sort, descending=descending)
    size, matching = definition.page_size, len(positions)
    pages = max(1, -(-matching // size))
    selected = index.by_token.get(doc)
    where = positions.index(selected) if selected is not None and selected in positions else None
    page = where // size + 1 if where is not None else min(max(page, 1), pages)
    window = positions[(page - 1) * size:page * size]
    if where is None and window:
        where = (page - 1) * size + (len(window) - 1 if edge == "last" else 0)
    state = {"view": view, "q": q.strip(), "sort": sort if sort != definition.default_sort or descending else "",
             "dir": "desc" if descending else "", "all_columns": "1" if show_all else ""}

    def url(**changes) -> str:
        query = {key: value for key, value in {**state, **changes}.items() if value}
        return base + ("?" + urlencode(query) if query else "")

    def item_url(position) -> str:
        return url(doc=index.items[position].token)

    rows = [{"item": index.items[p], "heading": heading(definition, index.items[p]), "url": item_url(p),
             "selected": where is not None and positions[where] == p,
             "family_context": index.items[p].family.get('context'),
             "cells": [cell(definition, column, index.items[p]) for column in columns]} for p in window]
    for row in rows:
        if row['family_context']:
            row['family_context'] = {**row['family_context'],
                                     'url': base + '?' + urlencode({'doc': row['family_context']['token']})}
    headers = [{"field": column, "sorted": column.key == sort, "descending": descending,
                "url": url(sort=column.key, dir="desc" if column.key == sort and not descending else "")}
               for column in columns]
    return {
        "columns": headers, "rows": rows, "total": len(index.items), "matching": matching,
        "filtered": bool(needle), "page": page, "pages": pages,
        "first_row": (page - 1) * size + 1 if window else 0, "last_row": (page - 1) * size + len(window),
        "previous_page": url(page=str(page - 1), edge="last") if page > 1 else "",
        "next_page": url(page=str(page + 1)) if page < pages else "",
        "selected": index.items[positions[where]] if where is not None else None,
        "position": where + 1 if where is not None else 0,
        "previous_item": item_url(positions[where - 1]) if where else "",
        "next_item": item_url(positions[where + 1]) if where is not None and where + 1 < matching else "",
        "clear_filter": url(q=""), "show_all": url(all_columns="1"), "state": state,
        "hidden": [definition.by_key[key].label for key in choice.hidden],
    }


def read_unit(store, item: Item, number: int):
    """Stream the stored extracted text and keep only the requested section."""
    document = store.get(item.document_id)
    if document.version_id != item.version_id:
        raise KeyError(item.token)
    total, chosen, last = 0, None, None
    for unit in document.iter_parsed_units():
        total += 1
        last = unit
        if total == number:
            chosen = unit
    return (chosen or last), (number if chosen else total), total


def _tiff_embed(store, slug: str, document_id: str, version_id: str, frame: int, links, *,
                from_page_record: bool = False, record_cursor: str = "") -> dict | None:
    from case_intelligence.modules.discovery_review.tiff_preview import TiffPreviewError, TiffPreviewStale, inspect_tiff

    try:
        with store.mutation_guard():
            document = store.get(document_id)
            if document.version_id != version_id or document.state != "ready" or document.origin != "upload":
                return None
            path = store.source_path(document.document_id, verify_digest=True)
            meta = inspect_tiff(path, document.digest)
    except (KeyError, OSError, ValueError, TiffPreviewError, TiffPreviewStale):
        return None
    current_frame = min(max(int(frame), 1), meta.frame_count)
    token = action_token(document_id, version_id)
    extra = {"record": record_cursor, "record_dir": "at"} if record_cursor else {}
    return {
        "kind": "image",
        "url": f"/matters/{slug}/production/tiff-preview/{token}?frame={current_frame}",
        "frame": current_frame,
        "frame_count": meta.frame_count,
        "from_page_record": from_page_record,
        "previous": links(view="document", tiff_frame=str(current_frame - 1), **extra) if current_frame > 1 else None,
        "next": links(view="document", tiff_frame=str(current_frame + 1), **extra) if current_frame < meta.frame_count else None,
    }


def item_panel(definition: Definition, item: Item, choice: Choice, store, *, slug: str, view: str,
               unit: int, list_url: str, production: dict | None = None,
               record_window: dict | None = None, tiff_frame: int = 1) -> dict:
    production = production or {}
    references = tuple(production.get('references', ()))
    page_records = any(reference['category'] == 'page' for reference in references)
    available = [key for key, offered in (("document", bool(item.embedded) or page_records), ("text", item.has_text),
                                          ("fields", True)) if offered]
    active = view if view in available else available[0]
    sources_url = f"/matters/{slug}/sources"
    source_url = f"{sources_url}/{item.token}"
    fragment = f"/matters/{slug}/production/items/{item.token}"
    parsed_url = urlsplit(list_url)
    base_query = {key: value for key, value in parse_qsl(parsed_url.query)
                  if key not in {'record', 'record_dir', 'unit', 'image_start', 'tiff_frame'}}

    def links(**query) -> dict:
        merged = {key: value for key, value in {**base_query, **query}.items() if value}
        encoded = urlencode(merged)
        return {"url": urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path,
                                  encoded, parsed_url.fragment)),
                "fragment": f"{fragment}?{encoded}"}

    panel = {
        "item": item, "heading": heading(definition, item), "active": active, "source_url": source_url,
        "kind": item.kind, "fragment": fragment,
        "state": {**dict(parse_qsl(urlsplit(list_url).query)), "view": active, "unit": unit},
        "views": [{"key": key, "label": label, "current": key == active, **links(view=key)}
                  for key, label in ITEM_VIEWS if key in available],
        "unavailable": [label for key, label in ITEM_VIEWS if key not in available],
        "production": production, "family": item.family,
        "production_reload": links(reload_attempt=production.get('attempt_id', ''),
                                   reload_token=production.get('reload', ''))['url'],
        "record_sources": [{"name": reference['source_name'], "status": reference.get('status', ''),
            "url": f'/matters/{slug}/production?' + urlencode({
                'doc': action_token(reference['source_id'], reference['source_version_id']),
                'view': 'document' if reference['category'] == 'page' else 'fields'})}
            for reference in references if reference['source_id'] != item.document_id],
    }
    if item.family.get('context'):
        panel['family'] = {**item.family, 'context': {**item.family['context'],
            'url': f'/matters/{slug}/production?' + urlencode({'doc': item.family['context']['token']})}}
    if active == "document":
        if item.media_type == "image/tiff" and item.embedded:
            panel["embed"] = _tiff_embed(store, slug, item.document_id, item.version_id, tiff_frame, links)
        else:
            panel["embed"] = {"kind": item.embedded, "url": f"{source_url}/content"} if item.embedded else None
        if record_window is not None and record_window['category'] == 'page':
            panel['records'] = record_panel(definition, record_window, links, view='document')
            panel['native_embed'], panel['embed'] = panel['embed'], None
            current = record_window.get('current') or {}
            image = current.get('image') or {}
            if image.get('media_type') in {'image/jpeg', 'image/png'}:
                image_token = action_token(image['document_id'], image['version_id'])
                panel['embed'] = {'kind': 'image', 'url': f'/matters/{slug}/sources/{image_token}/content'}
            elif image.get('media_type') == 'image/tiff':
                panel['embed'] = _tiff_embed(
                    store, slug, image['document_id'], image['version_id'], tiff_frame, links,
                    from_page_record=True, record_cursor=panel['records']['current_cursor'],
                )
    elif active == "text":
        try:
            found, number, total = read_unit(store, item, max(unit, 1))
        except (KeyError, OSError, RuntimeError, ValueError):
            found, number, total = None, 0, 0
        paged = item.media_type == "application/pdf"
        panel["text"] = {
            "body": found.text if found else "", "available": found is not None, "number": number, "total": total,
            "position": "" if found is None else (
                f"Page {found.number} of {item.numbers.get('pages') or total}" if paged
                else found.location + (f" ({number} of {total})" if total > 1 else "")),
            "previous": links(view="text", unit=str(number - 1)) if number > 1 else None,
            "next": links(view="text", unit=str(number + 1)) if found and number < total else None,
        }
    else:
        panel["fields"] = item_fields(definition, item, choice)
        if record_window is not None and record_window['category'] == 'document':
            panel['records'] = record_panel(definition, record_window, links, view='fields', sources_url=sources_url)
    if panel.get('records') and panel['records']['current_cursor']:
        panel['state'].update(record=panel['records']['current_cursor'], record_dir='at')
        for record in panel['records']['rows']:
            record['indexed_images'] = indexed_images(record, record_window['attempt'], slug, links)
    return panel


def indexed_images(record, attempt, slug, links):
    """Keep image file navigation separate from document and page associations."""
    def image_file(reference):
        source = reference['source']
        url = ''
        if source:
            token = action_token(source['document_id'], source['version_id'])
            url = f'/matters/{slug}/sources/{token}'
        return {**reference, 'url': url}

    if record.get('image_reference'):
        return {'page_file': image_file(record['image_reference'])}
    if (attempt['source_format'] != 'dii' or attempt['status'] != 'complete'
            or record['kind'] != 'accepted'):
        return {}
    cursor = f"dat:{attempt['attempt_id']}:{record['ordinal']}"
    def browse(start):
        return links(view='fields', record=cursor, image_start=str(start))['url']

    window = record.get('image_files')
    if window is None:
        return {'open': browse(1)}
    return {'rows': [image_file(entry) for entry in window['rows']],
            'start': window['start'], 'total': window['total'],
            'previous': browse(window['previous_start']) if window['previous_start'] else '',
            'next': browse(window['next_start']) if window['next_start'] else ''}


def record_panel(definition: Definition, window: dict, links, *, view: str, sources_url: str = "") -> dict:
    """Render bounded stored records exactly, including unrecoverable values."""
    kind = 'dat' if window['category'] == 'document' else 'opt'
    attempt = window['attempt']
    attempt_id = attempt['attempt_id']
    rows = []
    for record in window['records']:
        supplied = record.get('cells')
        headings = record.get('headers')
        if kind == 'opt' and headings is None:
            headings = tuple(column.label for column in definition.opt_columns)
        cells = []
        for ordinal, (label, value) in enumerate(zip_longest(
                headings or (), supplied or (), fillvalue=None), 1):
            cells.append({'position': ordinal, 'label': label, 'value': value,
                          'has_header': label is not None, 'has_value': value is not None})
        files = dict(record.get('files', {}))
        for role in ('matched', 'text'):
            if source := files.get(role):
                token = action_token(source['document_id'], source['version_id'])
                files[role] = {**source, 'url': f'{sources_url}/{token}'}
        rows.append({**record, 'files': files, 'display_cells': cells, 'recoverable': supplied is not None,
                     'source_format': str(record.get('source_format') or attempt['source_format']).upper()})
    current = window.get('current')
    cursor = f"{kind}:{attempt_id}:{current['ordinal']}" if current else ''
    return {'kind': kind, 'attempt': attempt, 'rows': rows, 'current_cursor': cursor,
            'counts': window.get('counts', {}), 'total': window.get('total', 0),
            'previous': links(view=view, record=window['previous'], record_dir='at')
            if window.get('previous') else None,
            'next': links(view=view, record=window['next'], record_dir='at')
            if window.get('next') else None}


def item_fields(definition: Definition, item: Item, choice: Choice) -> dict:
    """Group the chosen fields first; every other field stays one step away."""
    chosen = [f for f in (definition.field(key) for key in choice.item_fields) if f is not None]
    rest = [f for f in definition.fields if f.key not in choice.item_fields]

    def groups(candidates) -> list[dict]:
        shaped = []
        for group in definition.groups:
            members = [f for f in candidates if f.group == group.key and (unresolved(item) or applies(definition, f, item))]
            filled = [{"label": f.label, "value": unresolved(item, f) or item.value(f.key), "details": () if unresolved(item, f) else item.details.get(f.key, ())}
                      for f in members if unresolved(item, f) or item.value(f.key)]
            empty = [f.label for f in members if not unresolved(item, f) and not item.value(f.key)]
            if filled or empty:
                shaped.append({"label": group.label, "rows": filled, "empty": empty})
        return shaped

    extra = [f for f in chosen if f.group == LOAD_FILE_GROUP]
    produced = [{"label": header, "value": value} for header, value in item.load_file.items()]
    return {
        "primary": groups(chosen), "more": groups(rest),
        "picked": [{"label": f.label, "value": unresolved(item, f) or item.value(f.key), "absent": not unresolved(item, f) and not item.has(f.key)} for f in extra],
        "load_file_state": unresolved(item), "load_file": [] if unresolved(item) else produced, "load_file_open": definition.show_load_file_fields_by_default,
        "load_file_source": item.values.get("load_file", ""),
        "metadata_state": item.metadata_state if unresolved(item) else item.production_state,
    }


def chooser(definition: Definition, index: Index, choice: Choice) -> dict:
    """List every field once per choice, chosen ones first in their saved order."""
    produced = [definition.field(LOAD_FILE_PREFIX + header) for header in index.headers]
    known = {f.key: f for f in (*definition.fields, *(f for f in produced if f is not None))}
    for key in (*choice.columns, *choice.item_fields):
        if key not in known and (resolved := definition.field(key)) is not None:
            known[key] = resolved
    labels = {group.key: group.label for group in definition.groups} | {LOAD_FILE_GROUP: "Load file"}

    def options(selected) -> list[dict]:
        ordered = [known[key] for key in selected if key in known]
        ordered += [f for f in known.values() if f.key not in selected]
        return [{"key": f.key, "label": f.label, "group": labels[f.group], "checked": f.key in selected,
                 "note": "emails only" if f.applies_to == "email" else ""} for f in ordered]

    return {"columns": options(choice.columns), "item_fields": options(choice.item_fields),
            "origin": choice.origin, "max_columns": definition.max_columns}
