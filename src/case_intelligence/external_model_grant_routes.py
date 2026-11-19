"""Operator review of matter-specific destinations for external model requests."""

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .workspace_store import WorkspaceProblem


_CAPABILITIES = {
    "generation": ("Answer generation and extraction", "May send questions, review instructions and source text."),
    "embedding": ("Embedding", "May send extracted passages for indexing and search queries for matching."),
    "reranking": ("Reranking", "May send search queries and candidate passages to rank search results."),
}


def install_model_grants(app, *, workspace, templates, authorized_matter,
                         auth_context, refresh_authority, require_csrf):
    def review_rows(matter_id):
        rows = []
        for destination, approved, configuration_id in workspace.model_grant_review(matter_id):
            label, description = _CAPABILITIES[destination.capability]
            rows.append((destination, label, description, approved, configuration_id))
        return rows

    templates.env.globals["external_model_grant_rows"] = review_rows

    @app.post("/matters/{slug}/model-grants", dependencies=[Depends(require_csrf)])
    def change_model_grant(request: Request, slug: str, capability: str = Form(...),
                           endpoint: str = Form(...), model: str = Form(...), decision: str = Form(...),
                           configuration_id: str = Form(...)):
        matter = authorized_matter(request, slug, allow_administrator_write=True)
        context = auth_context(request)
        administrator_override = matter.owner_id != context.principal_id
        if administrator_override and not context.is_administrator:
            raise HTTPException(403, "Only the matter owner or an administrator can change model permissions.")
        if capability not in _CAPABILITIES or decision not in {"approve", "revoke"}:
            raise HTTPException(400, "Choose a model permission shown in matter setup.")

        approved = decision == "approve"
        review = workspace.model_grant_review(matter.matter_id)
        destination = next((item for item, granted, _ in review
                            if item.capability == capability and (approved or granted)), None)
        if destination is None or (destination.endpoint, destination.model) != (endpoint, model):
            raise HTTPException(409, "The model configuration or permission changed. Review it again in matter setup.")

        def check_authority():
            return refresh_authority(request, slug, context, administrator_override)

        try:
            workspace.set_model_grant(
                matter.matter_id, destination, context.principal_id, approved=approved,
                expected_configuration_id=configuration_id,
                administrator_override=administrator_override, check_authority=check_authority,
                session_id=context.session.session_id if context.session else None,
                request_id=request.state.request_id,
            )
        except KeyError as exc:
            raise HTTPException(404, "Matter is no longer available.") from exc
        except WorkspaceProblem as exc:
            raise HTTPException(409, "The permission could not be updated. Review it again in matter setup.") from exc
        return RedirectResponse(f"/matters/{slug}/setup#model-grants", status_code=303)
