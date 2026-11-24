"""Mount the production document list, the open item and the view chooser."""
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from case_intelligence.modules import ModuleContext
from case_intelligence.modules.discovery_review import code_routes, fields, presentation, reader, tiff_preview
from case_intelligence.modules.discovery_review.codes import CodeRepository
from case_intelligence.modules.discovery_review.stored_index import Service
from case_intelligence.modules.discovery_review.stored_index import query as stored_query
from case_intelligence.modules.discovery_review.identifiers import (
    IdentifierConflict, IdentifierRepository, IdentifierUnavailable, RangeUnavailable, produced_collisions, sequence,
)
from case_intelligence.modules.discovery_review.views import SCOPES, ViewRepository, ViewUnavailable
from case_intelligence.modules.discovery_review.production import ProductionService, ProductionStale
from case_intelligence.pilot_uploads import UploadProblem

TOKEN = "^[0-9a-f]{32}$"
VIEW = "^(|document|text|fields)$"
MAX_POSITION = 9_223_372_036_854_775_807
RECORD = re.compile(r'(dat|opt):([0-9a-f]{32}):([1-9][0-9]{0,18})')


@dataclass(frozen=True, slots=True)
class RecordCursor:
    kind: str
    attempt_id: str
    ordinal: int


def parse_record_cursor(value: str, direction: str, view: str, image_start: int | None = None) -> RecordCursor | None:
    """Validate the entire locator before any source or database work."""
    if image_start is not None and (not value.startswith("dat:") or view != "fields" or direction != "at"):
        raise HTTPException(422, "Select a DII document record to inspect its listed image files.")
    if direction not in {'at', 'next', 'previous'}:
        raise HTTPException(422, 'Choose a valid record direction.')
    if not value:
        if direction != 'at':
            raise HTTPException(422, 'A record direction requires a record locator.')
        return None
    matched = RECORD.fullmatch(value) if len(value) <= 56 else None
    if matched is None or int(matched[3]) > MAX_POSITION:
        raise HTTPException(422, 'The record locator is invalid.')
    if view != ('fields' if matched[1] == 'dat' else 'document'):
        raise HTTPException(422, 'Choose Fields for document records or Document for page records.')
    return RecordCursor(matched[1], matched[2], int(matched[3]))


def route_hook(app, *, context: ModuleContext, authorized_matter, auth_context,
               require_csrf, require_csrf_header, templates, base_context, audit,
               require_response_lease, transfer_response_lease):
    fields.load_definition()  # a malformed field definition stops startup, not a reviewer mid-case
    repository = ViewRepository(context.db, context.membership)
    identifiers = IdentifierRepository(context.db, context.membership)
    codes = CodeRepository(context.db)
    production = ProductionService(context)
    stored = Service(context, identifiers, codes, production=production)

    def membership(matter, actor):
        try:
            context.membership(matter.matter_id, actor)
        except KeyError as exc:
            raise HTTPException(404, "Matter is no longer available") from exc

    def current(request, slug, *, show_all=False, complete=False):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        membership(matter, actor)
        definition = fields.load_definition()
        store = context.source_store(matter)
        try:
            bounded = stored.current(matter.matter_id, store, definition, matter=matter)
            if complete:
                index = reader.index_for(matter.matter_id, store, definition, identifiers,
                    production_projection=lambda document_id, version_id: production.document_projection(
                        matter.matter_id, document_id, version_id), production_mark=production.revision(matter.matter_id))
                index = codes.project(matter.matter_id, index)
            else:
                index = bounded
        except reader.IndexUnavailable as exc:
            raise HTTPException(404, "Matter is no longer available") from exc
        except reader.MetadataUnavailable as exc:
            raise HTTPException(422, str(exc)) from exc
        choice = presentation.choose(definition, repository.read(matter.matter_id, actor), index,
                                     show_all=show_all)
        return matter, actor, definition, store, index, choice

    def production_panel(matter, selected, view, cursor, direction, image_start):
        try:
            projection = production.document_projection(
                matter.matter_id, selected.document_id, selected.version_id)
            if cursor is None and projection.get('state') in {'pending', 'paused'}:
                return projection, None
            references = tuple(projection.get('references', ()))
            if cursor is not None:
                attempt_id = cursor.attempt_id
                category = 'document' if cursor.kind == 'dat' else 'page'
                ordinal = cursor.ordinal
            else:
                category = 'page' if view == 'document' or (
                    not view and any(ref['category'] == 'page' for ref in references)) else 'document'
                reference = next((ref for ref in references if ref['category'] == category), None)
                if reference is None or view == 'text':
                    return projection, None
                counts = reference.get('counts', {})
                if not (counts.get('accepted', 0) + counts.get('rejected', 0)):
                    return projection, None
                if (reference.get('source_format') in {'csv', 'tsv'}
                        and reference['source_id'] != selected.document_id):
                    return projection, None
                if (projection.get('state') != 'complete'
                        and reference['source_id'] != selected.document_id):
                    return projection, None
                attempt_id, ordinal = reference['attempt_id'], 0
            window = production.record_window(
                matter.matter_id, attempt_id, category=category, ordinal=ordinal,
                direction=direction, document_id=selected.document_id, image_start=image_start,
                limit=1 if category == 'page' else 100)
            return projection, window
        except ProductionStale as exc:
            if cursor is None:
                return {'state': 'pending', 'values': {}, 'raw_fields': (), 'references': ()}, None
            raise HTTPException(409, 'These production records changed. Reload the list.') from exc
        except KeyError as exc:
            raise HTTPException(404, 'This production record is not available for this item.') from exc
        except ValueError as exc:
            raise HTTPException(422, 'The production record request is invalid.') from exc

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
        sort: str = Query("", max_length=fields.MAX_KEY_CHARS),
        dir: str = Query("", pattern="^(|asc|desc)$"),
        page: int = Query(1, ge=1, le=MAX_POSITION),
        cursor: str = Query("", max_length=4096),
        doc: str = Query("", pattern="^(|[0-9a-f]{32})$"),
        edge: str = Query("", pattern="^(|last)$"),
        view: str = Query("", pattern=VIEW),
        unit: int = Query(1, ge=1, le=MAX_POSITION),
        record: str = Query('', max_length=56),
        record_dir: str = Query('at', pattern='^(at|next|previous)$'),
        image_start: int | None = Query(None, ge=1, le=MAX_POSITION),
        tiff_frame: int = Query(1, ge=1, le=tiff_preview.MAX_TIFF_FRAMES),
        reload_attempt: str = Query('', pattern='^(|[0-9a-f]{32})$'),
        reload_token: str = Query('', pattern='^(|[0-9a-f]{32})$'),
        all_columns: str = Query("", pattern="^(|1)$"),
        notice: str = Query("", pattern="^(|saved|shared|reset|unavailable|assigned|cleared|conflict|nothing|invalid|confirm|range|retry)$"),
    ):
        record_cursor = parse_record_cursor(record, record_dir, view, image_start)
        if record_cursor is not None and not doc:
            raise HTTPException(422, 'Select an item before opening a production record.')
        matter, actor, definition, store, index, choice = current(request, slug, show_all=bool(all_columns))
        base = f"/matters/{matter.slug}/production"
        if reload_attempt and reload_token:
            production.resume(matter.matter_id, reload_attempt, reload_token)
            values = dict(request.query_params)
            values.pop('reload_attempt', None)
            values.pop('reload_token', None)
            return RedirectResponse(base + '?' + urlencode(values), status_code=303,
                                    headers={'Cache-Control': 'no-store'})
        listing = stored_query.list_page(
            definition, index, choice, base=base, q=q, sort=sort, descending=dir == "desc",
            page=page, doc=doc, edge=edge, show_all=bool(all_columns), cursor=cursor, selected_view=view, unit=unit)
        selected = listing["selected"]
        if record_cursor is not None and (selected is None or selected.token != doc):
            raise HTTPException(404, 'This item is not available in the current list.')
        state = {key: value for key, value in listing["state"].items() if value}
        projection, window = production_panel(matter, selected, view, record_cursor, record_dir, image_start) if selected else ({}, None)
        panel = presentation.item_panel(
            definition, selected, choice, store, slug=matter.slug, view=view, unit=unit,
            list_url=base + "?" + urlencode({**state, "doc": selected.token}),
            production=projection, record_window=window, tiff_frame=tiff_frame) if selected else None
        if panel is not None:
            panel["codes"] = codes.controls(matter.matter_id, selected.document_id)
        return respond(request, matter, actor, "workbench_production_documents.html", {
            "codes": codes.list(matter.matter_id),
            "document_count": index.total, "listing": listing, "panel": panel,
            "production_paused": [{**paused, 'url': base + '?' + urlencode({
                **state, 'reload_attempt': paused['attempt_id'], 'reload_token': paused['reload']})}
                for paused in production.paused.get(matter.matter_id, {}).values()],
            "production_coverage": index.summaries.get("production", {}).get("counts", {}),
            "chooser": presentation.chooser(definition, index, choice), "load_files": index.reports,
            "notice": notice, "base_url": base, "index_summary": index.summaries,
        })

    @app.get("/matters/{slug}/production/items/{token}", dependencies=[Depends(require_response_lease)])
    def production_document_item(
        request: Request, slug: str, token: str,
        view: str = Query("", pattern=VIEW), unit: int = Query(1, ge=1, le=MAX_POSITION),
        record: str = Query('', max_length=56),
        record_dir: str = Query('at', pattern='^(at|next|previous)$'),
        image_start: int | None = Query(None, ge=1, le=MAX_POSITION),
        tiff_frame: int = Query(1, ge=1, le=tiff_preview.MAX_TIFF_FRAMES),
        q: str = Query("", max_length=presentation.MAX_QUERY_CHARS),
        sort: str = Query("", max_length=fields.MAX_KEY_CHARS),
        dir: str = Query("", pattern="^(|asc|desc)$"),
        all_columns: str = Query("", pattern="^(|1)$"),
    ):
        record_cursor = parse_record_cursor(record, record_dir, view, image_start)
        matter, actor, definition, store, index, choice = current(request, slug)
        try:
            item = stored_query.lookup(index, token)
        except KeyError as exc:
            raise HTTPException(404, "This item is no longer in the matter. Reload the list.") from exc
        base = f"/matters/{matter.slug}/production"
        projection, window = production_panel(matter, item, view, record_cursor, record_dir, image_start)
        panel = presentation.item_panel(definition, item, choice, store, slug=matter.slug,
                                        view=view, unit=unit, list_url=base + "?" + urlencode({
                                            "doc": token, "q": q.strip(), "dir": dir, "all_columns": all_columns,
                                            "sort": sort if definition.field(sort) is not None else ""}),
                                        production=projection, record_window=window, tiff_frame=tiff_frame)
        panel["codes"] = codes.controls(matter.matter_id, item.document_id)
        return respond(request, matter, actor, "workbench_production_document_item.html", {"panel": panel})

    @app.get("/matters/{slug}/production/tiff-preview/{token}", dependencies=[Depends(require_response_lease)])
    def production_tiff_preview(
        request: Request, slug: str, token: str,
        frame: int = Query(1, ge=1, le=tiff_preview.MAX_TIFF_FRAMES),
    ):
        if re.fullmatch(TOKEN, token) is None:
            raise HTTPException(404, "This source is not available.")
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        membership(matter, actor)
        store = context.source_store(matter)
        with store.mutation_guard():
            try:
                document = store.get_by_action_token(token)
            except KeyError as exc:
                raise HTTPException(404, "This source is not available.") from exc
            if document.origin != "upload" or document.state != "ready":
                raise HTTPException(404, "This source is not available for preview.")
            if document.media_type != "image/tiff":
                raise HTTPException(415, "TIFF preview applies only to TIFF images.")
            try:
                path = store.source_path(document.document_id, verify_digest=False)
                raw = tiff_preview.read_verified_tiff_bytes(path, document.digest)
            except (UploadProblem, tiff_preview.TiffPreviewStale) as exc:
                raise HTTPException(409, "The stored TIFF original changed or is unavailable.") from exc
            except tiff_preview.TiffPreviewError as exc:
                raise HTTPException(422, str(exc)) from exc
        try:
            png, total_frames = tiff_preview.render_tiff_frame_bytes(raw, frame=frame)
        except tiff_preview.TiffPreviewError as exc:
            raise HTTPException(422, str(exc)) from exc
        membership(matter, actor)
        response = Response(
            content=png,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
                "X-Tiff-Frame": str(frame),
                "X-Tiff-Frames": str(total_frames),
            },
        )
        return transfer_response_lease(request, response)

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
        sort: str = Form("", max_length=fields.MAX_KEY_CHARS), dir: str = Form(""),
    ):
        matter, actor, definition, store, index, choice = current(request, slug, complete=True)
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

    code_routes.mount(app, context=context, current=lambda request, slug: current(request, slug, complete=True),
                      codes=codes, require_csrf=require_csrf, committed=stored.invalidate)
