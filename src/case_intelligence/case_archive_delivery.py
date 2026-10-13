"""Observe server-side delivery at the outer ASGI boundary, before lease release.

This records bytes accepted by ASGI send, not client storage or backup success.
Repository and authentication decisions belong to the supplied callbacks.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

import anyio


@dataclass
class ArchiveDeliveryProof:
    start: int
    length: int
    total_size: int
    status_code: int
    inner_verified: bool = False
    inner_sha256: str | None = None

    def __post_init__(self):
        if (any(type(value) is not int for value in (self.start, self.length, self.total_size))
                or self.start < 0 or self.length <= 0 or self.start + self.length > self.total_size
                or self.status_code not in {200, 206}
                or (self.status_code == 200 and (self.start != 0 or self.length != self.total_size))):
            raise ValueError('Invalid archive delivery bounds.')

    @property
    def end(self):
        return self.start + self.length - 1


class ArchiveDelivery:
    """One response observer, installed as scope.state.case_archive_delivery."""

    def __init__(self, proof: ArchiveDeliveryProof, *, complete: Callable[[], None],
                 discard: Callable[[], None] | None = None):
        self.proof, self.complete, self.discard = proof, complete, discard
        self.started = self.final_sent = self.failed = self.disconnected = False
        self.finished = False
        self.bytes_sent = 0
        self.digest = hashlib.sha256()

    def disconnect(self):
        # ASGI 2.3 can report normal teardown after the response is complete.
        # Only an interruption before the accepted final send defeats delivery.
        if not self.final_sent:
            self.disconnected = True

    def _start_matches(self, message):
        headers = message.get('headers', [])
        values = {}
        for key, value in headers:
            key = key.lower()
            if key not in {b'content-length', b'content-range', b'content-encoding'}:
                continue
            if key in values:
                return False
            values[key] = value
        expected_range = f'bytes {self.proof.start}-{self.proof.end}/{self.proof.total_size}'.encode()
        return (message.get('status') == self.proof.status_code
                and values.get(b'content-length') == str(self.proof.length).encode()
                and values.get(b'content-range') == (expected_range if self.proof.status_code == 206 else None)
                and values.get(b'content-encoding') in (None, b'identity')
                and not message.get('trailers', False))

    async def send(self, message, send):
        kind = message.get('type')
        body = message.get('body', b'')
        if kind == 'http.response.start':
            if self.started or self.final_sent or not self._start_matches(message):
                self.failed = True
        elif kind == 'http.response.body':
            if (not self.started or self.final_sent or not isinstance(body, bytes)
                    or self.bytes_sent + len(body) > self.proof.length):
                self.failed = True
        else:
            self.failed = True
        try:
            await send(message)
        except BaseException:
            self.failed = True
            raise
        # Observe only sends which the outer server accepted successfully.
        if kind == 'http.response.start':
            self.started = True
        elif kind == 'http.response.body' and isinstance(body, bytes):
            self.bytes_sent += len(body)
            self.digest.update(body)
            if not message.get('more_body', False):
                self.final_sent = True

    async def finish(self, *, app_returned: bool):
        if self.finished:
            return
        self.finished = True
        delivered = (app_returned and not self.failed and not self.disconnected
                     and self.started and self.final_sent
                     and self.bytes_sent == self.proof.length and self.proof.inner_verified
                     and self.digest.hexdigest() == self.proof.inner_sha256)
        if delivered:
            # Failure must leave the attempt incomplete. The repository callback
            # is responsible for an atomic transaction and fresh authorization.
            await anyio.to_thread.run_sync(self.complete)
        elif self.discard is not None:
            await anyio.to_thread.run_sync(self.discard)
