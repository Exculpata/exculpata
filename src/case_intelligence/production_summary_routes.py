"""Authorized production summary page and fresh exports."""
import csv
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import Response
from . import production_summary as summary
from .pilot_uploads import UploadProblem
from .workspace_store import WorkspaceProblem


def install_production_summary_routes(app, *, bench, authorized_matter, auth_context,
                                      templates, base_context, audit, require_response_lease, transfer_response_lease):
    def respond(request, slug, load_file, format=None):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        store = bench.source_store(matter)
        service = bench.assertion_service(matter)
        context = base_context(request, matter) if not format else {}
        try:
            with service.entities.source_guard(), service.repository.transaction(matter.matter_id, actor):
                bench.workspace.membership(matter.matter_id, actor)
                payload = summary.build_summary(matter.display_name, slug, store, load_file)
                if format:
                    body = summary.render_json(payload) if format == 'json' else summary.render_markdown(payload)
                    from .assertion_repository import MAX_STORAGE_BYTES
                    if len(body.encode()) > MAX_STORAGE_BYTES:
                        return Response(status_code=400, headers={'Cache-Control': 'no-store'})
                    response = Response(body, media_type='application/json' if format == 'json' else 'text/markdown',
                        headers={'Cache-Control': 'no-store', 'Content-Disposition':
                                 f'attachment; filename="production-summary.{"json" if format == "json" else "md"}"'})
                else:
                    response = templates.TemplateResponse(request=request, name='workbench_production_summary.html',
                        context={**context, 'matter': matter, 'summary': payload,
                                 'sections': summary.sections(payload), 'visible': summary.visible,
                                 'load_file': load_file, 'show_assistant_dock': False}, headers={'Cache-Control': 'no-store'})
            with service.repository.transaction(matter.matter_id, actor):
                pass
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        except (WorkspaceProblem, UploadProblem, OSError) as exc:
            raise HTTPException(409, 'The production changed or a source is unavailable') from exc
        except (ValueError, csv.Error, OverflowError) as exc:
            raise HTTPException(400, 'The load file could not be summarized') from exc
        if format:
            audit(request, 'production_summary.export', 'success', context=auth_context(request),
                  matter=matter, object_type='matter', object_id=matter.matter_id)
        return transfer_response_lease(request, response)

    @app.get('/matters/{slug}/production-summary', dependencies=[Depends(require_response_lease)])
    def page(request: Request, slug: str, load_file: str = Query('', max_length=32)):
        return respond(request, slug, load_file)

    @app.get('/matters/{slug}/production-summary/export', dependencies=[Depends(require_response_lease)])
    def export(request: Request, slug: str, load_file: str = Query('', max_length=32),
               format: str = Query('markdown', pattern='^(markdown|json)$')):
        return respond(request, slug, load_file, format)
