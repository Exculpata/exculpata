"""Synthetic transport proofs distinguish outer delivery from inner buffering."""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import anyio
import pytest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import ClientDisconnect

from case_intelligence.case_archive_delivery import ArchiveDelivery
from case_intelligence.case_archive_response import (
    ArchiveResponse, _VerifiedDescriptor, _identity, archive_response)
from case_intelligence.workbench import _MatterResponseSendLeaseMiddleware
from case_intelligence.workspace_store import WorkspaceProblem


def response_for(path, *, start=0, length=None):
    descriptor = os.open(path, os.O_RDONLY)
    size = os.fstat(descriptor).st_size
    length = size - start if length is None else length
    status = 200 if (start, length) == (0, size) else 206
    headers = {'Content-Length': str(length)}
    if status == 206:
        headers['Content-Range'] = f'bytes {start}-{start + length - 1}/{size}'
    response = ArchiveResponse(descriptor, start=start, length=length, total_size=size,
        status_code=status, headers=headers,
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return response


async def drive(response, *, buffered=False, send_hook=None, disconnect=False,
                callback_error=False, app_error=False, rewrite=None, spec_version=None,
                after_final_disconnect=False, method='GET'):
    events, sent = [], []
    def complete():
        assert events[-1] == 'app_returned'
        assert response.delivery_proof.inner_verified
        events.append('complete_called')
        if callback_error:
            raise RuntimeError('Generated database failure')
        events.append('completed')
    observer = ArchiveDelivery(response.delivery_proof, complete=complete,
                              discard=lambda: events.append('discarded'))
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': spec_version or ('2.3' if disconnect else '2.4')},
             'method': method, 'path': '/', 'query_string': b'', 'headers': [],
             'http_version': '1.1', 'scheme': 'http', 'state': {
                 'case_archive_delivery': observer,
                 'matter_response_lease': {'send_owned': True, 'matter': SimpleNamespace(matter_id='generated'),
                                          'lease_id': 'generated-lease'},
                 'media_clip_response_lease': ('generated', 'generated-clip')}}
    first_body = anyio.Event()
    request_read = False
    async def receive():
        nonlocal request_read
        if after_final_disconnect and observer.final_sent:
            return {'type': 'http.disconnect'}
        if not request_read:
            request_read = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        if disconnect:
            await first_body.wait()
            return {'type': 'http.disconnect'}
        await anyio.sleep_forever()
    async def send(message):
        assert 'complete_called' not in events
        if send_hook:
            await send_hook(message, response)
        sent.append(dict(message))
        if message['type'] == 'http.response.body' and message.get('body'):
            first_body.set()
            if disconnect:
                await anyio.sleep(0.01)
    async def endpoint(scope, receive, send):
        await response(scope, receive, send)
        events.append('inner_returned')
    async def passthrough(request, call_next):
        return await call_next(request)
    inner = BaseHTTPMiddleware(endpoint, dispatch=passthrough) if buffered else endpoint
    async def app(scope, receive, send):
        async def rewritten(message):
            await send(rewrite(dict(message)) if rewrite else message)
        await inner(scope, receive, rewritten)
        if after_final_disconnect:
            assert (await receive())['type'] == 'http.disconnect'
        if app_error:
            raise RuntimeError('Generated application failure after final send')
        events.append('app_returned')
    wrapped = _MatterResponseSendLeaseMiddleware(app,
        release=lambda matter, lease: events.append('lease_released'),
        release_clip=lambda matter, clip: events.append('clip_released'))
    error = None
    try:
        await wrapped(scope, receive, send)
    except BaseException as exc:
        error = exc
    with pytest.raises(OSError):
        os.fstat(response.descriptor)
    assert events[-2:] == ['lease_released', 'clip_released']
    return observer, events, sent, error


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'generated-archive'
    path.write_bytes(b'Generated archive bytes.\n' * 110_000)
    return path


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('bounds', [(0, None), (7, 37), (0, 1)])
def test_completion_follows_outer_final_send_and_app_return_before_lease_release(source, buffered, bounds):
    response = response_for(source, start=bounds[0], length=bounds[1])
    observer, events, sent, error = anyio.run(lambda: drive(response, buffered=buffered))
    assert error is None
    assert events[-4:] == ['complete_called', 'completed', 'lease_released', 'clip_released']
    assert observer.bytes_sent == response.length
    assert b''.join(message.get('body', b'') for message in sent) == source.read_bytes()[
        response.start:response.start + response.length]


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('phase', ['start', 'body', 'final'])
def test_send_failure_never_completes_even_when_inner_buffer_finished(source, buffered, phase):
    response = response_for(source)
    inner_finished = []
    async def fail(message, response):
        selected = (message['type'] == 'http.response.start' if phase == 'start' else
                    message['type'] == 'http.response.body' and bool(message.get('body')) if phase == 'body' else
                    message['type'] == 'http.response.body' and not message.get('more_body', False))
        if selected:
            inner_finished.append(response.delivery_proof.inner_verified)
            raise OSError('Generated send failure')
    observer, events, sent, error = anyio.run(lambda: drive(response, buffered=buffered, send_hook=fail))
    assert error is not None
    assert observer.failed and 'completed' not in events and 'discarded' in events
    if buffered and phase == 'final':
        assert inner_finished == [True]


@pytest.mark.parametrize('buffered', [False, True])
def test_asgi23_disconnect_does_not_complete_on_normal_inner_return(source, buffered):
    response = response_for(source)
    observer, events, sent, error = anyio.run(lambda: drive(response, buffered=buffered, disconnect=True))
    assert observer.disconnected
    assert 'completed' not in events and 'discarded' in events


@pytest.mark.parametrize('buffered', [False, True])
def test_asgi23_complete_body_then_connection_teardown_counts_delivery(source, buffered):
    response = response_for(source)
    observer, events, sent, error = anyio.run(lambda: drive(response, buffered=buffered,
        spec_version='2.3', after_final_disconnect=True))
    assert error is None and not observer.disconnected and 'completed' in events


def test_head_cannot_count_suppressed_body_as_delivered(source):
    response = response_for(source)
    observer, events, sent, error = anyio.run(lambda: drive(response, method='HEAD'))
    assert error is None and observer.failed and 'completed' not in events


@pytest.mark.parametrize('change', ['truncate', 'same_size'])
def test_descriptor_mutation_during_send_invalidates_inner_proof(source, change):
    response = response_for(source)
    changed = False
    async def mutate(message, response):
        nonlocal changed
        if not changed and message['type'] == 'http.response.body' and message.get('body'):
            changed = True
            if change == 'truncate':
                source.write_bytes(b'Generated shorter bytes')
            else:
                with source.open('r+b') as stream:
                    stream.seek(0)
                    stream.write(b'X')
    observer, events, sent, error = anyio.run(lambda: drive(response, send_hook=mutate))
    assert error is not None and not response.delivery_proof.inner_verified
    assert 'completed' not in events


@pytest.mark.parametrize('change', ['status', 'length', 'range', 'body', 'missing_final', 'encoding'])
def test_outer_status_headers_and_exact_body_must_match_inner_proof(source, change):
    response = response_for(source, start=4, length=25)
    def rewrite(message):
        if message['type'] == 'http.response.start':
            if change == 'status':
                message['status'] = 200
            elif change in {'length', 'range', 'encoding'}:
                key, value = {'length': (b'content-length', b'24'),
                              'range': (b'content-range', b'bytes 5-29/100'),
                              'encoding': (b'content-encoding', b'gzip')}[change]
                message['headers'] = [(k, v) for k, v in message['headers'] if k != key] + [(key, value)]
        elif message['type'] == 'http.response.body':
            if change == 'body' and message.get('body'):
                message['body'] = b'X' * len(message['body'])
            elif change == 'missing_final':
                message['more_body'] = True
        return message
    observer, events, sent, error = anyio.run(lambda: drive(response, rewrite=rewrite))
    assert error is None and response.delivery_proof.inner_verified
    assert 'completed' not in events and 'discarded' in events


@pytest.mark.parametrize('failure', ['callback', 'after_send'])
def test_database_or_postsend_application_error_fails_closed_and_releases(source, failure):
    response = response_for(source)
    observer, events, sent, error = anyio.run(lambda: drive(response,
        callback_error=failure == 'callback', app_error=failure == 'after_send'))
    assert isinstance(error, RuntimeError)
    assert 'completed' not in events
    assert observer.final_sent and response.delivery_proof.inner_verified


def test_cancellation_releases_descriptor_and_lease_without_completion(source):
    response = response_for(source)
    async def cancel(message, response):
        if message['type'] == 'http.response.body' and message.get('body'):
            raise anyio.get_cancelled_exc_class()()
    observer, events, sent, error = anyio.run(lambda: drive(response, send_hook=cancel))
    assert error is not None and 'completed' not in events


@pytest.mark.parametrize('range_header, expected', [('bytes=4-8', (4, 5)), ('bytes=-7', (93, 7)),
                                                   ('bytes=95-', (95, 5)), ('bytes=98-999', (98, 2))])
def test_range_response_exposes_exact_requested_bounds(tmp_path, range_header, expected):
    path = tmp_path / 'generated-range'
    path.write_bytes(b'x' * 100)
    descriptor = os.open(path, os.O_RDONLY)
    request = SimpleNamespace(headers={'range': range_header})
    current = SimpleNamespace(archive_digest=hashlib.sha256(path.read_bytes()).hexdigest())
    response = archive_response(request, SimpleNamespace(slug='generated'), current, descriptor, 100)
    assert (response.start, response.length) == expected
    assert response.total_size == 100 and response.status_code == 206
    observer, events, sent, error = anyio.run(lambda: drive(response))
    assert error is None and 'completed' in events


def test_verified_descriptor_mutation_before_range_construction_is_rejected(source):
    fd = os.open(source, os.O_RDONLY)
    descriptor = _VerifiedDescriptor(fd, _identity(os.fstat(fd)), hashlib.sha256(source.read_bytes()).hexdigest())
    try:
        with source.open('r+b') as stream:
            stream.write(b'X')
        with pytest.raises(WorkspaceProblem, match='changed before delivery'):
            ArchiveResponse(descriptor, start=4, length=10, total_size=os.fstat(fd).st_size,
                            status_code=206, headers={})
    finally:
        os.close(fd)


def test_full_stream_hash_rejects_mutation_between_verification_and_construction(source):
    fd = os.open(source, os.O_RDONLY)
    descriptor = _VerifiedDescriptor(fd, _identity(os.fstat(fd)), hashlib.sha256(source.read_bytes()).hexdigest())
    with source.open('r+b') as stream:
        stream.write(b'X')
    size = os.fstat(fd).st_size
    response = ArchiveResponse(descriptor, start=0, length=size, total_size=size, status_code=200,
                               headers={'Content-Length': str(size)})
    observer, events, sent, error = anyio.run(lambda: drive(response))
    assert isinstance(error, WorkspaceProblem) and 'completed' not in events


def test_replaced_path_keeps_verified_inode_and_requires_its_full_stream_hash(source):
    expected = source.read_bytes()
    fd = os.open(source, os.O_RDONLY)
    descriptor = _VerifiedDescriptor(fd, _identity(os.fstat(fd)), hashlib.sha256(expected).hexdigest())
    replacement = source.with_suffix('.replacement')
    replacement.write_bytes(b'Generated unrelated replacement bytes')
    replacement.replace(source)
    response = ArchiveResponse(descriptor, start=0, length=len(expected), total_size=len(expected),
                               status_code=200, headers={'Content-Length': str(len(expected))})
    observer, events, sent, error = anyio.run(lambda: drive(response))
    assert error is None and 'completed' in events
    assert b''.join(message.get('body', b'') for message in sent) == expected


def test_duplicate_relevant_header_blocks_delivery_but_duplicate_cookies_do_not(source):
    def cookies(message):
        if message['type'] == 'http.response.start':
            message['headers'] += [(b'set-cookie', b'generated_a=1'), (b'set-cookie', b'generated_b=2')]
        return message
    response = response_for(source)
    observer, events, sent, error = anyio.run(lambda: drive(response, rewrite=cookies))
    assert error is None and 'completed' in events
    def duplicate(message):
        if message['type'] == 'http.response.start':
            message['headers'] += [(b'Content-Length', str(response.length).encode())]
        return message
    response = response_for(source)
    observer, events, sent, error = anyio.run(lambda: drive(response, rewrite=duplicate))
    assert error is None and 'completed' not in events
