"""Authorized production summary pages and fresh source-cited exports."""
import csv
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import Response

from . import production_summary as summary
from .workspace_store import WorkspaceProblem


def install_production_summary_routes(app, *, bench, authorized_matter, auth_context,
                                      templates, base_context, audit,
                                      require_response_lease, transfer_response_lease):
    def membership(matter, actor):
        try:
            bench.workspace.membership(matter.matter_id, actor)
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc

    def current(request, slug, load_file):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        membership(matter, actor)
        store = bench.source_store(matter)
        try:
            payload = summary.build_summary(matter.display_name, slug, store, load_file)
        except WorkspaceProblem as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, csv.Error) as exc:
            raise HTTPException(400, 'Select a UTF-8 CSV with columns path,begin_bates,end_bates') from exc
        return matter, actor, payload

    @app.get('/matters/{slug}/production-summary', dependencies=[Depends(require_response_lease)])
    def page(request: Request, slug: str, load_file: str = Query('', max_length=200)):
        matter, actor, payload = current(request, slug, load_file)
        response = templates.TemplateResponse(request=request, name='workbench_production_summary.html', context={
            **base_context(request, matter), 'matter': matter, 'summary': payload,
            'sections': summary.sections(payload), 'visible': summary.visible, 'load_file': load_file,
            'show_assistant_dock': False,
        }, headers={'Cache-Control': 'no-store'})
        membership(matter, actor)
        return transfer_response_lease(request, response)

    @app.get('/matters/{slug}/production-summary/export', dependencies=[Depends(require_response_lease)])
    def export(request: Request, slug: str, load_file: str = Query('', max_length=200),
               format: str = Query('markdown', pattern='^(markdown|json)$')):
        matter, actor, payload = current(request, slug, load_file)
        body = (summary.render_markdown(payload) if format == 'markdown' else summary.render_json(payload)).encode('utf-8')
        from .assertion_repository import MAX_STORAGE_BYTES
        if len(body) > MAX_STORAGE_BYTES:
            return Response(status_code=400, headers={'Cache-Control': 'no-store'})
        membership(matter, actor)
        audit(request, 'production_summary.export', 'success', context=auth_context(request),
              matter=matter, object_type='matter', object_id=matter.matter_id)
        return transfer_response_lease(request, Response(body,
            media_type={'markdown': 'text/markdown', 'json': 'application/json'}[format],
            headers={'Cache-Control': 'no-store', 'X-Review-Notice': payload['notice'],
                     'Content-Disposition': f'attachment; filename="production-summary.{"md" if format == "markdown" else format}"'}))
