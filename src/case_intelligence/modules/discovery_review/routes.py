"""Mount the production document list, the open item and the view chooser."""
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from case_intelligence.modules import ModuleContext
from case_intelligence.modules.discovery_review import fields, presentation, reader
from case_intelligence.modules.discovery_review.identifiers import (
    IdentifierConflict, IdentifierRepository, IdentifierUnavailable, RangeUnavailable, produced_collisions, sequence,
)
from case_intelligence.modules.discovery_review.views import SCOPES, ViewRepository, ViewUnavailable

TOKEN = "^[0-9a-f]{32}$"
VIEW = "^(|document|text|fields)$"


def route_hook(app, *, context: ModuleContext, authorized_matter, auth_context,
               require_csrf, require_csrf_header, templates, base_context, audit,
               require_response_lease, transfer_response_lease):
    fields.load_definition()  # a malformed field definition stops startup, not a reviewer mid-case
    repository = ViewRepository(context.db, context.membership)
    identifiers = IdentifierRepository(context.db, context.membership)

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
        try:
            index = reader.index_for(matter.matter_id, store, definition, identifiers)
        except reader.IndexUnavailable as exc:
            raise HTTPException(404, "Matter is no longer available") from exc
        except reader.MetadataUnavailable as exc:
            raise HTTPException(422, str(exc)) from exc
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
        sort: str = Query("", max_length=fields.MAX_FIELD_KEY_CHARS),
        dir: str = Query("", pattern="^(|asc|desc)$"),
        page: int = Query(1, ge=1, le=1_000_000),
        doc: str = Query("", pattern="^(|[0-9a-f]{32})$"),
        edge: str = Query("", pattern="^(|last)$"),
        view: str = Query("", pattern=VIEW),
        unit: int = Query(1, ge=1, le=1_000_000),
        all_columns: str = Query("", pattern="^(|1)$"),
        notice: str = Query("", pattern="^(|saved|shared|reset|unavailable|assigned|cleared|conflict|nothing|invalid|confirm|range|retry)$"),
    ):
        matter, actor, definition, store, index, choice = current(request, slug, show_all=bool(all_columns))
        base = f"/matters/{matter.slug}/production"
        listing = presentation.list_page(
            definition, index, choice, base=base, q=q, sort=sort, descending=dir == "desc",
            page=page, doc=doc, edge=edge, show_all=bool(all_columns), view=view)
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
        except KeyError as exc:
            raise HTTPException(404, "Matter is no longer available") from exc
        except ViewUnavailable:
            outcome = "unavailable"
        action = {"saved": "view_save", "shared": "view_share", "reset": f"view_reset_{scope}"}.get(outcome, "view_save")
        audit(request, f"discovery_review.{action}", "failure" if outcome == "unavailable" else "success",
              context=auth_context(request), matter=matter, object_type="matter", object_id=matter.matter_id)
        membership(matter, actor)
        return RedirectResponse(f"/matters/{matter.slug}/production?notice={outcome}", status_code=303,
                                headers={"Cache-Control": "no-store"})

    @app.post("/matters/{slug}/production/identifiers", dependencies=[Depends(require_csrf), Depends(require_response_lease)])
    def assign_production_identifiers(
        request: Request, slug: str,
        prefix: str = Form("", max_length=40), start: int = Form(1, ge=0, le=10**12),
        digits: int = Form(6, ge=1, le=12), include_produced: str = Form(""),
        action: str = Form("assign"), confirm: str = Form(""),
        q: str = Form("", max_length=presentation.MAX_QUERY_CHARS),
        sort: str = Form("", max_length=fields.MAX_FIELD_KEY_CHARS), dir: str = Form(""),
    ):
        matter, actor, definition, store, index, choice = current(request, slug)
        if action not in {"assign", "clear"} or not definition.heading_fallback:
            raise HTTPException(400, "Choose what to do with team identifiers")
        own, produced = definition.heading_fallback, definition.heading_range
        listed = [index.items[p] for p in presentation.ordered(
            definition, index, choice, q=q, sort=sort, descending=dir == "desc")]
        count = 0
        try:
            if action == "clear":
                marked = [item.document_id for item in listed if item.value(own)]
                outcome = "confirm" if confirm != "1" else "cleared" if marked else "nothing"
                if outcome == "cleared":
                    count = identifiers.clear(matter.matter_id, actor, marked)
            else:
                targets = [item for item in listed if not item.value(own)
                           and (include_produced or not item.value(produced[0]))]
                if not targets:
                    outcome = "nothing"
                else:
                    try:
                        labels = sequence(prefix.strip(), start, digits, len(targets))
                    except ValueError:
                        labels = None
                    if labels is None:
                        outcome = "invalid"
                    elif produced_collisions(index, definition, labels):
                        outcome = "conflict"
                    else:
                        with store.mutation_guard():
                            if store.revision != index.revision:
                                outcome = "retry"
                            else:
                                count = identifiers.assign(matter.matter_id, actor, zip(
                                    (item.document_id for item in targets), labels))
                                outcome = "assigned"
        except KeyError as exc:
            raise HTTPException(404, "Matter is no longer available") from exc
        except RangeUnavailable:
            outcome = "range"
        except IdentifierConflict:
            outcome = "conflict"
        except IdentifierUnavailable:
            outcome = "unavailable"
        if outcome in {"assigned", "cleared", "unavailable"}:
            audit(request, "discovery_review.identifier_" + ("clear" if action == "clear" else "assign"),
                  "failure" if outcome == "unavailable" else "success", context=auth_context(request),
                  matter=matter, object_type="matter", object_id=matter.matter_id, details={"count": count})
        membership(matter, actor)
        query = {key: value for key, value in (("q", q.strip()), ("sort", sort), ("dir", dir), ("notice", outcome)) if value}
        return RedirectResponse(f"/matters/{matter.slug}/production?{urlencode(query)}", status_code=303,
                                headers={"Cache-Control": "no-store"})
