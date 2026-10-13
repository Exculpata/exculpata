"""Synthetic authenticated export-and-exit journeys with real archive bytes."""
from __future__ import annotations

import hashlib
import io
import re
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from case_intelligence.case_lifecycle_support import archive_path
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE, SESSION_COOKIE
from case_intelligence.managed_storage import StoragePolicy
from case_intelligence.matter_archive_verifier import verify_matter_archive
from case_intelligence.pilot_uploads import UploadProblem
from case_intelligence.workbench import create_workbench_app


def login(client, subject='taylor-morgan'):
    client.cookies.clear()
    page = client.get('/auth/login', params={'next': '/matters/manage'})
    assert page.status_code == 200
    response = client.post('/auth/login', data={'identity_subject': subject,
        'login_challenge': client.cookies.get(LOGIN_CHALLENGE_COOKIE), 'next': '/matters/manage'},
        follow_redirects=False)
    assert response.status_code == 303, response.text
    landing = client.get('/matters/manage')
    assert landing.status_code == 200
    return re.search(r'data-csrf-token="([0-9a-f]{64})"', landing.text).group(1)


@pytest.fixture
def case(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(),
        auth_mode='preview', answer_workers=1, storage_policy=StoragePolicy(reserve_bytes=0))
    bench = app.state.workbench
    clock = [datetime.now(timezone.utc)]
    bench.workspace._clock = lambda: clock[0]
    app.state.identity._clock = lambda: clock[0]
    with TestClient(app) as client:
        csrf = login(client)
        result = client.post('/matters', data={'name': 'Generated archive case', 'csrf_token': csrf},
                             follow_redirects=False)
        assert result.status_code == 303
        slug = result.headers['location'].split('/')[2]
        matter = bench.workspace.get_active_matter(slug)
        source = bench.source_store(matter)
        document, _ = source.store_stream('generated.txt', 'text/plain', io.BytesIO(b'Generated original evidence.\n'))
        yield client, bench, matter, source, document, clock, csrf


def post(client, matter, action, csrf, **values):
    return client.post(f'/matters/{matter.slug}/{action}', data={'csrf_token': csrf, **values},
                       follow_redirects=False)


def prepare(client, bench, matter, csrf):
    response = post(client, matter, 'close/export', csrf)
    assert response.status_code == 303
    assert 'error=' not in response.headers['location'], response.headers['location']
    current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    assert current.export_id
    return current


def confirm(client, matter, csrf, export_id):
    response = post(client, matter, 'close/confirm', csrf, export_id=export_id)
    assert response.status_code == 200, response.headers.get('location')
    return re.search(r'name="token" value="([^"]+)"', response.text).group(1)


def test_full_exit_waits_verifies_download_and_requires_fresh_explicit_confirmation(case, tmp_path):
    client, bench, matter, source, document, clock, csrf = case
    policy = bench.workspace.case_lifecycle.policy(matter.matter_id)
    assert policy.mode == 'ongoing'
    assert datetime.fromisoformat(policy.next_check_in_at.replace('Z', '+00:00')) - clock[0] >= timedelta(days=89)
    assert bench.workspace.matter_retention(matter.matter_id) is None
    page = client.get(f'/matters/{matter.slug}/close')
    assert 'Begin closure' in page.text
    assert 'Permanently delete matter' not in page.text
    direct = post(client, matter, 'close', csrf, confirmed_name=matter.display_name, acknowledge='yes')
    assert 'error=' in direct.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    assert post(client, matter, 'close/begin', csrf).status_code == 303
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    before = (source.files / document.stored_name).read_bytes()
    with pytest.raises(UploadProblem, match='closing'):
        source.remove(document.document_id)
    assert (source.files / document.stored_name).read_bytes() == before
    assert client.post(f'/matters/{matter.slug}/rename', data={'csrf_token': csrf, 'name': 'Denied rename'}).status_code == 404
    current = prepare(client, bench, matter, csrf)
    download = client.get(f'/matters/{matter.slug}/close/export')
    assert download.status_code == 200
    assert download.headers['x-archive-sha256'] == hashlib.sha256(download.content).hexdigest()
    downloaded = tmp_path / 'download.zip'
    downloaded.write_bytes(download.content)
    manifest = verify_matter_archive(downloaded, expected_sha256=current.archive_digest)
    assert manifest['original_source_files_included'] is True
    partial = client.get(f'/matters/{matter.slug}/close/export', headers={'Range': 'bytes=0-99'})
    assert partial.status_code == 206 and partial.content == download.content[:100]
    early = post(client, matter, 'close/confirm', csrf, export_id=current.export_id)
    assert 'seven-day' in early.headers['location']
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    token = confirm(client, matter, csrf, current.export_id)
    missing_ack = post(client, matter, 'close', csrf, token=token, export_id=current.export_id,
                       confirmed_name=matter.display_name, acknowledge='yes')
    assert 'error=' in missing_ack.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    finished = post(client, matter, 'close', csrf, token=token, export_id=current.export_id,
                    confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes')
    assert finished.status_code == 303 and finished.headers['location'].startswith('/matters/new?'), finished.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
    assert not (bench.storage.matters / matter.matter_id).exists()
    assert not (bench.storage.root / 'preservation-exports' / matter.matter_id).exists()
    assert client.get(f'/matters/{matter.slug}/close/export').status_code == 404
    actions = {event.action for event in bench.workspace.audit_events(matter.matter_id)}
    assert {'matter.exit_begin', 'matter.exit_export', 'matter.purge_confirm'} <= actions


def test_cancel_reopens_and_invalidates_export_with_a_new_seven_day_window(case):
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    first = prepare(client, bench, matter, csrf)
    first_path = archive_path(bench, matter, first)
    lease = bench.begin_matter_response(matter, allowed_states=frozenset({'closing'}))
    refused = post(client, matter, 'close/cancel', csrf, exit_id=first.exit_id)
    assert 'error=' in refused.headers['location']
    bench.finish_matter_response(matter.matter_id, lease)
    cancelled = post(client, matter, 'close/cancel', csrf, exit_id=first.exit_id)
    assert cancelled.status_code == 303
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    assert not first_path.exists()
    assert bench.workspace.case_lifecycle.current_exit(matter.matter_id) is None
    source = bench.source_store(matter)
    source.store_stream('more.txt', 'text/plain', io.BytesIO(b'Generated continued work.\n'))
    clock[0] += timedelta(days=1)
    csrf = login(client)
    post(client, matter, 'close/begin', csrf)
    second = prepare(client, bench, matter, csrf)
    assert first.exit_id != second.exit_id and first.export_id != second.export_id
    assert second.eligible_after > first.eligible_after
    assert 'Closure pending' in client.get('/matters/manage').text


def test_missing_restored_archive_or_changed_original_cannot_authorize_first_deletion(case):
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    current = prepare(client, bench, matter, csrf)
    path = archive_path(bench, matter, current)
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    token = confirm(client, matter, csrf, current.export_id)
    saved = path.read_bytes()
    path.unlink()
    denied = post(client, matter, 'close', csrf, token=token, export_id=current.export_id,
                  confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes')
    assert 'unavailable' in denied.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    path.write_bytes(saved)
    original = source.files / document.stored_name
    original.write_bytes(b'Generated changed bytes.\n')
    denied = post(client, matter, 'close', csrf, token=token, export_id=current.export_id,
                  confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes')
    assert 'error=' in denied.headers['location']
    assert original.exists() and bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'


def test_closure_routes_enforce_csrf_membership_and_source_readonly_open(case):
    client, bench, matter, source, document, clock, csrf = case
    missing_csrf = client.post(f'/matters/{matter.slug}/close/begin')
    assert missing_csrf.status_code == 403
    post(client, matter, 'close/begin', csrf)
    # Frozen reads must not perform legacy startup normalization or orphan cleanup.
    orphan = source.files / ('e' * 32)
    orphan.write_bytes(b'Generated orphan retained for recovery.')
    staged = source.staging / ('.upload-' + 'f' * 32 + '.part')
    staged.write_bytes(b'Generated pending bytes.')
    registry = source.registry_path.read_bytes()
    readonly = bench.archive_source_store(matter)
    try:
        assert readonly.document_count() == 1
        for operation in (lambda: readonly.remove(document.document_id),
                          lambda: readonly.discard_resumable_upload('a' * 32)):
            with pytest.raises(UploadProblem):
                operation()
    finally:
        readonly.close()
    assert orphan.exists() and staged.exists() and source.registry_path.read_bytes() == registry
    assert 'error=' in post(client, matter, 'close/export', csrf).headers['location']
    other_csrf = login(client, 'jordan-lee')
    for action in ('close/begin', 'close/export', 'check-in'):
        assert post(client, matter, action, other_csrf).status_code == 404
    assert client.get(f'/matters/{matter.slug}/close').status_code == 404


def test_stopped_backup_clean_restore_keeps_archive_but_requires_fresh_confirmation(tmp_path):
    runtime = tmp_path / 'generated-runtime'
    app = create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode='preview',
        answer_workers=1, storage_policy=StoragePolicy(reserve_bytes=0))
    clock = [datetime.now(timezone.utc)]
    app.state.workbench.workspace._clock = lambda: clock[0]
    app.state.identity._clock = lambda: clock[0]
    with TestClient(app) as client:
        csrf = login(client)
        result = client.post('/matters', data={'name': 'Generated restore matter', 'csrf_token': csrf},
                             follow_redirects=False)
        slug = result.headers['location'].split('/')[2]
        bench = app.state.workbench
        matter = bench.workspace.get_active_matter(slug)
        original = b'Generated original preserved across stopped backup and clean restore.\n'
        doc, _ = bench.source_store(matter).store_stream('generated-restore.txt', 'text/plain', io.BytesIO(original))
        post(client, matter, 'close/begin', csrf)
        before = prepare(client, bench, matter, csrf)
        archive_digest = before.archive_digest
        source_relative = ('matters', matter.matter_id, 'sources', 'files', doc.stored_name)
        clock[0] += timedelta(days=7, seconds=1)
        csrf = login(client)
        token = confirm(client, matter, csrf, before.export_id)
        cookies = dict(client.cookies)
        session_id = app.state.identity.resolve(cookies[SESSION_COOKIE], read_only=True).session.session_id
        nonce_expiry = bench.workspace.connection.execute(
            'SELECT expires_at FROM workbench_case_confirmation').fetchone()[0]
        assert datetime.fromisoformat(nonce_expiry.replace('Z', '+00:00')) > clock[0]
    # All source connections and workers have stopped before copying the entire
    # synthetic runtime; restore into an absent target, leaving the backup intact.
    backup = tmp_path / 'generated-stopped-backup'
    restored = tmp_path / 'generated-clean-restore'
    shutil.copytree(runtime, backup)
    shutil.copytree(backup, restored)
    original_database = hashlib.sha256((backup / 'workbench.sqlite').read_bytes()).hexdigest()
    recovered = create_workbench_app(restored, generator=UnavailableGenerator(), auth_mode='preview',
        answer_workers=1, storage_policy=StoragePolicy(reserve_bytes=0))
    recovered.state.workbench.workspace._clock = lambda: clock[0]
    recovered.state.identity._clock = lambda: clock[0]
    with TestClient(recovered) as client:
        client.cookies.update(cookies)
        restored_session = recovered.state.identity.resolve(cookies[SESSION_COOKIE], read_only=True)
        assert restored_session is not None and restored_session.session.session_id == session_id
        bench = recovered.state.workbench
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
        after = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        assert (after.exit_id, after.eligible_after, after.archive_digest, after.snapshot_digest) == (
            before.exit_id, before.eligible_after, before.archive_digest, before.snapshot_digest)
        assert restored.joinpath(*source_relative).read_bytes() == original
        assert backup.joinpath(*source_relative).read_bytes() == original
        verified = verify_matter_archive(archive_path(bench, matter, after), expected_sha256=archive_digest)
        assert verified['export_id'] == before.export_id
        assert client.get(f'/matters/{slug}/close/export').status_code == 200
        assert bench.run_maintenance_once()['matters_purged'] == 0
        denied = post(client, matter, 'close', csrf, token=token, export_id=after.export_id,
            confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes')
        assert denied.status_code == 303 and 'fresh+deletion+confirmation' in denied.headers['location']
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
        assert bench.workspace.case_lifecycle.current_exit(matter.matter_id) == after
        assert restored.joinpath(*source_relative).read_bytes() == original
        fresh_token = confirm(client, matter, csrf, after.export_id)
        finished = post(client, matter, 'close', csrf, token=fresh_token, export_id=after.export_id,
            confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes')
        assert finished.status_code == 303 and finished.headers['location'].startswith('/matters/new?')
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
        assert not restored.joinpath(*source_relative).exists()
        assert bench.workspace.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert hashlib.sha256((backup / 'workbench.sqlite').read_bytes()).hexdigest() == original_database
    assert hashlib.sha256((runtime / 'workbench.sqlite').read_bytes()).hexdigest() == original_database
    assert runtime.joinpath(*source_relative).read_bytes() == backup.joinpath(*source_relative).read_bytes() == original


@pytest.mark.parametrize('stage', ['build', 'download', 'purge'])
def test_long_archive_operations_recheck_revoked_session_before_release_or_deletion(case, monkeypatch, stage):
    from case_intelligence import case_archive_response, case_lifecycle_routes, case_lifecycle_support
    from case_intelligence.identity import SESSION_COOKIE
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    def revoke():
        context = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True)
        bench.workspace.revoke_session(context.session.session_id)
    if stage == 'build':
        original = case_lifecycle_routes.build_matter_archive
        def during_build(*args, **kwargs):
            artifact = original(*args, **kwargs)
            revoke()
            return artifact
        monkeypatch.setattr(case_lifecycle_routes, 'build_matter_archive', during_build)
        response = post(client, matter, 'close/export', csrf)
        assert response.status_code == 404
        assert bench.workspace.case_lifecycle.current_exit(matter.matter_id).export_id is None
        assert not list(bench.storage.root.glob('preservation-exports/*/*'))
    else:
        current = prepare(client, bench, matter, csrf)
        if stage == 'download':
            original = case_archive_response.open_verified_archive
            opened = []
            def during_hash(*args, **kwargs):
                value = original(*args, **kwargs)
                opened.append(value[0])
                revoke()
                return value
            monkeypatch.setattr(case_archive_response, 'open_verified_archive', during_hash)
            response = client.get(f'/matters/{matter.slug}/close/export')
            assert response.status_code == 404 and response.headers.get('content-type') != 'application/zip'
            import os
            with pytest.raises(OSError):
                os.fstat(opened[0])
        else:
            clock[0] += timedelta(days=7, seconds=1)
            csrf = login(client)
            token = confirm(client, matter, csrf, current.export_id)
            original = case_lifecycle_support.verified_archive
            def during_verification(*args, **kwargs):
                result = original(*args, **kwargs)
                revoke()
                return result
            monkeypatch.setattr(case_lifecycle_support, 'verified_archive', during_verification)
            response = post(client, matter, 'close', csrf, token=token, export_id=current.export_id,
                confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes')
            assert response.status_code == 404
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    assert (source.files / document.stored_name).exists()
    assert bench.matter_active_work_counts(matter.matter_id)['exports'] == 0


def test_archive_download_holds_verified_inode_and_closes_it(case, monkeypatch):
    import os
    from case_intelligence import case_archive_response
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    current = prepare(client, bench, matter, csrf)
    path = archive_path(bench, matter, current)
    expected = path.read_bytes()
    original = case_archive_response.open_verified_archive
    opened = []
    def replace_after_verification(*args, **kwargs):
        result = original(*args, **kwargs)
        opened.append(result[0])
        changed = path.with_suffix('.replacement')
        changed.write_bytes(b'Generated unverified replacement bytes.')
        changed.replace(path)
        return result
    monkeypatch.setattr(case_archive_response, 'open_verified_archive', replace_after_verification)
    response = client.get(f'/matters/{matter.slug}/close/export')
    assert response.status_code == 200 and response.content == expected
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert bench.matter_active_work_counts(matter.matter_id)['exports'] == 0


def test_archive_descriptor_closes_if_client_disconnects(tmp_path):
    import anyio
    import os
    from case_intelligence.case_archive_response import ArchiveResponse
    from starlette.requests import ClientDisconnect
    path = tmp_path / 'generated-download'
    path.write_bytes(b'Generated disconnected download.')
    descriptor = os.open(path, os.O_RDONLY)
    response = ArchiveResponse(descriptor, start=0, length=path.stat().st_size,
        status_code=200, headers={})
    async def disconnected():
        async def send(_message):
            raise OSError('Generated client disconnect')
        async def receive():
            return {'type': 'http.disconnect'}
        with pytest.raises(ClientDisconnect):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    anyio.run(disconnected)
    with pytest.raises(OSError):
        os.fstat(descriptor)
