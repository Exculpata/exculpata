"""Authorized summary jobs, current snapshots and leased exports."""
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from .production_summary import ProductionSummaryService
from .production_summary_exports import export_summary
from .production_summary_jobs import SummaryJobs
from .work_product_exports import ExportProblem
from .workspace_store import WorkspaceProblem


def install_production_summary_routes(app, *, bench, authorized_matter, auth_context, require_csrf,
                                      templates, base_context, require_response_lease, transfer_response_lease):
    jobs = SummaryJobs()
    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application) as state:
            try:
                yield state
            finally:
                jobs.close()
    app.router.lifespan_context = lifespan

    def service(request, slug):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        bench.workspace.membership(matter.matter_id, actor)
        return matter, actor, ProductionSummaryService(bench, matter, actor)

    @app.post('/matters/{slug}/production-summary', dependencies=[Depends(require_csrf)])
    def start(request: Request, slug: str, scope: str = Form('whole_matter', max_length=80)):
        try:
            matter, actor, summary = service(request, slug)
            if scope != 'whole_matter':
                from .intake_receipts import IntakeReceipts
                IntakeReceipts(bench.workspace).get(matter.matter_id, actor, scope)
            job = jobs.start((matter.matter_id, actor, scope), lambda: summary.summarize(matter.matter_id, scope=scope))
        except KeyError as exc:
            raise HTTPException(404, 'Matter or receipt is unavailable.') from exc
        except WorkspaceProblem as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse(f'/matters/{slug}/production-summary?' + urlencode(dict(scope=scope, job=job)), status_code=303)

    @app.get('/matters/{slug}/production-summary', dependencies=[Depends(require_response_lease)])
    def show(request: Request, slug: str, scope: str = Query('whole_matter', max_length=80),
             job: str = Query('', max_length=32), type: str = Query('', max_length=20), undated: bool = False, day: str = Query('', max_length=10)):
        try:
            matter, actor, service_ = service(request, slug)
            status = jobs.get((matter.matter_id, actor, scope), job) if job else None
            summary = (service_.summarize(matter.matter_id, scope=scope)
                       if not status or status['state'] == 'completed' else None)
            # Results are read fresh, under membership/source guards, even after a completed job.
            rows = [row for row in summary.sources if (not type or row['type'] == type) and (not undated or not row['dates']) and (not day or day in row['dates'])] if summary else []
            response = templates.TemplateResponse(request=request, name='workbench_production_summary.html', context={
                **base_context(request, matter), 'matter': matter, 'summary': summary, 'summary_sources': rows,
                'summary_job': status, 'scope': scope, 'export_query': urlencode(dict(scope=scope)),
                'show_assistant_dock': False}, headers={'Cache-Control': 'no-store'})
            bench.workspace.membership(matter.matter_id, actor)
            return transfer_response_lease(request, response)
        except KeyError as exc:
            raise HTTPException(404, 'Matter, receipt or temporary summary job is unavailable. Start a new summary.') from exc
        except WorkspaceProblem as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/matters/{slug}/production-summary/export', dependencies=[Depends(require_response_lease)])
    def export(request: Request, slug: str, scope: str = Query('whole_matter', max_length=80), format: str = Query('markdown', max_length=16)):
        try:
            matter, actor, service_ = service(request, slug)
            summary = service_.summarize(matter.matter_id, scope=scope)
            artifact = export_summary(summary, format, matter.display_name)
            bench.workspace.membership(matter.matter_id, actor)
        except KeyError as exc:
            raise HTTPException(404, 'Matter or receipt is unavailable.') from exc
        except (WorkspaceProblem, ExportProblem) as exc:
            raise HTTPException(409, str(exc)) from exc
        response = Response(artifact.body, media_type=artifact.media_type, headers={
            'Content-Disposition': f'attachment; filename="{artifact.filename}"', 'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff'})
        return transfer_response_lease(request, response)
