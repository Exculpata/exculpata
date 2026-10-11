"""Explicit source-role editing on the existing reader and membership boundary."""
import json
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .source_roles import SourceRoleConflict, SourceRoleService
from .workspace_store import WorkspaceProblem


def install_source_roles(app, *, bench, authorized_matter, auth_context, refresh_authority,
                         require_csrf, require_csrf_header, templates, base_context):
    def scope(request, slug, *, write=False):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        admin = not write and getattr(request.state, 'administrator_matter_override', None) == matter.matter_id
        check = lambda: refresh_authority(request, slug, context, admin)
        check()
        return matter, context, admin, check

    def reader_href(request, slug, token):
        query = {key: request.query_params[key] for key in ('unit', 'q', 'browse', 'entity_return_to')
                 if key in request.query_params}
        return f'/matters/{slug}/sources/{token}' + ('?' + urlencode(query) if query else '')

    def page(request, slug, token, *, proposed=None, error='', status=200):
        matter, context, admin, check = scope(request, slug)
        try:
            store = bench.source_store(matter)
            with store._lock:
                document = store.get_by_action_token(token)
                saved = SourceRoleService(bench.workspace, administrator_override=admin).read(
                    matter.matter_id, context.principal_id, token, check_authority=check)
                name = document.display_name
            if 'application/json' in request.headers.get('accept', '') and not error:
                return JSONResponse(saved, headers={'Cache-Control': 'no-store'})
            response = templates.TemplateResponse(request=request, name='workbench_source_role.html',
                context={**base_context(request, matter), 'matter': matter, 'source_name': name,
                         'saved': saved, 'proposed': proposed, 'error': error, 'can_edit': not admin,
                         'reader_href': reader_href(request, slug, token)},
                status_code=status, headers={'Cache-Control': 'no-store'})
            check()
            return response
        except KeyError as exc:
            raise HTTPException(404, 'Source is no longer available') from exc

    def save(request, slug, token, values):
        matter, context, _, check = scope(request, slug, write=True)
        if not isinstance(values, dict) or set(values) != {'expected_revision', 'source_role'}:
            raise WorkspaceProblem('Supply expected_revision and source_role together.')
        store = bench.source_store(matter)
        # Follow the existing source-then-control lock order through audit/commit.
        with store._lock:
            store.get_by_action_token(token)
            return SourceRoleService(bench.workspace).update(matter.matter_id, context.principal_id,
                token, **values, request_id=request.state.request_id,
                session_id=context.session.session_id if context.session else None, check_authority=check)

    @app.get('/matters/{slug}/sources/{token}/role')
    def get_role(request: Request, slug: str, token: str):
        return page(request, slug, token)

    @app.put('/matters/{slug}/sources/{token}/role', dependencies=[Depends(require_csrf_header)])
    async def put_role(request: Request, slug: str, token: str):
        scope(request, slug, write=True)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, 'Source role request is too large')
        try:
            result = save(request, slug, token, json.loads(body))
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, RecursionError, WorkspaceProblem) as exc:
            return JSONResponse({'message': str(exc) if isinstance(exc, WorkspaceProblem) else 'Supply a valid JSON object.'},
                status_code=409 if isinstance(exc, SourceRoleConflict) else 400, headers={'Cache-Control': 'no-store'})
        except KeyError as exc:
            raise HTTPException(404, 'Source is no longer available') from exc

    @app.post('/matters/{slug}/sources/{token}/role', dependencies=[Depends(require_csrf)])
    def post_role(request: Request, slug: str, token: str, expected_revision: int = Form(..., ge=0),
                  source_role: str = Form(..., max_length=20)):
        try:
            save(request, slug, token, dict(expected_revision=expected_revision, source_role=source_role))
        except WorkspaceProblem as exc:
            return page(request, slug, token, proposed=source_role, error=str(exc),
                        status=409 if isinstance(exc, SourceRoleConflict) else 400)
        except KeyError as exc:
            raise HTTPException(404, 'Source is no longer available') from exc
        return RedirectResponse(reader_href(request, slug, token), status_code=303)
