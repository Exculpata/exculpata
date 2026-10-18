"""Synthetic selection, byte receipt, exact source support, and portable downloads."""

from tests.fixture_write import fixture_write

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import csv
import io
import json
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'


@pytest.fixture
def workspace(tmp_path):
    with TestClient(create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(),
            auth_mode='test', background_ingestion=True, ingestion_workers=1)) as client:
        response = client.post('/matters', data={'name': 'Synthetic receipt review', 'descriptor': 'Generated selection'}, follow_redirects=False)
        assert response.status_code == 303
        slug = response.headers['location'].split('/')[2]
        bench = client.app.state.workbench
        yield client, bench, bench.matter(slug, ACTOR)


def descriptor(path, size):
    return {'name': path.rsplit('/', 1)[-1], 'relative_path': path, 'size': size, 'media_type': 'text/plain'}


def selection(client, slug, files, indexes, key='a' * 32, title='Generated folder'):
    root = f'/matters/{slug}/intake-receipts'
    payload = {'selection_key': key, 'selection_fingerprint': 'b' * 64,
        'selected_count': len(files), 'eligible_indexes': indexes, 'collection_name': title}
    response = client.post(root, json=payload)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert client.post(root, json=payload).json()['receipt_id'] == receipt['receipt_id']
    url = root + '/' + receipt['receipt_id']
    states = ['valid' if i in indexes else 'unsupported' for i in range(len(files))]
    assert client.post(url + '/items', json={'start': 0, 'files': files, 'reviewed_states': states}).status_code == 200
    sealed = client.post(url + '/seal')
    assert sealed.status_code == 200
    return sealed.json()


def upload(client, slug, receipt, files, ordinals, **extra):
    payload = {'files': files, 'collection_name': 'Generated folder',
        'intake_receipt_id': receipt['receipt_id'], 'intake_ordinals': ordinals, **extra}
    response = client.post(f'/matters/{slug}/upload-sessions', json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()


def put(client, item, body, offset=0):
    response = client.put(item['chunk_url'], content=body,
        headers={'Content-Type': 'application/octet-stream', 'X-Upload-Offset': str(offset)})
    assert response.status_code == 200, response.text


def test_receipt_retains_skips_received_bytes_exact_source_and_complete_exports(workspace):
    client, bench, matter = workspace
    body = b'Synthetic copper journal passage.\n'
    files = [descriptor('North/report.txt', len(body)), descriptor('Unsupported/opaque.bin', 27)]
    receipt = selection(client, matter.slug, files, [0])
    session = upload(client, matter.slug, receipt, files[:1], [0])
    item = session['items'][0]
    put(client, item, body)
    status = client.get(f"/matters/{matter.slug}/intake-receipts/{receipt['receipt_id']}").json()
    assert status['counts']['received'] == 1 and status['counts']['searchable'] == 0
    assert client.post(item['finalize_url']).status_code == 200
    receipts = IntakeReceipts(bench.workspace)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if receipts.get(matter.matter_id, ACTOR, receipt['receipt_id'])['counts']['searchable'] == 1:
            break
        time.sleep(0.02)
    page = client.get(receipt['receipt_url'])
    assert page.status_code == 200
    assert 'Unsupported/opaque.bin' in page.text and '1 not uploaded' in page.text
    assert 'check readiness for suggested people, things and dates' in page.text
    assert 'not automatically limited to this collection' in page.text
    link = re.search(r'href="(/matters/[^\"]+/sources/[^\"]+)"[^>]*>Open source</a>', page.text)
    assert link is not None
    source = client.get(link.group(1))
    assert source.status_code == 200 and body.decode().strip() in source.text
    ranged = client.get(link.group(1) + '/content', headers={'Range': 'bytes=0-8'})
    assert ranged.status_code == 206 and ranged.content == body[:9]
    document = next(iter(bench.source_store(matter).documents.values()))
    binding = bench.workspace.connection.execute('SELECT source_version_id FROM workbench_intake_transfer WHERE matter_id=?', (matter.matter_id,)).fetchone()[0]
    assert binding == document.version_id
    exported = client.get(receipt['receipt_url'] + '/export?format=json')
    assert exported.status_code == 200
    data = exported.json()
    assert data['counts']['received'] == data['counts']['searchable'] == 1
    assert len(data['items']) == 2 and data['items'][1]['preflight_state'] == 'unsupported'
    assert not {'document_id', 'catalog_document_id', 'version_id'} & data['items'][0].keys()
    bundle = client.get(f'/matters/{matter.slug}/export')
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = [n for n in archive.namelist() if n.startswith('intake/')]
        assert len(names) == 3
        bundled = json.loads(archive.read(next(n for n in names if n.endswith('.json'))))
        assert bundled == data
        assert not any(n.endswith('report.txt') for n in archive.namelist())
    # A version replacement cannot retarget a previously recorded receipt.
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute('UPDATE workbench_source_catalog SET version_id=? WHERE matter_id=?', ('f' * 32, matter.matter_id))
    page = client.get(receipt['receipt_url'])
    assert '>Open source</a>' not in page.text and 'Source unavailable' in page.text
    assert receipts.get(matter.matter_id, ACTOR, receipt['receipt_id'])['counts']['received'] == 1


def test_legacy_http_resume_preserves_received_offsets_and_selection(workspace):
    client, bench, matter = workspace
    body = b'Generated legacy upload resumes exactly.\n'
    files = [descriptor('Legacy/report.txt', len(body))]
    old = client.post(f'/matters/{matter.slug}/upload-sessions', json={'files': files, 'collection_name': 'Generated folder'}).json()
    put(client, old['items'][0], body[:7])
    receipt = selection(client, matter.slug, files + [descriptor('Skipped/opaque.bin', 2)], [0])
    resumed = upload(client, matter.slug, receipt, files, [0], resume_session_id=old['upload_session_id'], collection_id=old['collection_id'])
    assert resumed['upload_session_id'] == old['upload_session_id']
    assert resumed['items'][0]['received_size'] == 7
    repeated = upload(client, matter.slug, receipt, files, [0])
    assert repeated['items'][0]['received_size'] == 7
    put(client, resumed['items'][0], body[7:], 7)
    assert client.post(resumed['items'][0]['finalize_url']).status_code == 200
    # Ingestion is still live after finalize. Reads must share its workspace
    # lock too, through cursor consumption, not just writes/transactions.
    with bench.workspace._lock:
        assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_upload_session WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 1


def test_all_skipped_receipts_remain_discoverable_and_csv_safe(workspace):
    client, bench, matter = workspace
    first = None
    for i in range(12):
        receipt = selection(client, matter.slug, [descriptor('=SUM(1+1).bin', 2)], [], key=f'{i:032x}', title=f'Generated selection {i}')
        first = first or receipt
    page1 = client.get(f'/matters/{matter.slug}/setup')
    page2 = client.get(f'/matters/{matter.slug}/setup?receipt_page=2')
    assert 'Older receipts' in page1.text
    assert first['receipt_url'] not in page1.text and first['receipt_url'] in page2.text
    csv_file = client.get(first['receipt_url'] + '/export?format=csv')
    rows = list(csv.reader(io.StringIO(csv_file.content.decode('utf-8-sig'))))
    assert rows[1][5] == "'=SUM(1+1).bin"
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_upload_session WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0
    bundle = client.get(f'/matters/{matter.slug}/export')
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert len([n for n in archive.namelist() if n.startswith('intake/')]) == 36


def test_receipt_http_rejects_cross_matter_and_malformed_batches_without_writes(workspace):
    client, bench, matter = workspace
    receipt = selection(client, matter.slug, [descriptor('One/record.txt', 20)], [0])
    foreign = bench.create_matter('Other synthetic matter', '', 'development-jordan-lee')
    for suffix in ('', '/items', '/seal'):
        url = f"/matters/{foreign.slug}/intake-receipts/{receipt['receipt_id']}" + suffix
        response = client.get(url) if not suffix else client.post(url, json={})
        assert response.status_code == 404
    bad = client.post(f"/matters/{matter.slug}/intake-receipts/{receipt['receipt_id']}/items", json={'start': 0, 'files': [{'name': {'nested': 'invalid'}}], 'reviewed_states': ['valid']})
    assert bad.status_code == 409
    binding = client.post(f'/matters/{matter.slug}/upload-sessions', json={'files': [descriptor('One/record.txt', 20)], 'intake_receipt_id': receipt['receipt_id'], 'intake_ordinals': [{}]})
    assert binding.status_code == 400
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_upload_session WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0


def test_receipt_writes_require_real_session_csrf_and_current_membership(tmp_path):
    from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='preview')
    with TestClient(app) as client:
        assert client.get('/auth/login').status_code == 200
        login = client.post('/auth/login', data={'identity_subject': 'taylor-morgan',
            'login_challenge': client.cookies.get(LOGIN_CHALLENGE_COOKIE), 'next': '/'}, follow_redirects=False)
        assert login.status_code == 303
        landing = client.get('/')
        csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', landing.text).group(1)
        created = client.post('/matters', data={'name': 'Synthetic protected receipt', 'descriptor': '', 'csrf_token': csrf}, follow_redirects=False)
        assert created.status_code == 303
        slug = created.headers['location'].split('/')[2]
        root = f'/matters/{slug}/intake-receipts'
        payload = {'selection_key': 'a'*32, 'selection_fingerprint': 'b'*64,
            'selected_count': 1, 'eligible_indexes': [], 'collection_name': 'Generated selection'}
        assert client.post(root, json=payload).status_code == 403
        headers = {'X-CSRF-Token': csrf}
        response = client.post(root, json=payload, headers=headers)
        assert response.status_code == 201
        receipt = response.json()
        url = root + '/' + receipt['receipt_id']
        assert client.post(url + '/items', json={}).status_code == 403
        assert client.post(url + '/seal').status_code == 403
        assert client.post(receipt['receipt_url'] + '/discard', data={'confirm': 'yes'}).status_code == 403
        bench = app.state.workbench
        matter = bench.matter(slug, ACTOR)
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (ACTOR,))
        assert client.post(url + '/seal', headers=headers, follow_redirects=False).status_code in {303, 401, 403, 404}
        assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_intake_item WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['cancelled', 'missing'])
def test_expired_legacy_checkpoint_keeps_existing_fresh_retry_contract(workspace, kind):
    client, bench, matter = workspace
    files = [descriptor('Legacy/report.txt', 20)]
    if kind == 'cancelled':
        old = client.post(f'/matters/{matter.slug}/upload-sessions', json={'files': files, 'collection_name': 'Generated folder'}).json()
        session_id = old['upload_session_id']
        assert client.post(old['status_url'] + '/cancel').status_code == 200
    else:
        session_id = 'upload-session-' + 'f'*32
    receipt = selection(client, matter.slug, files, [0])
    rejected = client.post(f'/matters/{matter.slug}/upload-sessions', json={'files': files,
        'intake_receipt_id': receipt['receipt_id'], 'intake_ordinals': [0], 'resume_session_id': session_id})
    assert rejected.status_code == 409 and rejected.json()['code'] == 'upload_resume_mismatch'
    assert bench.workspace.connection.execute('SELECT count(*) FROM workbench_intake_transfer WHERE matter_id=?', (matter.matter_id,)).fetchone()[0] == 0
    fresh = upload(client, matter.slug, receipt, files, [0])
    assert fresh['upload_session_id'] != session_id


@pytest.mark.parametrize('outcome', ['no_audio', 'no_speech', 'uncertain', 'failed'])
def test_receipt_distinguishes_held_media_from_failed_transfers(tmp_path, monkeypatch, outcome):
    from tests.test_media_preflight import ObservedProcessor, app_for, silence, wait_job
    import case_intelligence.media_evidence as media
    monkeypatch.setattr(media, 'inspect_recording', lambda *a, **kw: {'outcome': outcome,
        'complete': True, 'quality': [], 'language': 'not_assessed'})
    source = tmp_path/'generated-silence.wav'
    silence(source)
    body = source.read_bytes()
    processor = ObservedProcessor()
    with TestClient(app_for(tmp_path/'runtime', processor, background=True)) as client:
        response = client.post('/matters', data={'name': 'Generated held-media receipt', 'descriptor': ''}, follow_redirects=False)
        slug = response.headers['location'].split('/')[2]
        file = {**descriptor('Recordings/generated-silence.wav', len(body)), 'media_type': 'audio/wav'}
        receipt = selection(client, slug, [file], [0])
        session = upload(client, slug, receipt, [file], [0])
        put(client, session['items'][0], body)
        assert client.post(session['items'][0]['finalize_url']).status_code == 200
        bench = client.app.state.workbench
        matter = bench.matter(slug, ACTOR)
        document = next(iter(bench.source_store(matter).documents.values()))
        wait_job(bench, matter, document)
        counts = client.get(f"/matters/{slug}/intake-receipts/{receipt['receipt_id']}").json()['counts']
        assert counts['received'] == 1 and counts['failed'] == counts['processing'] == counts['searchable'] == 0
        assert counts['playback_only' if outcome == 'no_audio' else 'needs_review'] == 1
        page = client.get(receipt['receipt_url'])
        assert f'/matters/{slug}/sources/{bench.source_store(matter).action_token(document)}' in page.text
        assert processor.submissions == 0


def test_administrator_receipt_read_and_export_follow_existing_audited_boundary(tmp_path):
    from tests.test_group_administrator_authentication import (
        ADMIN, ADMIN_GROUP, OTHER, OWNER, USER_GROUP, _csrf, _headers, _profile, _settings,
    )
    groups = {ADMIN: {ADMIN_GROUP}, OWNER: {USER_GROUP}, OTHER: {USER_GROUP}}
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(),
        auth_mode='kerberos', secure_cookie=True, kerberos_settings=_settings(),
        kerberos_profile_resolver=_profile,
        kerberos_group_resolver=lambda principal: groups.get(principal, set()),
        background_ingestion=False)
    with TestClient(app, base_url=f"https://{LEGACY_STEM}.example.test") as client:
        client.get('/auth/login', headers=_headers(OWNER))
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        created = client.post('/matters', data={'name': 'Generated receipt administrator review',
            'descriptor': '', 'csrf_token': csrf}, headers=_headers(OWNER), follow_redirects=False)
        assert created.status_code == 303
        slug = created.headers['location'].split('/')[2]
        workspace = app.state.workbench.workspace
        owner_id = workspace.connection.execute(
            'SELECT principal_id FROM workbench_principal WHERE provider_subject=?', (OWNER,)).fetchone()[0]
        matter = app.state.workbench.matter(slug, owner_id)
        receipts = IntakeReceipts(workspace)
        receipt = receipts.create(matter.matter_id, owner_id, selection_key='c'*32,
            selection_fingerprint='d'*64, selected_count=1, eligible_indexes=[],
            collection_name='Generated administrator-readable selection')
        receipts.append(matter.matter_id, owner_id, receipt['receipt_id'], start=0,
            files=[descriptor('Generated/unsupported.bin', 3)], reviewed_states=['unsupported'],
            document_limit=1024, media_limit=2048, malware_scan_mode='off', scanner_ready=True)
        receipts.seal(matter.matter_id, owner_id, receipt['receipt_id'])
        root = f'/matters/{slug}/intake-receipts'
        page = f"/matters/{slug}/intake/{receipt['receipt_id']}"
        client.get('/auth/login', headers=_headers(ADMIN))
        admin_csrf = _csrf(client.get('/matters/new', headers=_headers(ADMIN)).text)
        for path in [f'/matters/{slug}/setup', root+'/'+receipt['receipt_id'], page,
                page+'/export?format=csv', page+'/export?format=json', page+'/export?format=markdown']:
            response = client.get(path, headers=_headers(ADMIN))
            assert response.status_code == 200, path
            assert 'Generated administrator-readable selection' in response.text
        assert workspace.connection.execute(
            "SELECT count(*) FROM workbench_audit_event WHERE matter_id=? AND action='matter.admin_access'",
            (matter.matter_id,)).fetchone()[0] >= 6
        denied = client.post(root+'/'+receipt['receipt_id']+'/seal',
            headers={**_headers(ADMIN), 'X-CSRF-Token': admin_csrf})
        assert denied.status_code == 403
        client.get('/auth/login', headers=_headers(OTHER))
        for path in [f'/matters/{slug}/setup', root+'/'+receipt['receipt_id'], page, page+'/export?format=json']:
            denied = client.get(path, headers=_headers(OTHER))
            assert denied.status_code == 404 and 'Generated administrator-readable selection' not in denied.text


def test_receipt_idempotency_does_not_override_a_conflicting_explicit_resume(workspace):
    client, bench, matter = workspace
    files = [descriptor('Generated/report.txt', 20)]
    receipt = selection(client, matter.slug, files, [0])
    current = upload(client, matter.slug, receipt, files, [0])
    put(client, current['items'][0], b'Partial')
    for extra in [
        {'resume_session_id': 'upload-session-'+'f'*32, 'collection_id': current['collection_id']},
        {'resume_session_id': current['upload_session_id'], 'collection_id': 'source-collection-'+'f'*32},
    ]:
        response = client.post(f'/matters/{matter.slug}/upload-sessions', json={
            'files': files, 'collection_name': 'Generated folder', 'intake_receipt_id': receipt['receipt_id'],
            'intake_ordinals': [0], **extra})
        assert response.status_code == 409
        assert response.json()['code'] == 'upload_resume_mismatch'
    # Missing browser checkpoint still recovers the exact bound session.
    resumed = upload(client, matter.slug, receipt, files, [0])
    assert resumed['upload_session_id'] == current['upload_session_id']
    assert resumed['items'][0]['received_size'] == 7
    assert len(bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR)) == 1


@pytest.mark.parametrize('invalid', ['bad\ud800', 'bad\udfff'])
def test_non_utf8_collection_names_have_a_bounded_validation_response(workspace, invalid):
    client, bench, matter = workspace
    payload = {'selection_key': 'e' * 32, 'selection_fingerprint': 'f' * 64,
        'selected_count': 1, 'eligible_indexes': [], 'collection_name': invalid}
    response = client.post(f'/matters/{matter.slug}/intake-receipts',
        content=json.dumps(payload, ensure_ascii=True).encode(), headers={'Content-Type': 'application/json'})
    assert response.status_code == 409 and 'unsupported characters' in response.json()['message']
    assert IntakeReceipts(bench.workspace).recent(matter.matter_id, ACTOR) == []


def test_reported_selection_reason_stays_distinct_from_server_checks_in_every_format(workspace):
    client, bench, matter = workspace
    file = descriptor('Generated/valid.txt', 48)
    preflight = client.post(f'/matters/{matter.slug}/upload-preflight', json={'files': [file]})
    assert preflight.status_code == 200 and preflight.json()['items'][0]['state'] == 'valid'
    root = f'/matters/{matter.slug}/intake-receipts'
    created = client.post(root, json={'selection_key': 'e' * 32, 'selection_fingerprint': 'f' * 64,
        'selected_count': 1, 'eligible_indexes': [], 'collection_name': 'Generated reported selection'})
    assert created.status_code == 201
    receipt = created.json()
    url = root + '/' + receipt['receipt_id']
    assert client.post(url + '/items', json={'start': 0, 'files': [file], 'reviewed_states': ['over_limit']}).status_code == 200
    assert client.post(url + '/seal').status_code == 200
    row = IntakeReceipts(bench.workspace).snapshot(matter.matter_id, ACTOR, receipt['receipt_id'])['items'][0]
    assert row['reviewed_basis'] == 'browser_report' and row['preflight_state'] == 'valid'
    assert row['availability'] == 'not_uploaded' and row['received_size'] == 0
    assert row['reviewed_reason'].startswith('Browser-reported selection review:')
    assert 'Browser-reported selection review:' in client.get(receipt['receipt_url']).text
    for format_name in ('csv', 'json', 'markdown'):
        exported = client.get(receipt['receipt_url'] + '/export', params={'format': format_name})
        assert exported.status_code == 200 and 'Browser-reported selection review:' in exported.text
    assert not bench.workspace.recent_upload_sessions(matter.matter_id, ACTOR)
