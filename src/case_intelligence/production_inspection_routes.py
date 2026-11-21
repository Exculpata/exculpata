"""Inspect one explicitly selected load file before its upload is confirmed."""
from contextlib import ExitStack, contextmanager
import hashlib
import os
import uuid

import anyio
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .malware_scan import scanner_status
from .modules.discovery_review.inspection import inspect_load_file
from .pilot_uploads import UploadProblem
from .source_admission import LOAD_FILE_SUFFIXES
from .source_load_files import LoadFileError


def _body_size(request, maximum):
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    if not declared.isascii() or not declared.isdecimal() or len(declared) > 19:
        raise UploadProblem("The file size could not be verified.", 400)
    size = int(declared)
    if size > maximum:
        raise UploadProblem("The selected load file exceeds the document size limit.", 413)
    return size


@contextmanager
def _inspection_stage(bench, matter, store, reservation):
    key = "inspection-" + uuid.uuid4().hex
    bench.reserve_temporary_space(matter.matter_id, key, reservation, purpose="load-file inspection")
    try:
        # Use the store's existing crash-recovery naming convention.
        path = store.staging / f".upload-{uuid.uuid4().hex}.part"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                yield path, stream
        finally:
            path.unlink(missing_ok=True)
    finally:
        bench.release_temporary_space(matter.matter_id, key)


def _write_chunk(stream, digest, chunk):
    stream.write(chunk)
    digest.update(chunk)


def install_production_inspection(app, *, bench, auth_context, refresh_authority,
                                  require_csrf_header, require_response_lease, response_matter, transfer_lease):
    @app.post("/matters/{slug}/production-inspection/{suffix}",
              dependencies=[Depends(require_csrf_header), Depends(require_response_lease)])
    async def inspect_selected(request: Request, slug: str, suffix: str):
        suffix = "." + suffix.lower()
        matter = response_matter(request, slug)
        context = auth_context(request)
        try:
            if suffix not in LOAD_FILE_SUFFIXES or request.headers.get("content-type") != "application/octet-stream":
                raise UploadProblem("Choose a DAT, OPT, LFP or DII file to inspect.", 415)
            store = await run_in_threadpool(bench.source_store, matter)
            maximum = store.maximum_file_bytes(suffix)
            declared = _body_size(request, maximum)
            if store.malware_scan_mode != "disabled":
                status = await run_in_threadpool(scanner_status, store.malware_scanner)
                if not status.ready:
                    raise UploadProblem("Required security scanning is unavailable. Inspection stopped.", 503)
            cleanup = ExitStack()
            try:
                path, stream = await run_in_threadpool(cleanup.enter_context, _inspection_stage(
                    bench, matter, store, maximum if declared is None else declared))
                count = 0
                digest = hashlib.sha256()
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > maximum:
                        raise UploadProblem("The selected load file exceeds the document size limit.", 413)
                    if declared is not None and count > declared:
                        raise UploadProblem("The file size changed. Choose the file again.", 400)
                    if chunk:
                        await run_in_threadpool(_write_chunk, stream, digest, chunk)
                if declared is not None and count != declared:
                    raise UploadProblem("The file size changed. Choose the file again.", 400)
                await run_in_threadpool(stream.flush)
                sha256 = digest.hexdigest()
                report = await run_in_threadpool(store.inspect_staged_load_file, path, suffix,
                                                 sha256, inspect_load_file)
                report["sha256"] = sha256
            finally:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(cleanup.close)
            await run_in_threadpool(refresh_authority, request, slug, context, False)
            return transfer_lease(request, JSONResponse(report), through_send=True)
        except UploadProblem as exc:
            return JSONResponse({"message": str(exc)}, status_code=exc.status_code)
        except LoadFileError:
            return JSONResponse({"message": "The load file could not be inspected completely."}, status_code=422)
        except OSError:
            return JSONResponse({"message": "Temporary inspection storage is unavailable."}, status_code=503)
