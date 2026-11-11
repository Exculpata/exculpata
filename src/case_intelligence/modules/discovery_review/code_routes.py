"""Ordinary member-authorized form posts for matter codes and item events."""
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from case_intelligence.modules.discovery_review.codes import CodeUnavailable, MAX_LABEL


def mount(app, *, context, current, codes, require_csrf):
    def change(request, slug, action, *, token="", view=""):
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
        query = urlencode({"doc": token, "view": view})
        return RedirectResponse(f"/matters/{matter.slug}/production?{query}", status_code=303,
                                headers={"Cache-Control": "no-store"})

    @app.post("/matters/{slug}/production/codes", dependencies=[Depends(require_csrf)])
    def define_production_code(request: Request, slug: str, label: str = Form("", max_length=MAX_LABEL)):
        return change(request, slug, lambda matter, actor, item: codes.define(matter.matter_id, actor, label))

    @app.post("/matters/{slug}/production/codes/{code_id}/retire", dependencies=[Depends(require_csrf)])
    def retire_production_code(request: Request, slug: str, code_id: str):
        return change(request, slug, lambda matter, actor, item: codes.retire(matter.matter_id, code_id))

    @app.post("/matters/{slug}/production/items/{token}/codes/{code_id}", dependencies=[Depends(require_csrf)])
    def record_production_code(request: Request, slug: str, token: str, code_id: str,
                               action: str = Form(""), view: str = Form("", pattern="^(|document|text|fields)$")):
        return change(request, slug, lambda matter, actor, item: codes.record(
            matter.matter_id, item.document_id, code_id, actor, action), token=token, view=view)
