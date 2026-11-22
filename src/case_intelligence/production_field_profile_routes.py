"""Look up a producer's confirmed DAT field mapping for the current case team."""
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .intake_receipts import IntakeReceipts
from .workspace_store import WorkspaceProblem


def install_producer_profile(app, *, workspace, auth_context, read_intake_json, refresh_authority,
                             require_csrf_header, require_response_lease, response_matter, transfer_lease):
    @app.post("/matters/{slug}/production-field-profile",
              dependencies=[Depends(require_csrf_header), Depends(require_response_lease)])
    async def lookup(request: Request, slug: str):
        matter = response_matter(request, slug)
        context = auth_context(request)
        payload = await read_intake_json(request, maximum=4096)
        if set(payload) != {"producer"} or not isinstance(payload["producer"], str):
            raise HTTPException(400, "Supply a producer name.")
        try:
            profile = await run_in_threadpool(IntakeReceipts(workspace).producer_profile,
                                              matter.matter_id, context.principal_id, payload["producer"])
        except WorkspaceProblem as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "Matter not found") from exc
        await run_in_threadpool(refresh_authority, request, slug, context, False)
        response = JSONResponse({"profile": profile}, headers={"Cache-Control": "no-store"})
        return transfer_lease(request, response, through_send=True)
