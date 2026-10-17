"""Authorized draft timeline adapters; source and access checks remain live."""
from datetime import datetime, timezone
from urllib.parse import urlencode
from fastapi import BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from .timeline_builder import build_timeline
from .timeline_exports import export_timeline
from .timeline_jobs import TimelineJobs
from .timeline_promotion import promote_entry
from .workspace_store import WorkspaceProblem


def install_timeline_routes(app, *, bench, authorized_matter, auth_context, require_csrf,
                            templates, base_context, audit, require_response_lease, transfer_response_lease):
    jobs = TimelineJobs()

    def snapshot(matter, actor):
        # Resolve cache before entering source/workspace locks (see DEVELOPMENT).
        store = bench.source_store(matter)
        service = bench.assertion_service(matter)
        with service.entities.source_guard(), service.repository.transaction(matter.matter_id, actor):
            units = []
            total_unit_count = 0
            for document in store.ready_documents():
                # Count the live population without retaining all passage text or
                # constructing candidates beyond the existing collection ceiling.
                for ordinal, unit in enumerate(document.iter_parsed_units(), 1):
                    total_unit_count += 1
                    if len(units) < 2501:
                        units.append(bench._candidate(matter, document, unit, ordinal))
            payload = build_timeline(units, support_token=bench._support_token,
                                     total_unit_count=total_unit_count).payload()
            payload['generated_at'] = datetime.now(timezone.utc).isoformat()
            return payload

    def access(request, slug):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        bench.workspace.membership(matter.matter_id, actor)
        return matter, actor

    def current(request, slug):
        try:
            matter, actor = access(request, slug)
            return matter, actor, snapshot(matter, actor)
        except KeyError as exc:
            raise HTTPException(404, 'Matter or source is no longer available') from exc

    @app.get('/matters/{slug}/timeline/draft')
    def draft(request: Request, slug: str, q: str = Query('', max_length=200), page: int = Query(1, ge=1, le=100000)):
        matter, actor, payload = current(request, slug)
        service = bench.assertion_service(matter)
        try:
            choices, total = service.entities.list(matter.matter_id, actor, query=q, page=page)
            response = templates.TemplateResponse(request=request, name='workbench_timeline_draft.html', context={
                **base_context(request, matter), 'matter': matter, 'timeline': payload, 'choices': choices,
                'choice_total': total, 'q': q, 'page': page, 'refresh_state': jobs.state(matter.matter_id, actor),
                'show_assistant_dock': False,
                'source_url': lambda c: f'/matters/{slug}?' + urlencode(dict(support=c['support_token'], entity_return_to=f'/matters/{slug}/timeline/draft')) + '#support-pane',
            }, headers={'Cache-Control': 'no-store'})
            with service.repository.transaction(matter.matter_id, actor):
                pass
            return response
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc

    @app.post('/matters/{slug}/timeline/draft/refresh', dependencies=[Depends(require_csrf)])
    def refresh(request: Request, slug: str, background_tasks: BackgroundTasks):
        try:
            matter, actor = access(request, slug)
            jobs.submit(background_tasks, matter.matter_id, actor, lambda: snapshot(matter, actor))
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        except WorkspaceProblem as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f'/matters/{slug}/timeline/draft', status_code=303, headers={'Cache-Control':'no-store'})

    @app.get('/matters/{slug}/timeline/draft/export', dependencies=[Depends(require_response_lease)])
    def export(request: Request, slug: str, format: str = Query('json', pattern='^(json|csv|markdown)$')):
        matter, actor, payload = current(request, slug)
        body = export_timeline(payload, format)
        from .assertion_repository import MAX_STORAGE_BYTES
        if len(body.encode()) > MAX_STORAGE_BYTES:
            raise HTTPException(400, 'This export exceeds the payload limit. No partial export was produced.')
        try:
            with bench.assertion_service(matter).repository.transaction(matter.matter_id, actor):
                pass
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        audit(request, 'timeline.export', 'success', context=auth_context(request), matter=matter, object_type='matter', object_id=matter.matter_id)
        return transfer_response_lease(request, Response(body, media_type={'json':'application/json', 'csv':'text/csv', 'markdown':'text/markdown'}[format],
            headers={'Cache-Control':'no-store', 'Content-Disposition':f'attachment; filename="draft-timeline.{"md" if format == "markdown" else format}"', 'X-Review-Notice': payload['notice']}))

    @app.post('/matters/{slug}/timeline/draft/{entry_id}/promote', dependencies=[Depends(require_csrf)])
    def promote(request: Request, slug: str, entry_id: str, entity_selection: str = Form(..., max_length=120), role: str = Form('subject', max_length=24)):
        matter, actor, payload = current(request, slug)
        entry = next((e for e in payload['entries'] + payload['undated'] if e['entry_id'] == entry_id), None)
        if entry is None:
            raise HTTPException(404, 'Draft entry changed or is no longer available')
        service = bench.assertion_service(matter)
        try:
            entity_id, revision = entity_selection.rsplit(':', 1)
            revision = int(revision)
        except ValueError as exc:
            raise HTTPException(400, 'Select an entity and its displayed revision') from exc
        try:
            row = promote_entry(service, matter.matter_id, actor, entry,
                                entity_id=entity_id, revision=revision, role=role)
        except KeyError as exc:
            raise HTTPException(404, 'Entry, source or entity is no longer available') from exc
        except WorkspaceProblem as exc:
            raise HTTPException(409, str(exc)) from exc
        audit(request, 'timeline.promote', 'success', context=auth_context(request), matter=matter, object_type='assertion', object_id=row['assertion_id'])
        return RedirectResponse(f'/matters/{slug}/assertions/' + row['assertion_id'], status_code=303)
