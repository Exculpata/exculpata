"""Serve exactly the verified archive descriptor, with bounded range retry."""
from __future__ import annotations

import hashlib
import os
import re
import stat

import anyio
from fastapi import HTTPException
from starlette.responses import StreamingResponse

from .case_archive_delivery import ArchiveDeliveryProof
from .case_lifecycle_support import archive_path
from .workspace_store import WorkspaceProblem


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink)


class _VerifiedDescriptor(int):
    """An ordinary OS descriptor retaining its verified identity across routing."""

    def __new__(cls, descriptor, identity, sha256):
        result = super().__new__(cls, descriptor)
        result.verified_identity, result.sha256 = identity, sha256
        return result


class ArchiveResponse(StreamingResponse):
    def __init__(self, descriptor, *, start, length, status_code, headers,
                 total_size=None, expected_sha256=None):
        self.descriptor = descriptor
        before = os.fstat(descriptor)
        self.start, self.length = start, length
        self.total_size = before.st_size if total_size is None else total_size
        self.delivery_proof = ArchiveDeliveryProof(start, length, self.total_size, status_code)
        identity = _identity(before)
        verified = getattr(descriptor, 'verified_identity', identity)
        expected_sha256 = expected_sha256 or getattr(descriptor, 'sha256', None)
        whole = start == 0 and length == self.total_size
        if (not stat.S_ISREG(before.st_mode) or before.st_size != self.total_size
                or before.st_nlink > 1 or (before.st_nlink != 1 and expected_sha256 is None)
                or (identity != verified and not (whole and expected_sha256))):
            raise WorkspaceProblem('The verified archive changed before delivery.')
        os.lseek(descriptor, start, os.SEEK_SET)
        async def chunks():
            digest = hashlib.sha256()
            remaining = length
            if _identity(os.fstat(descriptor)) != identity:
                raise WorkspaceProblem('The verified archive changed before delivery.')
            while remaining:
                chunk = await anyio.to_thread.run_sync(os.read, descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError('The verified archive ended during delivery.')
                remaining -= len(chunk)
                digest.update(chunk)
                yield chunk
            if (_identity(os.fstat(descriptor)) != identity
                    or (whole and expected_sha256 and digest.hexdigest() != expected_sha256)):
                raise WorkspaceProblem('The verified archive changed during delivery.')
            self.delivery_proof.inner_sha256 = digest.hexdigest()
            self.delivery_proof.inner_verified = True
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
        if digest.hexdigest() != current.archive_digest or _identity(before) != _identity(after):
            raise WorkspaceProblem('The saved full export changed. Create a new export before deletion.')
        return _VerifiedDescriptor(descriptor, _identity(after), current.archive_digest), after.st_size
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
    return ArchiveResponse(descriptor, start=start, length=end - start + 1, total_size=size,
                           expected_sha256=current.archive_digest, status_code=status_code, headers=headers)
