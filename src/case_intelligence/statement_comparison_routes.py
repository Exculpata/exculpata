"""Thin authenticated adapters for possible inconsistencies and review decisions."""
from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from .candidate_review import ReviewConflict
from .review_wording import REVIEW_NOTICE, NOTE_TEMPLATES
from .statement_comparison_exports import export_payload
from .statement_comparison_service import StatementComparisonService
from .workspace_store import WorkspaceProblem


def install_statement_comparison_routes(app, *, bench, authorized_matter, auth_context,
        require_csrf, templates, base_context, audit, require_response_lease, transfer_response_lease):

    def load(request, slug, witness, type, status):
        matter = authorized_matter(request, slug)
        service = StatementComparisonService(bench, matter)
        try:
            payload = service.payload(auth_context(request).principal_id, witness=witness, difference_type=type, status=status)
        except KeyError as exc:
            raise HTTPException(404, 'Matter or original passage is no longer available') from exc
        return matter, service, payload

    def finish(request, matter, service, response):
        try:
            with service.entities.repository.reading(matter.matter_id, auth_context(request).principal_id):
                pass
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        return transfer_response_lease(request, response)

    @app.get('/matters/{slug}/inconsistencies', dependencies=[Depends(require_response_lease)])
    def inconsistencies(request: Request, slug: str, witness: str = Query('', max_length=80),
            type: str = Query('', max_length=40), status: str = Query('active', pattern='^(active|all|open|reviewed|dismissed)$')):
        matter, service, payload = load(request, slug, witness, type, status)
        response = templates.TemplateResponse(request=request, name='workbench_inconsistencies.html', context={
            **base_context(request, matter), 'matter': matter, **payload, 'review_notice': REVIEW_NOTICE,
            'difference_types': NOTE_TEMPLATES, 'witness': witness, 'difference_type': type, 'status': status,
            'show_assistant_dock': False,
        }, headers={'Cache-Control': 'no-store'})
        return finish(request, matter, service, response)

    @app.post('/matters/{slug}/inconsistencies/{signature}/review', dependencies=[Depends(require_csrf)])
    def review(request: Request, slug: str, signature: str, action: str = Form(..., max_length=20),
               revision: int = Form(..., ge=0)):
        matter = authorized_matter(request, slug)
        try:
            StatementComparisonService(bench, matter).review(auth_context(request).principal_id, signature, action, revision)
        except KeyError as exc:
            raise HTTPException(404, 'Candidate or matter is no longer available') from exc
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except WorkspaceProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(request, 'inconsistency.' + action, 'success', context=auth_context(request), matter=matter,
              object_type='candidate', object_id=signature)
        return RedirectResponse(f'/matters/{slug}/inconsistencies', status_code=303)

    @app.get('/matters/{slug}/inconsistencies/export', dependencies=[Depends(require_response_lease)])
    def export(request: Request, slug: str, format: str = Query('json', pattern='^(json|markdown|csv)$'),
            witness: str = Query('', max_length=80), type: str = Query('', max_length=40),
            status: str = Query('all', pattern='^(active|all|open|reviewed|dismissed)$')):
        matter, service, payload = load(request, slug, witness, type, status)
        body = export_payload(payload, format)
        if len(body.encode()) > 8 * 1024 * 1024:
            raise HTTPException(400, 'Export exceeds the size limit. Narrow the filters.')
        audit(request, 'inconsistency.export', 'success', context=auth_context(request), matter=matter,
              object_type='matter', object_id=matter.matter_id)
        extension = 'md' if format == 'markdown' else format
        return finish(request, matter, service, Response(body,
            media_type={'json': 'application/json', 'markdown': 'text/markdown', 'csv': 'text/csv'}[format],
            headers={'Cache-Control': 'no-store', 'Content-Disposition': f'attachment; filename="inconsistencies.{extension}"'}))
