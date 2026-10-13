"""Real archive and authenticated preview helpers for lifecycle integration tests."""
from datetime import datetime, timedelta
import re

from tests.test_case_lifecycle_web import login


def preview_login(client):
    csrf = login(client)
    client.headers['X-CSRF-Token'] = csrf
    return csrf


def prepare_preview_exit(client, matter):
    bench = client.app.state.workbench
    current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    if current is None:
        response = client.post(f'/matters/{matter.slug}/close/begin', follow_redirects=False)
        assert response.status_code == 303
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    if current.export_id is None:
        response = client.post(f'/matters/{matter.slug}/close/export', follow_redirects=False)
        assert response.status_code == 303 and 'error=' not in response.headers['location'], response.headers['location']
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    if current.state == 'closing':
        response = client.get(f'/matters/{matter.slug}/close/export')
        assert response.status_code == 200
    eligible = datetime.fromisoformat(current.eligible_after.replace('Z', '+00:00'))
    now = max(bench.workspace.current_time(), eligible + timedelta(seconds=1))
    bench.workspace._clock = lambda: now
    client.app.state.identity._clock = lambda: now
    preview_login(client)
    response = client.post(f'/matters/{matter.slug}/close/confirm',
                           data={'export_id': current.export_id}, follow_redirects=False)
    assert response.status_code == 200, response.headers.get('location')
    token = re.search(r'name="token" value="([^"]+)"', response.text).group(1)
    return dict(confirmed_name=matter.display_name, acknowledge='yes',
                export_acknowledged='yes', token=token, export_id=current.export_id)


def prepare_bench_exit(bench, matter, actor_id=None, *, administrator_override=False):
    """Trusted service integration with real bytes and a real persisted session."""
    import hashlib
    import uuid
    from case_intelligence.case_lifecycle_support import install_archive, source_lock, idle
    from case_intelligence.matter_archive import build_matter_archive
    actor_id = actor_id or matter.owner_id
    from tests.case_lifecycle_helpers import lifecycle_session
    admission = lifecycle_session(bench.workspace, actor_id, administrator_override=administrator_override)
    with source_lock(bench, matter.matter_id):
        idle(bench, matter.matter_id)
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        if current is None:
            current = bench.workspace.case_lifecycle.begin_exit(matter.slug, actor_id,
                session_id=admission.session_id, administrator_override=administrator_override)
        if current.export_id is None:
            artifact = build_matter_archive(bench, matter, actor_id,
                administrator_override=administrator_override)
            install_archive(bench, matter, artifact)
            current = bench.workspace.case_lifecycle.register_export(matter.matter_id, actor_id,
                artifact.exit_id, artifact.export_id, artifact.sha256, artifact.snapshot_digest,
                session_id=admission.session_id, administrator_override=administrator_override)
        eligible = datetime.fromisoformat(current.eligible_after.replace('Z', '+00:00'))
        now = max(bench.workspace.current_time(), eligible + timedelta(seconds=1))
        bench.workspace._clock = lambda: now
        session = lifecycle_session(bench.workspace, actor_id, administrator_override=administrator_override)
        if current.state == 'closing':
            deliver_bench_archive(bench, matter, current, actor_id, session.session_id,
                                  administrator_override=administrator_override)
        confirmation = bench.workspace.case_lifecycle.issue_confirmation(matter.slug, actor_id,
            session.session_id, current.export_id, administrator_override=administrator_override)
        return dict(token=confirmation.token, session_id=session.session_id,
                    export_id=current.export_id, export_acknowledged=True)


def claim_bench_exit(bench, matter, actor_id=None, *, administrator_override=False):
    return bench.begin_matter_purge(matter.slug, actor_id or matter.owner_id, matter.display_name,
        administrator_override=administrator_override,
        **prepare_bench_exit(bench, matter, actor_id, administrator_override=administrator_override))


def deliver_bench_archive(bench, matter, current, actor_id, session_id, *, administrator_override=False):
    """Exercise real archive reads and successful outbound sends to a synthetic sink."""
    import anyio
    from case_intelligence.case_archive_response import open_verified_archive, ArchiveResponse
    from case_intelligence.case_archive_delivery import ArchiveDelivery
    descriptor, size = open_verified_archive(bench, matter, current)
    response = ArchiveResponse(descriptor, start=0, length=size, total_size=size,
        expected_sha256=current.archive_digest, status_code=200,
        headers={'Content-Length': str(size)})
    downloads = bench.workspace.case_lifecycle.downloads
    attempt = downloads.begin(matter.matter_id, actor_id, session_id, current.export_id,
        current.archive_digest, size, 0, size, administrator_override=administrator_override)
    observer = ArchiveDelivery(response.delivery_proof,
        complete=lambda: downloads.complete(attempt), discard=lambda: downloads.discard(attempt))
    async def run():
        async def sink(message):
            pass
        async def send(message):
            await observer.send(message, sink)
        async def receive():
            return {'type': 'http.request', 'body': b''}
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        await observer.finish(app_returned=True)
    anyio.run(run)
    assert downloads.receipt(current, actor_id) is not None
