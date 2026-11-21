"""Inspect one explicitly selected load file before its upload is confirmed."""
from contextlib import ExitStack, contextmanager
from functools import partial
import hashlib
import json
import os
from pathlib import PurePosixPath
import uuid

import anyio
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .intake_receipts import MAX_METADATA_BATCH_BYTES
from .malware_scan import scanner_status
from .modules.discovery_review.inspection import inspect_load_file
from .modules.discovery_review.reference_qc import SelectionInventory
from .pilot_uploads import UploadProblem
from .source_admission import LOAD_FILE_SUFFIXES
from .source_load_files import LoadFileError


def _size_header(request, name):
    values = request.headers.getlist(name)
    if not values:
        return None
    value = values[0]
    if len(values) != 1 or not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise UploadProblem("The submitted byte lengths could not be verified.", 400)
    return int(value)


def _body_sizes(request, maximum):
    prefix = _size_header(request, "x-selection-manifest-bytes")
    if prefix == 0:
        raise UploadProblem("The selection inventory is empty.", 400)
    if prefix is not None and prefix > MAX_METADATA_BATCH_BYTES:
        raise UploadProblem("The selection inventory exceeds the metadata size limit.", 413)
    prefix = prefix or 0
    total = _size_header(request, "content-length")
    if total is None:
        return prefix, None
    if total < prefix:
        raise UploadProblem("The selection inventory is incomplete.", 400)
    if total - prefix > maximum:
        raise UploadProblem("The selected load file exceeds the document size limit.", 413)
    return prefix, total - prefix


def _parse_inventory(raw, suffix):
    def unique_keys(pairs):
        obj = dict(pairs)
        if len(pairs) != len(obj):
            raise ValueError("Duplicate inventory key")
        return obj

    try:
        inventory = SelectionInventory(json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys))
    except (UnicodeError, ValueError, RecursionError):
        raise UploadProblem("The selection inventory is invalid. Review the folder again.", 400) from None
    if PurePosixPath(inventory.index_path.replace("\\", "/")).suffix.casefold() != suffix:
        raise UploadProblem("The selected index does not match its file type.", 400)
    return inventory


async def _read_inventory(chunks, length, suffix):
    if not length:
        return None, b""
    prefix = bytearray()
    async for chunk in chunks:
        needed = length - len(prefix)
        prefix.extend(memoryview(chunk)[:needed])
        if len(prefix) == length:
            return await run_in_threadpool(_parse_inventory, prefix, suffix), memoryview(chunk)[needed:]
    raise UploadProblem("The selection inventory is incomplete.", 400)


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
            prefix_size, declared = _body_sizes(request, maximum)
            if store.malware_scan_mode != "disabled":
                status = await run_in_threadpool(scanner_status, store.malware_scanner)
                if not status.ready:
                    raise UploadProblem("Required security scanning is unavailable. Inspection stopped.", 503)
            chunks = request.stream()
            inventory, chunk = await _read_inventory(chunks, prefix_size, suffix)
            cleanup = ExitStack()
            try:
                path, stream = await run_in_threadpool(cleanup.enter_context, _inspection_stage(
                    bench, matter, store, maximum if declared is None else declared))
                count = 0
                digest = hashlib.sha256()
                while chunk is not None:
                    count += len(chunk)
                    if count > maximum:
                        raise UploadProblem("The selected load file exceeds the document size limit.", 413)
                    if declared is not None and count > declared:
                        raise UploadProblem("The file size changed. Choose the file again.", 400)
                    if chunk:
                        await run_in_threadpool(_write_chunk, stream, digest, chunk)
                    chunk = await anext(chunks, None)
                if declared is not None and count != declared:
                    raise UploadProblem("The file size changed. Choose the file again.", 400)
                await run_in_threadpool(stream.flush)
                sha256 = digest.hexdigest()
                inspect = inspect_load_file if inventory is None else partial(inspect_load_file, inventory=inventory)
                report = await run_in_threadpool(store.inspect_staged_load_file, path, suffix, sha256, inspect)
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
