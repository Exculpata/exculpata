"""Ordinary member-authorized form posts for matter codes and item events."""
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from case_intelligence.modules.discovery_review import fields, presentation
from case_intelligence.modules.discovery_review.codes import CodeUnavailable, MAX_LABEL


def mount(app, *, context, current, codes, require_csrf):
    def change(request, slug, action, *, token="", state=None):
        try:
            matter, actor, definition, store, index, choice = current(request, slug)
            with context.db.lock:
                context.membership(matter.matter_id, actor)
                item = None
                if token:
                    position = index.by_token.get(token)
                    if position is None:
                        raise KeyError(token)
                    item = index.items[position]
                action(matter, actor, item)
        except KeyError as exc:
            raise HTTPException(404, "This item or code is no longer in the matter.") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except CodeUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc
        state = dict(state or {})
        if state.get("sort") and definition.field(state["sort"]) is None:
            state["sort"] = ""
        query = urlencode({key: value for key, value in {**state, "doc": token}.items() if value})
        return RedirectResponse(f"/matters/{matter.slug}/production?{query}", status_code=303,
                                headers={"Cache-Control": "no-store"})

    @app.post("/matters/{slug}/production/codes", dependencies=[Depends(require_csrf)])
    def define_production_code(request: Request, slug: str, label: str = Form("", max_length=MAX_LABEL),
                               digit: str = Form("", pattern="^[1-9]?$")):
        return change(request, slug, lambda matter, actor, item: codes.define(
            matter.matter_id, actor, label, digit=int(digit) if digit else None))

    @app.post("/matters/{slug}/production/codes/{code_id}/retire", dependencies=[Depends(require_csrf)])
    def retire_production_code(request: Request, slug: str, code_id: str):
        return change(request, slug, lambda matter, actor, item: codes.retire(matter.matter_id, code_id))

    @app.post("/matters/{slug}/production/items/{token}/codes/{code_id}", dependencies=[Depends(require_csrf)])
    def record_production_code(request: Request, slug: str, token: str, code_id: str,
                               action: str = Form(""), view: str = Form("", pattern="^(|document|text|fields)$"),
                               q: str = Form("", max_length=presentation.MAX_QUERY_CHARS),
                               sort: str = Form("", max_length=fields.MAX_HEADER_CHARS + 3),
                               dir: str = Form("", pattern="^(|asc|desc)$"),
                               unit: int = Form(1, ge=1, le=1_000_000),
                               all_columns: str = Form("", pattern="^(|1)$")):
        return change(request, slug, lambda matter, actor, item: codes.record(
            matter.matter_id, item.document_id, code_id, actor, action), token=token, state={
                "q": q.strip(), "sort": sort, "dir": dir, "view": view, "unit": unit, "all_columns": all_columns})
