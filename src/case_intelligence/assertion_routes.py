"""Authorized HTTP adapters for human-owned events and source assertions."""
import json
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from .assertion_repository import AssertionEditConflict
from .assertion_service import ROLE_TYPES as ROLES, ASSERTION_STATUSES as STATUSES
from .workspace_store import WorkspaceProblem
from .review_navigation import matter_return_path

# The longest return path the entity and chronology routes accept.
RETURN_LIMIT = 4000


def install_assertion_routes(app, *, service_for, entities_for, authorized_matter, auth_context,
                             require_csrf, templates, base_context, audit,
                             require_response_lease, transfer_response_lease, model_notice=lambda: ''):
    def safe_return(slug, value):
        return matter_return_path(slug, value, fallback=f'/matters/{slug}')

    def render(request, slug, *, assertion_id='', creating=False, entity_id='', q='', page=1,
               support='', return_to='', error='', draft=None, status_code=200, removed=False, dates_page=1):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        service, entities = service_for(matter), entities_for(matter)
        detail, entity, mentions = None, None, []
        try:
            if assertion_id and not removed:
                detail = service.detail(matter.matter_id, actor, assertion_id)
                detail['history'] = [dict(entry, snapshot=json.loads(entry['snapshot_json'])) for entry in detail['history']]
            if entity_id:
                entity, mentions, _, _ = entities.detail(matter.matter_id, actor, entity_id)
            records, total = service.list(matter.matter_id, actor, entity_id=entity_id, page=page)
            # The matter-wide timeline also shows a read-only draft of found dates.
            found_dates, found_total, found_ordered = (entities.date_draft(matter.matter_id, actor, page=dates_page)
                if not (assertion_id or creating or entity_id) else ([], 0, 0))
            choices, choice_total = entities.list(matter.matter_id, actor, query=q, page=page)
        except KeyError as exc:
            raise HTTPException(404, 'Record, entity or matter is no longer available') from exc
        passage = None
        if support:
            try:
                with entities.source_guard(), entities.repository.transaction(matter.matter_id, actor):
                    passage = entities.resolve_support(support)
            except (WorkspaceProblem, KeyError):
                error = error or 'This selected passage changed or is unavailable. Select a current original passage.'
        return_to = safe_return(slug, return_to)
        def record_url(identifier):
            return f'/matters/{slug}/assertions/{identifier}?' + urlencode(dict(support=support, return_to=return_to))
        def page_url(number):
            path = f'/matters/{slug}/assertions/{assertion_id}' if assertion_id else f'/matters/{slug}/chronology'
            # Paging saved records keeps the found-date draft's page too.
            kept = dict(dates_page=dates_page) if dates_page > 1 and not assertion_id else {}
            return path + '?' + urlencode(dict(entity_id=entity_id, q=q, page=number, support=support,
                                               return_to=return_to, **kept))
        def dates_page_url(number):
            return (f'/matters/{slug}/chronology?' + urlencode(dict(
                page=page, dates_page=number, support=support, return_to=return_to)) + '#found-dates-heading')
        # Reviewing a found date can return to this timeline position, keeping any selected
        # passage. A nested return path that would exceed the identity page's limit is dropped.
        timeline_here = f'/matters/{slug}/chronology?' + urlencode(dict(
            page=page, dates_page=dates_page, support=support, return_to=return_to)) + '#found-dates-heading'
        if len(timeline_here) > RETURN_LIMIT:
            timeline_here = f'/matters/{slug}/chronology?' + urlencode(dict(
                page=page, dates_page=dates_page, support=support)) + '#found-dates-heading'
        response = templates.TemplateResponse(request=request, name='workbench_assertions.html', context={
            **base_context(request, matter), 'matter': matter, 'detail': detail,
            'creating': creating, 'entity': entity, 'entity_id': entity_id, 'mentions': mentions,
            'records': records, 'total': total, 'page': page, 'q': q,
            'choices': choices, 'choice_total': choice_total, 'roles': ROLES, 'statuses': STATUSES,
            'passage': passage, 'support': support, 'return_to': return_to,
            'record_url': record_url, 'page_url': page_url, 'error': error, 'draft': draft,
            'found_dates': found_dates, 'found_total': found_total, 'found_ordered': found_ordered,
            'dates_page': dates_page, 'dates_page_url': dates_page_url, 'dates_page_size': 25,
            'timeline_here': timeline_here,
            'removed': removed, 'show_assistant_dock': False,
        }, status_code=status_code, headers={'Cache-Control': 'no-store'})
        try:
            with service.repository.transaction(matter.matter_id, actor):
                pass
        except KeyError as exc:
            raise HTTPException(404, 'Record or matter is no longer available') from exc
        return response

    @app.get('/matters/{slug}/chronology')
    def chronology(request: Request, slug: str, entity_id: str = Query('', max_length=80),
                   page: int = Query(1, ge=1, le=100000), support: str = Query('', max_length=40),
                   return_to: str = Query('', max_length=4000), dates_page: int = Query(1, ge=1, le=100000)):
        return render(request, slug, entity_id=entity_id, page=page, support=support, return_to=return_to,
                      dates_page=dates_page)

    @app.get('/matters/{slug}/assertions/new')
    def new_assertion(request: Request, slug: str, entity_id: str = Query('', max_length=80),
                      support: str = Query('', max_length=40), return_to: str = Query('', max_length=4000)):
        return render(request, slug, creating=True, entity_id=entity_id, support=support, return_to=return_to)

    @app.get('/matters/{slug}/assertions/{assertion_id}')
    def assertion(request: Request, slug: str, assertion_id: str, q: str = Query('', max_length=200),
                  page: int = Query(1, ge=1, le=100000), support: str = Query('', max_length=40),
                  return_to: str = Query('', max_length=4000)):
        return render(request, slug, assertion_id=assertion_id, q=q, page=page, support=support, return_to=return_to)

    @app.post('/matters/{slug}/assertions/actions', dependencies=[Depends(require_csrf)])
    def action(request: Request, slug: str, action: str = Form(..., max_length=24),
               assertion_id: str = Form('', max_length=80), expected_revision: int = Form(0, ge=0),
               record_type: str = Form('assertion', max_length=24), title: str = Form('', max_length=160),
               statement: str = Form('', max_length=6000), raw_date: str = Form('', max_length=200),
               date_uncertainty: str = Form('', max_length=1000), sort_date: str = Form('', max_length=10),
               status: str = Form('needs_review', max_length=24), support: str = Form('', max_length=40),
               attributed_to: str = Form('', max_length=200), stance: str = Form('supporting', max_length=24),
               entity_id: str = Form('', max_length=80), entity_revision: int = Form(0, ge=0),
               entity_selection: str = Form('', max_length=120), role: str = Form('subject', max_length=24),
               role_id: str = Form('', max_length=80), account_id: str = Form('', max_length=80),
               q: str = Form('', max_length=200), return_to: str = Form('', max_length=4000)):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        service = service_for(matter)
        fields = dict(record_type=record_type, title=title, statement=statement, raw_date=raw_date,
                      date_uncertainty=date_uncertainty, sort_date=sort_date, status=status)
        draft = dict(fields, action=action, assertion_id=assertion_id, expected_revision=expected_revision,
                     support=support, attributed_to=attributed_to, stance=stance, entity_id=entity_id,
                     entity_revision=entity_revision, entity_selection=entity_selection, role=role,
                     role_id=role_id, account_id=account_id)
        try:
            if action == 'create':
                row = service.create(matter.matter_id, actor, **fields, support=support, attributed_to=attributed_to,
                    roles=[dict(entity_id=entity_id, expected_revision=entity_revision, role=role)])
                assertion_id = row['assertion_id']
            elif action == 'update':
                service.update(matter.matter_id, actor, assertion_id, expected_revision=expected_revision, **fields)
            elif action == 'attach':
                service.attach(matter.matter_id, actor, assertion_id, expected_revision=expected_revision,
                               support=support, stance=stance, attributed_to=attributed_to)
            elif action == 'revise_account':
                service.revise_account(matter.matter_id, actor, assertion_id, expected_revision=expected_revision,
                                       account_id=account_id, stance=stance, attributed_to=attributed_to)
            elif action == 'remove_account':
                service.remove_account(matter.matter_id, actor, assertion_id, expected_revision=expected_revision, account_id=account_id)
            elif action in ('add_role', 'revise_role'):
                try:
                    selected_id, selected_revision = entity_selection.rsplit(':', 1)
                    selected_revision = int(selected_revision)
                except (ValueError, TypeError) as exc:
                    raise WorkspaceProblem('Select an entity and its displayed revision.') from exc
                action = 'revise_role' if role_id else 'add_role'
                operation = service.revise_role if role_id else service.add_role
                extra = dict(role_id=role_id) if role_id else {}
                operation(matter.matter_id, actor, assertion_id, expected_revision=expected_revision,
                          entity_id=selected_id, entity_revision=selected_revision, role=role, **extra)
            elif action == 'remove_role':
                service.remove_role(matter.matter_id, actor, assertion_id, expected_revision=expected_revision, role_id=role_id)
            elif action == 'delete':
                service.delete(matter.matter_id, actor, assertion_id, expected_revision=expected_revision)
            else:
                raise WorkspaceProblem('Choose an available record action.')
        except (WorkspaceProblem, KeyError) as exc:
            # Reauthorize before returning any saved record or submitted draft.
            try:
                with service.repository.transaction(matter.matter_id, actor):
                    pass
            except KeyError as denied:
                raise HTTPException(404, 'Record or matter is no longer available') from denied
            from .entity_repository import EntityEditConflict
            code = 409 if isinstance(exc, (AssertionEditConflict, EntityEditConflict, KeyError)) else 400
            error = str(exc) if isinstance(exc, WorkspaceProblem) else 'The selected record, entity or original is no longer available. Your unsaved inputs are preserved.'
            try:
                return render(request, slug, assertion_id=assertion_id, creating=action == 'create',
                              entity_id=entity_id if action == 'create' else '', q=q, support=support,
                              return_to=return_to, error=error, draft=draft, status_code=code)
            except HTTPException as gone:
                if gone.status_code != 404:
                    raise
                return render(request, slug, q=q, return_to=return_to, error=error, draft=draft,
                              removed=True, status_code=409)
        audit(request, 'assertion.' + action, 'success', context=auth_context(request), matter=matter,
              object_type='assertion', object_id=assertion_id)
        path = f'/matters/{slug}/chronology' if action == 'delete' else f'/matters/{slug}/assertions/{assertion_id}'
        return RedirectResponse(path + '?' + urlencode(dict(return_to=safe_return(slug, return_to),
            support=support if action not in ('create', 'attach', 'delete') else '')), status_code=303)

    def export_response(request, matter, payload, format_name, filename, *, object_type, object_id):
        from .assertion_exports import markdown_export, qualified_payload
        payload = qualified_payload(payload, model_notice())
        body = json.dumps(payload, indent=2) if format_name == 'json' else markdown_export(payload)
        from .assertion_repository import MAX_STORAGE_BYTES
        if len(body.encode('utf-8')) > MAX_STORAGE_BYTES:
            raise HTTPException(400, 'This export exceeds the payload limit. No partial export was produced.')
        try:
            with service_for(matter).repository.transaction(matter.matter_id, auth_context(request).principal_id):
                pass
        except KeyError as exc:
            raise HTTPException(404, 'Record or matter is no longer available') from exc
        audit(request, 'assertion.export', 'success', context=auth_context(request), matter=matter,
              object_type=object_type, object_id=object_id)
        return transfer_response_lease(request, Response(body, media_type='application/json' if format_name == 'json' else 'text/markdown',
            headers={'Content-Disposition': f'attachment; filename="{filename}.{ "json" if format_name == "json" else "md"}"', 'Cache-Control': 'no-store'}))

    @app.get('/matters/{slug}/assertions/{assertion_id}/export', dependencies=[Depends(require_response_lease)])
    def export_assertion(request: Request, slug: str, assertion_id: str,
                         format: str = Query('json', pattern='^(json|markdown)$')):
        matter = authorized_matter(request, slug)
        try:
            payload = service_for(matter).export(matter.matter_id, auth_context(request).principal_id, assertion_id)
        except KeyError as exc:
            raise HTTPException(404, 'Record is no longer available') from exc
        return export_response(request, matter, payload, format, 'assertion',
                               object_type='assertion', object_id=assertion_id)

    @app.get('/matters/{slug}/chronology/export', dependencies=[Depends(require_response_lease)])
    def export_chronology(request: Request, slug: str, entity_id: str = Query('', max_length=80),
                          format: str = Query('json', pattern='^(json|markdown)$')):
        matter = authorized_matter(request, slug)
        try:
            payload = service_for(matter).chronology_export(matter.matter_id, auth_context(request).principal_id, entity_id=entity_id)
        except KeyError as exc:
            raise HTTPException(404, 'Entity or matter is no longer available') from exc
        except WorkspaceProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        return export_response(request, matter, payload, format, 'chronology',
                               object_type='matter', object_id=matter.matter_id)
