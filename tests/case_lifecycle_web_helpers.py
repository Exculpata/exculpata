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
    with source_lock(bench, matter.matter_id):
        idle(bench, matter.matter_id)
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        if current is None:
            current = bench.workspace.case_lifecycle.begin_exit(matter.slug, actor_id,
                administrator_override=administrator_override)
        if current.export_id is None:
            artifact = build_matter_archive(bench, matter, actor_id,
                administrator_override=administrator_override)
            install_archive(bench, matter, artifact)
            current = bench.workspace.case_lifecycle.register_export(matter.matter_id, actor_id,
                artifact.exit_id, artifact.export_id, artifact.sha256, artifact.snapshot_digest,
                administrator_override=administrator_override)
        eligible = datetime.fromisoformat(current.eligible_after.replace('Z', '+00:00'))
        now = max(bench.workspace.current_time(), eligible + timedelta(seconds=1))
        bench.workspace._clock = lambda: now
        expires = bench.workspace._timestamp(now + timedelta(hours=1))
        session = bench.workspace.create_session(actor_id, hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            'test', expires, expires, application_roles=frozenset({'administrator'}) if administrator_override else frozenset())
        confirmation = bench.workspace.case_lifecycle.issue_confirmation(matter.slug, actor_id,
            session.session_id, current.export_id, administrator_override=administrator_override)
        return dict(token=confirmation.token, session_id=session.session_id,
                    export_id=current.export_id, export_acknowledged=True)


def claim_bench_exit(bench, matter, actor_id=None, *, administrator_override=False):
    return bench.begin_matter_purge(matter.slug, actor_id or matter.owner_id, matter.display_name,
        administrator_override=administrator_override,
        **prepare_bench_exit(bench, matter, actor_id, administrator_override=administrator_override))
