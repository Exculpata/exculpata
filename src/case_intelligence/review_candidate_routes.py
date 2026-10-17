"""Thin authenticated adapters for review candidates and review decisions."""
from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from .candidate_review import ReviewConflict
from .review_candidates import NOTICE, CATEGORIES
from .review_candidate_exports import export_payload
from .review_candidate_service import ReviewCandidateService
from .workspace_store import WorkspaceProblem


def install_review_candidate_routes(app, *, bench, authorized_matter, auth_context,
        require_csrf, templates, base_context, audit, require_response_lease, transfer_response_lease):

    def load(request, slug, category, status):
        matter = authorized_matter(request, slug)
        service = ReviewCandidateService(bench, matter)
        try:
            payload = service.payload(auth_context(request).principal_id, category=category, status=status)
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

    @app.get('/matters/{slug}/review-candidates', dependencies=[Depends(require_response_lease)])
    def candidates(request: Request, slug: str, category: str = Query('', max_length=40), status: str = Query('active', pattern='^(active|all|open|reviewed|dismissed)$')):
        matter, service, payload = load(request, slug, category, status)
        response = templates.TemplateResponse(request=request, name='workbench_review_candidates.html', context={
            **base_context(request, matter), 'matter': matter, **payload, 'review_notice': NOTICE,
            'categories': CATEGORIES, 'category': category, 'status': status,
            'show_assistant_dock': False,
        }, headers={'Cache-Control': 'no-store'})
        return finish(request, matter, service, response)

    @app.post('/matters/{slug}/review-candidates/{signature}/review', dependencies=[Depends(require_csrf)])
    def review(request: Request, slug: str, signature: str, action: str = Form(..., max_length=20),
               revision: int = Form(..., ge=0)):
        matter = authorized_matter(request, slug)
        try:
            ReviewCandidateService(bench, matter).review(auth_context(request).principal_id, signature, action, revision)
        except KeyError as exc:
            raise HTTPException(404, 'Candidate or matter is no longer available') from exc
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except WorkspaceProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        audit(request, 'review_candidates.' + action, 'success', context=auth_context(request), matter=matter,
              object_type='candidate', object_id=signature)
        return RedirectResponse(f'/matters/{slug}/review-candidates', status_code=303)

    @app.get('/matters/{slug}/review-candidates/export', dependencies=[Depends(require_response_lease)])
    def export(request: Request, slug: str, format: str = Query('json', pattern='^(json|markdown|csv)$'),
            category: str = Query('', max_length=40),
            status: str = Query('all', pattern='^(active|all|open|reviewed|dismissed)$')):
        matter, service, payload = load(request, slug, category, status)
        body = export_payload(payload, format)
        if len(body.encode()) > 8 * 1024 * 1024:
            raise HTTPException(400, 'Export exceeds the size limit. Narrow the filters.')
        audit(request, 'review_candidates.export', 'success', context=auth_context(request), matter=matter,
              object_type='matter', object_id=matter.matter_id)
        extension = 'md' if format == 'markdown' else format
        return finish(request, matter, service, Response(body,
            media_type={'json': 'application/json', 'markdown': 'text/markdown', 'csv': 'text/csv'}[format],
            headers={'Cache-Control': 'no-store', 'Content-Disposition': f'attachment; filename="review-candidates.{extension}"'}))
