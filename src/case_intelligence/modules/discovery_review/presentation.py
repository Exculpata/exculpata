"""Shape the review list, the open item and the view chooser for templates."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from case_intelligence.modules.discovery_review.fields import (
    LOAD_FILE_GROUP, LOAD_FILE_PREFIX, Definition, Field,
)
from case_intelligence.modules.discovery_review.reader import Index, Item

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
    if definition.hide_empty_default_columns and index.items and not show_all:
        hidden = tuple(key for key in columns if key not in index.filled)
        columns = tuple(key for key in columns if key in index.filled) or columns
    return Choice(columns, definition.default_item_fields, "default", hidden if len(hidden) < len(definition.default_columns) else ())


def applies(definition: Definition, chosen: Field, item: Item) -> bool:
    """Type-specific fields exist only on items of that type."""
    group = next((g for g in definition.groups if g.key == chosen.group), None)
    needs = chosen.applies_to or (group.applies_to if group else "")
    return not needs or needs == item.kind


def cell(definition: Definition, chosen: Field, item: Item) -> dict:
    if not applies(definition, chosen, item):
        return {"value": "", "state": "na", "note": f"Shown only for {chosen.applies_to or 'matching'} items"}
    if not item.has(chosen.key):
        return {"value": "", "state": "absent", "note": "This item's load file has no such field"}
    value = item.value(chosen.key)
    return {"value": value, "state": "value" if value else "empty", "note": ""}


def heading(definition: Definition, item: Item) -> dict:
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
              descending: bool, page: int, doc: str, edge: str, show_all: bool) -> dict:
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
    state = {"q": q.strip(), "sort": sort if sort != definition.default_sort or descending else "",
             "dir": "desc" if descending else "", "all_columns": "1" if show_all else ""}

    def url(**changes) -> str:
        query = {key: value for key, value in {**state, **changes}.items() if value}
        return base + ("?" + urlencode(query) if query else "")

    def item_url(position) -> str:
        return url(doc=index.items[position].token)

    rows = [{"item": index.items[p], "heading": heading(definition, index.items[p]), "url": item_url(p),
             "selected": where is not None and positions[where] == p,
             "cells": [cell(definition, column, index.items[p]) for column in columns]} for p in window]
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


def item_panel(definition: Definition, item: Item, choice: Choice, store, *, slug: str, view: str,
               unit: int, list_url: str) -> dict:
    available = [key for key, offered in (("document", bool(item.embedded)), ("text", item.has_text),
                                          ("fields", True)) if offered]
    active = view if view in available else available[0]
    source_url = f"/matters/{slug}/sources/{item.token}"
    fragment = f"/matters/{slug}/production/items/{item.token}"
    joiner = "&" if "?" in list_url else "?"

    def links(**query) -> dict:
        encoded = urlencode({key: value for key, value in query.items() if value})
        return {"url": f"{list_url}{joiner}{encoded}", "fragment": f"{fragment}?{encoded}"}

    panel = {
        "item": item, "heading": heading(definition, item), "active": active, "source_url": source_url,
        "kind": item.kind, "fragment": fragment,
        "views": [{"key": key, "label": label, "current": key == active, **links(view=key)}
                  for key, label in ITEM_VIEWS if key in available],
        "unavailable": [label for key, label in ITEM_VIEWS if key not in available],
    }
    if active == "document":
        panel["embed"] = {"kind": item.embedded, "url": f"{source_url}/content"}
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
    return panel


def item_fields(definition: Definition, item: Item, choice: Choice) -> dict:
    """Group the chosen fields first; every other field stays one step away."""
    chosen = [f for f in (definition.field(key) for key in choice.item_fields) if f is not None]
    rest = [f for f in definition.fields if f.key not in choice.item_fields]

    def groups(candidates) -> list[dict]:
        shaped = []
        for group in definition.groups:
            members = [f for f in candidates if f.group == group.key and applies(definition, f, item)]
            filled = [{"label": f.label, "value": item.value(f.key)} for f in members if item.value(f.key)]
            empty = [f.label for f in members if not item.value(f.key)]
            if filled or empty:
                shaped.append({"label": group.label, "rows": filled, "empty": empty})
        return shaped

    extra = [f for f in chosen if f.group == LOAD_FILE_GROUP]
    produced = [{"label": header, "value": value} for header, value in item.load_file.items()]
    return {
        "primary": groups(chosen), "more": groups(rest),
        "picked": [{"label": f.label, "value": item.value(f.key), "absent": not item.has(f.key)} for f in extra],
        "load_file": produced, "load_file_open": definition.show_load_file_fields_by_default,
        "load_file_source": item.values.get("load_file", ""),
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
