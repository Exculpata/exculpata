"""Synthetic authenticated export-and-exit journeys with real archive bytes."""
from __future__ import annotations

from tests.fixture_write import fixture_write

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


def confirm(client, matter, csrf, export_id='', *, mode='export_exit'):
    response = post(client, matter, 'close/confirm', csrf, export_id=export_id or '', mode=mode)
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
    assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
    assert bench.workspace.case_lifecycle.downloads.receipt(first, matter.owner_id) is not None
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
    assert bench.workspace.case_lifecycle.downloads.receipt(first, matter.owner_id) is None
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
    assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
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


@pytest.mark.parametrize('mode', ['export_exit', 'delete_without_export'])
def test_stopped_backup_clean_restore_keeps_mode_but_requires_fresh_confirmation(tmp_path, mode):
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
        post(client, matter, 'close/begin', csrf, mode=mode)
        if mode == 'export_exit':
            before = prepare(client, bench, matter, csrf)
            assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
        else:
            before = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
            assert before.export_id is None
        archive_digest = before.archive_digest
        source_relative = ('matters', matter.matter_id, 'sources', 'files', doc.stored_name)
        clock[0] += timedelta(days=7, seconds=1)
        csrf = login(client)
        token = confirm(client, matter, csrf, before.export_id, mode=mode)
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
        assert after.mode == mode
        assert (after.exit_id, after.eligible_after, after.archive_digest, after.snapshot_digest) == (
            before.exit_id, before.eligible_after, before.archive_digest, before.snapshot_digest)
        assert restored.joinpath(*source_relative).read_bytes() == original
        assert backup.joinpath(*source_relative).read_bytes() == original
        if mode == 'export_exit':
            verified = verify_matter_archive(archive_path(bench, matter, after), expected_sha256=archive_digest)
            assert verified['export_id'] == before.export_id
            assert client.get(f'/matters/{slug}/close/export').status_code == 200
        else:
            assert client.get(f'/matters/{slug}/close/export').status_code == 409
        assert bench.run_maintenance_once()['matters_purged'] == 0
        denied = post(client, matter, 'close', csrf, token=token, export_id=after.export_id or '', mode=mode,
            confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes', no_backup_acknowledged='yes')
        assert denied.status_code == 303 and 'fresh+deletion+confirmation' in denied.headers['location']
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
        assert bench.workspace.case_lifecycle.current_exit(matter.matter_id) == after
        assert restored.joinpath(*source_relative).read_bytes() == original
        fresh_token = confirm(client, matter, csrf, after.export_id, mode=mode)
        finished = post(client, matter, 'close', csrf, token=fresh_token, export_id=after.export_id or '', mode=mode,
            confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes', no_backup_acknowledged='yes')
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
            assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
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


def login_administrator(client, bench):
    """Use a synthetic persisted administrator session; no authentication bypass."""
    csrf = login(client, 'jordan-lee')
    session = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True).session
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute('UPDATE workbench_session SET application_roles=? WHERE session_id=?',
            ('["administrator"]', session.session_id))
    return csrf


@pytest.mark.parametrize('mode', ['export_exit', 'delete_without_export'])
@pytest.mark.parametrize('role', ['owner', 'administrator'])
def test_two_modes_require_wait_name_matching_mode_and_specific_acknowledgement(case, mode, role):
    client, bench, matter, source, document, clock, csrf = case
    if role == 'administrator':
        csrf = login_administrator(client, bench)
    actor = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True).principal_id
    page = client.get(f'/matters/{matter.slug}/close')
    assert 'value="export_exit"' in page.text and 'value="delete_without_export"' in page.text
    begin = post(client, matter, 'close/begin', csrf, mode=mode)
    assert begin.status_code == 303 and 'error=' not in begin.headers['location']
    current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    assert current.mode == mode
    if mode == 'export_exit':
        current = prepare(client, bench, matter, csrf)
        assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
    else:
        assert 'No preservation archive or backup will be provided' in client.get(f'/matters/{matter.slug}/close').text
        assert 'error=' in post(client, matter, 'close/export', csrf).headers['location']
        assert current.export_id is None
    early = post(client, matter, 'close/confirm', csrf, mode=mode, export_id=current.export_id or '')
    assert early.status_code == 303 and 'seven-day' in early.headers['location']
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login_administrator(client, bench) if role == 'administrator' else login(client)
    token = confirm(client, matter, csrf, current.export_id, mode=mode)
    values = dict(mode=mode, token=token, export_id=current.export_id or '',
                  confirmed_name=matter.display_name, acknowledge='yes')
    missing = post(client, matter, 'close', csrf, **values)
    assert missing.status_code == 303 and 'error=' in missing.headers['location']
    wrong_mode = 'delete_without_export' if mode == 'export_exit' else 'export_exit'
    wrong = post(client, matter, 'close', csrf, **{**values, 'mode': wrong_mode,
        'export_acknowledged': 'yes', 'no_backup_acknowledged': 'yes'})
    assert wrong.status_code == 303 and 'error=' in wrong.headers['location']
    wrong_name = post(client, matter, 'close', csrf, **{**values, 'confirmed_name': 'Generated wrong matter',
        'export_acknowledged': 'yes', 'no_backup_acknowledged': 'yes'})
    assert wrong_name.status_code == 303 and 'error=' in wrong_name.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    assert (source.files / document.stored_name).exists()
    finished = post(client, matter, 'close', csrf, **{**values,
        ('export_acknowledged' if mode == 'export_exit' else 'no_backup_acknowledged'): 'yes'})
    assert finished.status_code == 303 and finished.headers['location'].startswith('/matters/new?')
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
    assert not (bench.storage.matters / matter.matter_id).exists()
    action = 'matter.purge_confirm' if mode == 'export_exit' else 'matter.purge_without_export_confirm'
    events = [event for event in bench.workspace.audit_events(matter.matter_id) if event.action == action]
    assert len(events) == 1 and events[0].actor_principal_id == actor


@pytest.mark.parametrize('mode', ['export_exit', 'delete_without_export'])
def test_ordinary_member_cannot_begin_confirm_or_purge_either_mode(case, mode):
    client, bench, matter, source, document, clock, csrf = case
    member_csrf = login(client, 'jordan-lee')
    member = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True).principal_id
    bench.workspace.add_member(matter.matter_id, member, matter.owner_id)
    assert bench.workspace.membership(matter.matter_id, member).role == 'member'
    assert post(client, matter, 'close/begin', member_csrf, mode=mode).status_code == 404
    owner_csrf = login(client)
    post(client, matter, 'close/begin', owner_csrf, mode=mode)
    clock[0] += timedelta(days=8)
    member_csrf = login(client, 'jordan-lee')
    for action in ('close/confirm', 'close'):
        response = post(client, matter, action, member_csrf, mode=mode,
            token='generated-forged-' + 'a' * 40, confirmed_name=matter.display_name,
            acknowledge='yes', export_acknowledged='yes', no_backup_acknowledged='yes')
        assert response.status_code == 404
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
    assert (source.files / document.stored_name).exists()


@pytest.mark.parametrize('mode', ['export_exit', 'delete_without_export'])
def test_changing_modes_requires_cancel_reopen_and_a_new_window(case, mode):
    client, bench, matter, source, document, clock, csrf = case
    invalid = post(client, matter, 'close/begin', csrf, mode='generated-unknown-mode')
    assert invalid.status_code == 303 and 'error=' in invalid.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    post(client, matter, 'close/begin', csrf, mode=mode)
    before = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    other = 'delete_without_export' if mode == 'export_exit' else 'export_exit'
    denied = post(client, matter, 'close/begin', csrf, mode=other)
    assert denied.status_code == 303 and 'Cancel+closure' in denied.headers['location']
    assert bench.workspace.case_lifecycle.current_exit(matter.matter_id) == before
    post(client, matter, 'close/cancel', csrf, exit_id=before.exit_id)
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'active'
    clock[0] += timedelta(days=1)
    csrf = login(client)
    post(client, matter, 'close/begin', csrf, mode=other)
    after = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
    assert after.mode == other and after.exit_id != before.exit_id
    assert after.eligible_after > before.eligible_after


def test_partial_repeated_and_gapped_ranges_require_complete_coverage_for_the_same_actor(case):
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    current = prepare(client, bench, matter, csrf)
    size = archive_path(bench, matter, current).stat().st_size
    middle = size // 2
    url = f'/matters/{matter.slug}/close/export'
    for header in (f'bytes=0-{middle - 1}', f'bytes=0-{middle - 1}', f'bytes={middle + 1}-'):
        assert client.get(url, headers={'Range': header}).status_code == 206
    assert bench.workspace.case_lifecycle.downloads.receipt(current, matter.owner_id) is None
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    denied = post(client, matter, 'close/confirm', csrf, export_id=current.export_id)
    assert denied.status_code == 303 and 'Download+the+complete' in denied.headers['location']
    admin_csrf = login_administrator(client, bench)
    admin = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True).principal_id
    assert client.get(url, headers={'Range': f'bytes={middle}-{middle}'}).status_code == 206
    assert bench.workspace.case_lifecycle.downloads.receipt(current, matter.owner_id) is None
    assert bench.workspace.case_lifecycle.downloads.receipt(current, admin) is None
    csrf = login(client)
    assert client.get(url, headers={'Range': f'bytes={middle}-{middle}'}).status_code == 206
    assert bench.workspace.case_lifecycle.downloads.receipt(current, matter.owner_id) is not None
    confirm(client, matter, csrf, current.export_id)
    admin_csrf = login_administrator(client, bench)
    denied = post(client, matter, 'close/confirm', admin_csrf, export_id=current.export_id)
    assert denied.status_code == 303 and 'Download+the+complete' in denied.headers['location']
    assert client.get(url).status_code == 200
    assert bench.workspace.case_lifecycle.downloads.receipt(current, admin) is not None
    confirm(client, matter, admin_csrf, current.export_id)


def test_replacement_export_invalidates_delivery_and_old_confirmation(case):
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    before = prepare(client, bench, matter, csrf)
    assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    token = confirm(client, matter, csrf, before.export_id)
    after = prepare(client, bench, matter, csrf)
    assert after.export_id != before.export_id
    assert bench.workspace.case_lifecycle.downloads.receipt(after, matter.owner_id) is None
    blocked = post(client, matter, 'close/confirm', csrf, export_id=after.export_id)
    assert blocked.status_code == 303 and 'Download+the+complete' in blocked.headers['location']
    old = post(client, matter, 'close', csrf, token=token, export_id=before.export_id,
        confirmed_name=matter.display_name, acknowledge='yes', export_acknowledged='yes')
    assert old.status_code == 303 and 'error=' in old.headers['location']
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'


@pytest.mark.parametrize('failure', ['session', 'database'])
def test_outer_completed_send_cannot_record_delivery_after_authority_or_database_failure(case, monkeypatch, failure):
    from case_intelligence.case_archive_delivery import ArchiveDelivery
    from fastapi import HTTPException
    import sqlite3
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    current = prepare(client, bench, matter, csrf)
    session = client.app.state.identity.resolve(client.cookies.get(SESSION_COOKIE), read_only=True).session
    observed = []
    original = ArchiveDelivery.finish
    async def fail_after_send(self, *, app_returned):
        assert app_returned and self.final_sent and self.proof.inner_verified
        observed.append(self.bytes_sent)
        if failure == 'session':
            bench.workspace.revoke_session(session.session_id)
        else:
            def failed_completion(_attempt):
                raise sqlite3.OperationalError('Generated completion transaction failure')
            monkeypatch.setattr(bench.workspace.case_lifecycle.downloads, 'complete', failed_completion)
        await original(self, app_returned=app_returned)
    monkeypatch.setattr(ArchiveDelivery, 'finish', fail_after_send)
    expected_error = sqlite3.OperationalError if failure == 'database' else (HTTPException, RuntimeError)
    with pytest.raises(expected_error):
        client.get(f'/matters/{matter.slug}/close/export')
    assert observed == [archive_path(bench, matter, current).stat().st_size]
    assert bench.workspace.case_lifecycle.downloads.receipt(current, matter.owner_id) is None
    assert bench.matter_active_work_counts(matter.matter_id)['exports'] == 0
    assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'


@pytest.mark.parametrize('interruption', ['final_send_failure', 'asgi23_disconnect'])
def test_interrupted_http_download_discards_attempt_and_retry_can_complete(case, interruption):
    import anyio
    client, bench, matter, source, document, clock, csrf = case
    post(client, matter, 'close/begin', csrf)
    current = prepare(client, bench, matter, csrf)
    path = f'/matters/{matter.slug}/close/export'
    cookie = '; '.join(f'{key}={value}' for key, value in client.cookies.items()).encode()
    async def interrupted_request():
        body_started = anyio.Event()
        received = False
        statuses, attempts = [], []
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            if interruption == 'asgi23_disconnect':
                await body_started.wait()
                return {'type': 'http.disconnect'}
            await anyio.sleep_forever()
        async def send(message):
            if message['type'] == 'http.response.start':
                statuses.append(message['status'])
            elif message['type'] == 'http.response.body':
                if message.get('body'):
                    attempts.append(bench.workspace.connection.execute(
                        'SELECT COUNT(*) FROM workbench_case_download_attempt').fetchone()[0])
                    body_started.set()
                    if interruption == 'asgi23_disconnect':
                        await anyio.sleep(0.01)
                if interruption == 'final_send_failure' and not message.get('more_body', False):
                    raise OSError('Generated interrupted outer final send')
        scope = {'type': 'http', 'asgi': {'version': '3.0',
                 'spec_version': '2.3' if interruption == 'asgi23_disconnect' else '2.4'},
                 'http_version': '1.1', 'method': 'GET', 'scheme': 'http', 'path': path,
                 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
                 'headers': [(b'host', b'testserver'), (b'cookie', cookie)],
                 'client': ('127.0.0.1', 41000), 'server': ('testserver', 80)}
        try:
            await client.app(scope, receive, send)
        except OSError:
            assert interruption == 'final_send_failure'
        assert statuses == [200] and attempts and all(value == 1 for value in attempts)
    anyio.run(interrupted_request)
    assert bench.workspace.case_lifecycle.downloads.receipt(current, matter.owner_id) is None
    assert bench.workspace.connection.execute('SELECT COUNT(*) FROM workbench_case_download_attempt').fetchone()[0] == 0
    assert bench.matter_active_work_counts(matter.matter_id)['exports'] == 0
    assert client.get(path).status_code == 200
    assert bench.workspace.case_lifecycle.downloads.receipt(current, matter.owner_id) is not None


def test_final_archive_verification_does_not_block_unrelated_requests(case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from case_intelligence import case_lifecycle_routes
    client, bench, matter, _, _, clock, csrf = case
    assert post(client, matter, 'close/begin', csrf).status_code == 303
    current = prepare(client, bench, matter, csrf)
    assert client.get(f'/matters/{matter.slug}/close/export').status_code == 200
    clock[0] += timedelta(days=7, seconds=1)
    csrf = login(client)
    started, release = Event(), Event()
    verify = case_lifecycle_routes.verified_archive
    def slow_verify(*args, **kwargs):
        started.set()
        assert release.wait(15), 'Archive verification was not released'
        return verify(*args, **kwargs)
    monkeypatch.setattr(case_lifecycle_routes, 'verified_archive', slow_verify)
    with ThreadPoolExecutor(max_workers=2) as workers:
        confirmation = workers.submit(confirm, client, matter, csrf, current.export_id)
        try:
            assert started.wait(5), 'Confirmation did not reach archive verification'
            navigation = workers.submit(client.get, '/matters/manage')
            assert navigation.result(timeout=5).status_code == 200
            assert not confirmation.done()
        finally:
            release.set()
        assert confirmation.result(timeout=5)
