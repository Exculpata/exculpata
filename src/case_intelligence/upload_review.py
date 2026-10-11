"""Read-only review of a specific legacy upload, within matter membership."""
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse


def install_upload_review(app, *, bench, authorized_matter, auth_context, templates, base_context):
    @app.get("/matters/{slug}/upload-review/{session_id}/{item_id}", response_class=HTMLResponse)
    def upload_review(request: Request, slug: str, session_id: str, item_id: str):
        actor = auth_context(request).principal_id
        try:
            matter = authorized_matter(request, slug)
            with bench.workspace._lock:
                bench.workspace.membership(matter.matter_id, actor)
                item = bench.workspace.connection.execute(
                    "SELECT c.name AS collection_name,u.relative_path,u.display_name,u.state,u.message,"
                    "u.expected_size,u.received_size FROM workbench_upload_item u "
                    "JOIN workbench_upload_session s ON s.matter_id=u.matter_id "
                    "AND s.upload_session_id=u.upload_session_id "
                    "JOIN workbench_source_collection c ON c.matter_id=s.matter_id AND c.collection_id=s.collection_id "
                    "WHERE u.matter_id=? AND u.upload_session_id=? AND u.upload_item_id=?",
                    (matter.matter_id, session_id, item_id)).fetchone()
                if item is None:
                    raise KeyError(item_id)
                item = dict(item)
        except KeyError as exc:
            raise HTTPException(404, "Upload item not found") from exc
        return templates.TemplateResponse(request=request, name="workbench_upload_review.html",
            context={**base_context(request, matter), "matter": matter, "upload_item": item},
            headers={"Cache-Control": "no-store"})
