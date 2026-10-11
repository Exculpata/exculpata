"""Matter orientation HTTP and Home panel; no model inputs or source content."""
import json

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .matter_purpose import MatterPurposeConflict
from .workspace_store import WorkspaceProblem


def install_matter_purpose(app, *, workspace, authorized_matter, auth_context,
                           refresh_authority, require_csrf, require_csrf_header,
                           templates, base_context):
    def scope(request, slug, *, write=False):
        context = auth_context(request)
        matter = authorized_matter(request, slug)
        admin = not write and getattr(request.state, 'administrator_matter_override', None) == matter.matter_id
        check = lambda: refresh_authority(request, slug, context, admin)
        check()
        return matter, context, admin, check

    def panel(request, matter):
        matter, context, admin, check = scope(request, matter.slug)
        try:
            return dict(record=workspace.matter_purpose(matter.matter_id, context.principal_id,
                administrator_override=admin, check_authority=check), can_edit=not admin)
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc

    templates.env.globals['matter_purpose_panel'] = panel

    def save(request, slug, values):
        matter, context, _, check = scope(request, slug, write=True)
        if not isinstance(values, dict) or set(values) != {'expected_revision', 'objective', 'background'}:
            raise WorkspaceProblem('Supply expected_revision, objective and background together.')
        return workspace.update_matter_purpose(matter.matter_id, context.principal_id, **values,
            request_id=request.state.request_id,
            session_id=context.session.session_id if context.session else None, check_authority=check)

    @app.get('/matters/{slug}/purpose')
    def get_purpose(request: Request, slug: str):
        matter, context, admin, check = scope(request, slug)
        try:
            values = workspace.matter_purpose(matter.matter_id, context.principal_id,
                administrator_override=admin, check_authority=check)
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        return JSONResponse(values, headers={'Cache-Control': 'no-store'})

    @app.put('/matters/{slug}/purpose', dependencies=[Depends(require_csrf_header)])
    async def put_purpose(request: Request, slug: str):
        scope(request, slug, write=True)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128 * 1024:
                raise HTTPException(413, 'Matter orientation request is too large')
        try:
            values = json.loads(body)
            result = save(request, slug, values)
        except (ValueError, RecursionError, WorkspaceProblem) as exc:
            message = str(exc) if isinstance(exc, WorkspaceProblem) else 'Supply a valid JSON object.'
            return JSONResponse({'message': message}, status_code=409 if isinstance(exc, MatterPurposeConflict) else 400,
                                headers={'Cache-Control': 'no-store'})
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/matters/{slug}/purpose', dependencies=[Depends(require_csrf)])
    def post_purpose(request: Request, slug: str, expected_revision: int = Form(..., ge=0),
                     objective: str = Form('', max_length=500), background: str = Form('', max_length=10000)):
        try:
            save(request, slug, dict(expected_revision=expected_revision, objective=objective, background=background))
        except WorkspaceProblem as exc:
            matter, _, _, check = scope(request, slug, write=True)
            saved = panel(request, matter)['record']
            response = templates.TemplateResponse(request=request, name='workbench_purpose_edit.html',
                context={**base_context(request, matter), 'matter': matter, 'saved': saved,
                    'draft': dict(objective=objective, background=background, revision=saved['revision']),
                    'error': str(exc), 'conflict': isinstance(exc, MatterPurposeConflict)},
                status_code=409 if isinstance(exc, MatterPurposeConflict) else 400,
                headers={'Cache-Control': 'no-store'})
            check()
            return response
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        return RedirectResponse(f'/matters/{slug}/home#matter-purpose', status_code=303)
