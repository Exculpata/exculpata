"""Serve exactly the verified archive descriptor, with bounded range retry."""
from __future__ import annotations

import hashlib
import os
import re
import stat

import anyio
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from .case_lifecycle_support import archive_path
from .workspace_store import WorkspaceProblem


class ArchiveResponse(StreamingResponse):
    def __init__(self, descriptor, *, start, length, status_code, headers):
        self.descriptor = descriptor
        os.lseek(descriptor, start, os.SEEK_SET)
        async def chunks():
            remaining = length
            while remaining:
                chunk = await anyio.to_thread.run_sync(os.read, descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError('The verified archive ended during delivery.')
                remaining -= len(chunk)
                yield chunk
        super().__init__(chunks(), status_code=status_code, media_type='application/zip', headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            os.close(self.descriptor)


def open_verified_archive(bench, matter, current):
    """Hold an exclusive regular inode whose bytes match the certified ZIP."""
    path = archive_path(bench, matter, current)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size <= 0:
            raise WorkspaceProblem('The saved full export is unsafe.')
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if digest.hexdigest() != current.archive_digest or identity(before) != identity(after):
            raise WorkspaceProblem('The saved full export changed. Create a new export before deletion.')
        return descriptor, after.st_size
    except BaseException:
        os.close(descriptor)
        raise


def archive_response(request, matter, current, descriptor, size):
    start, end, status_code = 0, size - 1, 200
    headers = {'Cache-Control': 'no-store', 'X-Archive-SHA256': current.archive_digest,
               'ETag': f'"{current.archive_digest}"', 'Accept-Ranges': 'bytes',
               'Content-Disposition': f'attachment; filename="{matter.slug}-full-export.zip"'}
    range_value = request.headers.get('range', '')
    if range_value and request.headers.get('if-range', headers['ETag']) == headers['ETag']:
        match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', range_value.strip())
        if match is None or not any(match.groups()):
            raise HTTPException(416, 'Requested archive range is unavailable.', headers={'Content-Range': f'bytes */{size}'})
        first, last = match.groups()
        if first:
            start, end = int(first), min(int(last), size - 1) if last else size - 1
        else:
            suffix = int(last)
            start = max(size - suffix, 0)
            if suffix <= 0:
                start = size
        if start >= size or start > end:
            raise HTTPException(416, 'Requested archive range is unavailable.', headers={'Content-Range': f'bytes */{size}'})
        status_code = 206
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    headers['Content-Length'] = str(end - start + 1)
    return ArchiveResponse(descriptor, start=start, length=end - start + 1,
                           status_code=status_code, headers=headers)
