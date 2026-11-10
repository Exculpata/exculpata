"""Mount the authorized production document count page."""
from fastapi import Depends, HTTPException, Request

from case_intelligence.modules import ModuleContext


def route_hook(app, *, context: ModuleContext, authorized_matter, auth_context,
               templates, base_context, require_response_lease, transfer_response_lease):
    def membership(matter, actor):
        try:
            context.membership(matter.matter_id, actor)
        except KeyError as exc:
            raise HTTPException(404, "Matter is no longer available") from exc

    @app.get("/matters/{slug}/production", dependencies=[Depends(require_response_lease)])
    def production_documents(request: Request, slug: str):
        matter = authorized_matter(request, slug)
        actor = auth_context(request).principal_id
        membership(matter, actor)
        count = context.source_store(matter).document_count()
        response = templates.TemplateResponse(
            request=request, name="workbench_production_documents.html",
            context={**base_context(request, matter), "matter": matter,
                     "document_count": count, "show_assistant_dock": False},
            headers={"Cache-Control": "no-store"},
        )
        membership(matter, actor)
        return transfer_response_lease(request, response)
