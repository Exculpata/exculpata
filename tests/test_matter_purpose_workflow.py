"""C1 through actual HTTP, Home, complete exports and administrator boundaries."""

from tests.fixture_write import fixture_write
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_management import _app, _create_matter, _csrf, _headers, _principal_id, ADMIN, OWNER, OTHER
from tests.test_matter_purpose import OBJECTIVE, BACKGROUND


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    app = create_workbench_app(tmp_path / 'runtime', generator=UnavailableGenerator(), auth_mode='test',
                               background_ingestion=False, learned_retrieval=False)
    with TestClient(app) as client:
        yield client


def test_home_api_edit_conflict_clear_export_fidelity_and_no_consumption(client, monkeypatch, caplog):
    bench = client.app.state.workbench
    actor = 'development-taylor-morgan'
    matter = bench.create_matter('Synthetic orientation workflow', '', actor)
    path = f'/matters/{matter.slug}/purpose'
    home = f'/matters/{matter.slug}/home'
    page = client.get(home)
    assert page.status_code == 200
    assert 'Add an objective.' in page.text and 'Add background when you are ready.' in page.text
    assert "Background text here is separate from a source's Background document role." in page.text
    assert 'expand Source details &amp; extraction, and choose Change source role.' in page.text
    token = re.search(r'name="csrf_token" value="([^\"]+)"', page.text).group(1)
    headers = {'X-CSRF-Token': token}
    values = dict(expected_revision=0, objective=OBJECTIVE, background=BACKGROUND)
    saved = client.put(path, json=values, headers=headers)
    assert saved.status_code == 200
    assert saved.json()['objective'].encode() == OBJECTIVE.encode()
    assert saved.json()['background'].encode() == BACKGROUND.encode()
    assert saved.json() == client.get(path).json()
    assert client.get(path).headers['cache-control'] == 'no-store'
    page = client.get(home)
    assert '&lt;synthetic&gt;' in page.text and '<synthetic>' not in page.text
    assert 'names and dates in your objective can move matching suggested questions first' in page.text
    assert 'Background stays for reviewers; answers do not use either field' in page.text
    assert "Background text here is separate from a source's Background document role." in page.text
    assert 'That designation does not change which sources questions or briefings use.' in page.text
    stale = client.put(path, json=values, headers=headers)
    assert stale.status_code == 409 and OBJECTIVE not in stale.text
    conflict = client.post(path, data=dict(values, csrf_token=token, objective='Unsaved synthetic draft'))
    assert conflict.status_code == 409
    assert 'Unsaved synthetic draft' in conflict.text and 'Current saved version' in conflict.text
    assert 'Save this draft over current version' in conflict.text
    assert client.get(path).json() == saved.json()
    # All-purpose exports add a complete JSON record; notes stay notes.
    note, _ = bench.workspace.create_notebook_item(matter.matter_id, actor,
        item_type='note', status='confirmed', title='Synthetic note', body='Note only fixture')
    bench.source_store(matter).store_stream('synthetic-secret-source.txt', 'text/plain',
        io.BytesIO(b'UNIQUE_SYNTHETIC_SOURCE_BYTES_DO_NOT_EXPORT'))
    bundle = client.get(f'/matters/{matter.slug}/export')
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        purpose = json.loads(archive.read('context/matter-purpose.json'))
        assert {key: purpose[key] for key in saved.json()} == saved.json()
        events = [dict(row) for row in bench.workspace.connection.execute(
            "SELECT * FROM workbench_audit_event WHERE matter_id=? AND action='matter.purpose_update' ORDER BY occurred_at,event_id",
            (matter.matter_id,))]
        assert purpose['audit_events'] == events
        manifest = json.loads(archive.read('manifest.json'))
        assert any(row['kind'] == 'matter_purpose' for row in manifest['additional_work_product'])
        assert all(b'UNIQUE_SYNTHETIC_SOURCE_BYTES_DO_NOT_EXPORT' not in archive.read(name) for name in archive.namelist())
    notes = client.get(f'/matters/{matter.slug}/notebook/export?format=markdown')
    assert notes.status_code == 200 and 'Note only fixture' in notes.text
    assert OBJECTIVE not in notes.text and 'matter-purpose' not in notes.text
    response = client.post(path, data=dict(csrf_token=token, expected_revision=1, objective='', background=''))
    assert response.status_code == 200
    assert client.get(path).json()['objective'] == client.get(path).json()['background'] == ''
    assert OBJECTIVE not in caplog.text and BACKGROUND not in caplog.text


def test_api_validation_and_cross_matter_isolation(client):
    bench = client.app.state.workbench
    first = bench.create_matter('First synthetic orientation', '', 'development-taylor-morgan')
    second = bench.create_matter('Second synthetic orientation', '', 'development-taylor-morgan')
    headers = {'X-CSRF-Token': re.search(r'name="csrf_token" value="([^\"]+)"', client.get(f'/matters/{first.slug}/home').text).group(1)}
    path = f'/matters/{first.slug}/purpose'
    valid = dict(expected_revision=0, objective=OBJECTIVE, background=BACKGROUND)
    for body in (None, [], {}, dict(valid, expected_revision=True), dict(valid, extra='no'),
                 dict(valid, objective='界' * 342), dict(valid, background=None)):
        result = client.put(path, json=body, headers=headers)
        assert result.status_code == 400
        assert OBJECTIVE not in result.text and BACKGROUND not in result.text
    assert client.put(path, content=b'x' * (128 * 1024 + 1), headers=headers).status_code == 413
    assert client.put(path, json=valid, headers=headers).status_code == 200
    assert client.get(f'/matters/{second.slug}/purpose').json()['revision'] == 0


def test_administrator_read_only_nonmember_and_revoked_account(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    app = _app(tmp_path)
    with TestClient(app, base_url='https://testserver') as owner:
        token = _csrf(owner.get('/', headers=_headers(OWNER)).text)
        slug = _create_matter(owner, principal=OWNER, csrf_token=token, name='Synthetic restricted orientation')
        path = f'/matters/{slug}/purpose'
        values = dict(expected_revision=0, objective=OBJECTIVE, background=BACKGROUND)
        assert owner.put(path, json=values, headers=_headers(OWNER)).status_code == 403
        assert owner.put(path, json=values, headers={**_headers(OWNER), 'X-CSRF-Token': token}).status_code == 200
        other = TestClient(app, base_url='https://testserver')
        other_token = _csrf(other.get('/', headers=_headers(OTHER)).text)
        assert other.get(path, headers=_headers(OTHER)).status_code == 404
        assert other.put(path, json=values, headers={**_headers(OTHER), 'X-CSRF-Token': other_token}).status_code == 404
        admin = TestClient(app, base_url='https://testserver')
        admin_token = _csrf(admin.get('/', headers=_headers(ADMIN)).text)
        assert admin.get(path, headers=_headers(ADMIN)).json()['objective'] == OBJECTIVE
        page = admin.get(f'/matters/{slug}/home', headers=_headers(ADMIN))
        assert page.status_code == 200 and 'Join the matter' in page.text
        assert 'name="objective"' not in page.text
        assert "Background text here is separate from a source's Background document role." in page.text
        assert admin.put(path, json=dict(values, expected_revision=1),
            headers={**_headers(ADMIN), 'X-CSRF-Token': admin_token}).status_code == 403
        assert admin.post(path, data=dict(values, csrf_token=admin_token), headers=_headers(ADMIN)).status_code == 403
        store = app.state.workbench.workspace
        actor = _principal_id(owner, OWNER)
        with fixture_write(store):
            store.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (actor,))
        assert owner.get(path, headers=_headers(OWNER), follow_redirects=False).status_code in (302, 303, 401, 403, 404)
