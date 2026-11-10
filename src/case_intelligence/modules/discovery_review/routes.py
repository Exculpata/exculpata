"""Mount the production document list, the open item and the view chooser."""
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from case_intelligence.modules import ModuleContext
from case_intelligence.modules.discovery_review import fields, presentation, reader
from case_intelligence.modules.discovery_review.views import SCOPES, ViewRepository, ViewUnavailable

TOKEN = "^[0-9a-f]{32}$"
VIEW = "^(|document|text|fields)$"


def route_hook(app, *, context: ModuleContext, authorized_matter, auth_context,
               require_csrf, require_csrf_header, templates, base_context, audit,
               require_response_lease, transfer_response_lease):
    fields.load_definition()  # a malformed field definition stops startup, not a reviewer mid-case
    repository = ViewRepository(context.db)

    def membership(matter, actor):
        try:
            context.membership(matter.matter_id, actor)
        except KeyError as exc:
            raise HTTPException(404, "Matter is no longer available") from exc

    def current(request, slug, *, show_all=False):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        membership(matter, actor)
        definition = fields.load_definition()
        store = context.source_store(matter)
        index = reader.index_for(matter.matter_id, store, definition)
        choice = presentation.choose(definition, repository.read(matter.matter_id, actor), index,
                                     show_all=show_all)
        return matter, actor, definition, store, index, choice

    def respond(request, matter, actor, name, values):
        response = templates.TemplateResponse(
            request=request, name=name,
            context={**base_context(request, matter), "matter": matter, "show_assistant_dock": False, **values},
            headers={"Cache-Control": "no-store"})
        membership(matter, actor)
        return transfer_response_lease(request, response)

    @app.get("/matters/{slug}/production", dependencies=[Depends(require_response_lease)])
    def production_documents(
        request: Request, slug: str,
        q: str = Query("", max_length=presentation.MAX_QUERY_CHARS),
        sort: str = Query("", max_length=fields.MAX_HEADER_CHARS + 3),
        dir: str = Query("", pattern="^(|asc|desc)$"),
        page: int = Query(1, ge=1, le=1_000_000),
        doc: str = Query("", pattern="^(|[0-9a-f]{32})$"),
        edge: str = Query("", pattern="^(|last)$"),
        view: str = Query("", pattern=VIEW),
        unit: int = Query(1, ge=1, le=1_000_000),
        all_columns: str = Query("", pattern="^(|1)$"),
        notice: str = Query("", pattern="^(|saved|shared|reset|unavailable)$"),
    ):
        matter, actor, definition, store, index, choice = current(request, slug, show_all=bool(all_columns))
        base = f"/matters/{matter.slug}/production"
        listing = presentation.list_page(
            definition, index, choice, base=base, q=q, sort=sort, descending=dir == "desc",
            page=page, doc=doc, edge=edge, show_all=bool(all_columns))
        selected = listing["selected"]
        state = {key: value for key, value in listing["state"].items() if value}
        panel = presentation.item_panel(
            definition, selected, choice, store, slug=matter.slug, view=view, unit=unit,
            list_url=base + "?" + urlencode({**state, "doc": selected.token})) if selected else None
        return respond(request, matter, actor, "workbench_production_documents.html", {
            "document_count": len(index.items), "listing": listing, "panel": panel,
            "chooser": presentation.chooser(definition, index, choice), "load_files": index.reports,
            "notice": notice, "base_url": base,
        })

    @app.get("/matters/{slug}/production/items/{token}", dependencies=[Depends(require_response_lease)])
    def production_document_item(
        request: Request, slug: str, token: str,
        view: str = Query("", pattern=VIEW), unit: int = Query(1, ge=1, le=1_000_000),
    ):
        matter, actor, definition, store, index, choice = current(request, slug)
        position = index.by_token.get(token)
        if position is None:
            raise HTTPException(404, "This item is no longer in the matter. Reload the list.")
        base = f"/matters/{matter.slug}/production"
        panel = presentation.item_panel(definition, index.items[position], choice, store, slug=matter.slug,
                                        view=view, unit=unit, list_url=f"{base}?doc={token}")
        return respond(request, matter, actor, "workbench_production_document_item.html", {"panel": panel})

    @app.post("/matters/{slug}/production/view", dependencies=[Depends(require_csrf)])
    def save_production_view(
        request: Request, slug: str,
        columns: list[str] = Form(default=[]), item_fields: list[str] = Form(default=[]),
        scope: str = Form("user"), action: str = Form("save"),
    ):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        membership(matter, actor)
        definition = fields.load_definition()
        if scope not in SCOPES or action not in {"save", "reset"} or len(columns) > 500 or len(item_fields) > 500:
            raise HTTPException(400, "Choose a view to save")
        chosen = definition.clean(columns, limit=definition.max_columns)
        try:
            if action == "reset" or not chosen:
                repository.clear(matter.matter_id, scope, actor)
                outcome = "reset"
            else:
                repository.save(matter.matter_id, scope, actor, chosen, definition.clean(item_fields))
                outcome = "shared" if scope == "matter" else "saved"
        except ViewUnavailable:
            outcome = "unavailable"
        membership(matter, actor)
        action = {"saved": "view_save", "shared": "view_share", "reset": f"view_reset_{scope}"}.get(outcome, "view_save")
        audit(request, f"discovery_review.{action}", "failure" if outcome == "unavailable" else "success",
              context=auth_context(request), matter=matter, object_type="matter", object_id=matter.matter_id)
        return RedirectResponse(f"/matters/{matter.slug}/production?notice={outcome}", status_code=303,
                                headers={"Cache-Control": "no-store"})
