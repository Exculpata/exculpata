
from tests.fixture_write import fixture_write
from tests.case_lifecycle_web_helpers import claim_bench_exit
"""C2 through HTTP, actual intake retries and a stopped-runtime recovery drill."""
import io
import json
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.source_roles import SourceRoleService
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_management import _app, _create_matter, _csrf, _headers, _principal_id, ADMIN, OWNER, OTHER
from tests.test_intake_receipt_http import ACTOR, descriptor, selection, upload, put, workspace  # noqa: F401
from tests.test_upload_occurrences import _settled
from tests.test_source_roles import snapshot


def application(runtime):
    return create_workbench_app(runtime, generator=UnavailableGenerator(), auth_mode='test',
        background_ingestion=False, learned_retrieval=False)


def add_source(bench, matter):
    store = bench.source_store(matter)
    document, _ = store.store_stream('synthetic-charge.txt', 'text/plain', io.BytesIO(b'Synthetic allegation: copper arrived at noon.'))
    bench._ensure_source_organizations(matter, store)
    bench.index_document(matter, document.document_id)
    return document, store.action_token(document)


def role(client, path):
    response = client.get(path, headers={'Accept': 'application/json'})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_editor_api_conflict_clear_export_and_no_query_changes(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(application(tmp_path / 'runtime')) as client:
        bench = client.app.state.workbench
        matter = bench.create_matter('Synthetic charging workflow', 'Original descriptor', ACTOR)
        document, token = add_source(bench, matter)
        path = f'/matters/{matter.slug}/sources/{token}/role'
        original = role(client, path)
        assert original['source_role'] == 'unclassified'
        source_bytes = bench.source_store(matter).source_path(document.document_id).read_bytes()
        fingerprint = bench.workspace.source_availability_fingerprint(matter.matter_id)
        sources_before = bench.exact_search(matter, 'copper')
        bench.workspace.update_matter_purpose(matter.matter_id, ACTOR, expected_revision=0,
            objective='Synthetic unused objective', background='Synthetic unused background', request_id='synthetic-purpose')
        before_sets = bench.workspace.source_sets(matter.matter_id)
        assert 'Change source role' in client.get(path.removesuffix('/role')).text
        values = dict(expected_revision=0, source_role='background')
        assert client.put(path, json=values).status_code == 200
        saved = role(client, path)
        assert saved['revision'] == 1
        assert client.put(path, json=values).status_code == 409
        assert client.put(path, json=dict(values, expected_revision=1)).json() == saved
        page = client.post(path, data=dict(expected_revision=0, source_role='unclassified'))
        assert page.status_code == 409 and 'Your unsaved choice: Unclassified' in page.text
        assert 'Current role: <strong>Background document' in page.text
        assert role(client, path) == saved
        assert 'Background document' in client.get(f'/matters/{matter.slug}/setup?view=list').text
        assert 'Role: Background document' in client.get(path.removesuffix('/role')).text
        bundle = client.get(f'/matters/{matter.slug}/export')
        assert bundle.status_code == 200, bundle.text
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            payload = json.loads(archive.read('context/source-roles.json'))
            assert payload['sources'] == [saved]
            assert json.loads(payload['audit_events'][0]['details_json']) == {}
            assert all(source_bytes not in archive.read(name) for name in archive.namelist())
        assert bench.exact_search(matter, 'copper') == sources_before
        assert bench.workspace.source_availability_fingerprint(matter.matter_id) == fingerprint
        assert bench.workspace.source_sets(matter.matter_id) == before_sets
        assert not bench.workspace.answer_jobs(matter.matter_id, ACTOR)
        assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)
        assert bench.workspace.get_matter(matter.slug, ACTOR).descriptor == 'Original descriptor'
        assert bench.source_store(matter).source_path(document.document_id).read_bytes() == source_bytes
        for body in (None, [], {}, dict(values, extra='no'), dict(values, expected_revision=True), dict(values, source_role='discovery')):
            assert client.put(path, json=body).status_code == 400
        assert client.put(path, content=b'x' * 4097).status_code == 413
        assert client.post(path, data=dict(expected_revision=1, source_role='unclassified')).status_code == 200
        assert role(client, path)['revision'] == 2
        assert 'Synthetic unused' not in caplog.text


def test_resumable_intake_retries_duplicate_occurrences_and_catalog_rebuild(workspace):
    client, bench, matter = workspace
    body = b'Synthetic charging paper occurrence.\n'
    records = []
    for index in (1, 2):
        files = [descriptor('Records/charge.txt', len(body))]
        receipt = selection(client, matter.slug, files, [0], key=str(index) * 32, title=f'Synthetic intake {index}')
        session = upload(client, matter.slug, receipt, files, [0], collection_name=f'Synthetic intake {index}')
        item = session['items'][0]
        put(client, item, body)
        assert client.post(item['finalize_url']).status_code == 200
        _settled(bench, matter)
        entry = bench.source_library(matter, collection_id=session['collection_id']).items[0]
        path = f'/matters/{matter.slug}/sources/{entry.action_token}/role'
        assert role(client, path)['source_role'] == 'unclassified'
        if index == 1:
            assert client.put(path, json=dict(expected_revision=0, source_role='background')).status_code == 200
        records.append((path, role(client, path)))
        assert client.post(item['finalize_url']).status_code == 200
    bench._sync_source_catalog(matter, tuple(bench.source_store(matter).documents.values()))
    for path, expected in records:
        assert role(client, path) == expected
    assert records[0][1]['source_role'] == 'background' and records[1][1]['source_role'] == 'unclassified'


def test_stopped_runtime_restore_original_navigation_export_and_purge(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    runtime = tmp_path / 'runtime'
    with TestClient(application(runtime)) as client:
        bench = client.app.state.workbench
        matter = bench.create_matter('Synthetic role recovery', '', ACTOR)
        document, token = add_source(bench, matter)
        path = f'/matters/{matter.slug}/sources/{token}/role'
        assert client.put(path, json=dict(expected_revision=0, source_role='background')).status_code == 200
        expected = role(client, path)
        before = snapshot(bench.workspace, matter.matter_id)
    restored_path = tmp_path / 'clean-runtime'
    shutil.copytree(runtime, restored_path)
    runtime.rename(tmp_path / 'unavailable-original-runtime')
    with TestClient(application(restored_path)) as restored:
        bench = restored.app.state.workbench
        assert role(restored, path) == expected
        assert snapshot(bench.workspace, matter.matter_id) == before
        reader = restored.get(path.removesuffix('/role'))
        assert reader.status_code == 200 and 'Synthetic allegation: copper arrived at noon.' in reader.text
        bundle = restored.get(f'/matters/{matter.slug}/export')
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert json.loads(archive.read('context/source-roles.json'))['sources'] == [expected]
        prepared, lifecycle = claim_bench_exit(bench, matter)
        bench.execute_matter_purge(prepared, lifecycle)
        assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted'
        assert not bench.workspace.connection.execute('SELECT * FROM workbench_source_organization WHERE matter_id=?', (matter.matter_id,)).fetchall()
        assert restored.get(path).status_code == 404
        assert len([e for e in bench.workspace.audit_events(matter.matter_id) if e.action == 'source.role_update']) == 1


def test_csrf_admin_read_only_foreign_source_revoked_and_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    app = _app(tmp_path)
    with TestClient(app, base_url='https://testserver') as owner:
        csrf = _csrf(owner.get('/', headers=_headers(OWNER)).text)
        slug = _create_matter(owner, principal=OWNER, csrf_token=csrf, name='Synthetic restricted roles')
        bench = app.state.workbench
        actor = _principal_id(owner, OWNER)
        matter = bench.matter(slug, actor)
        _, token = add_source(bench, matter)
        path = f'/matters/{slug}/sources/{token}/role'
        values = dict(expected_revision=0, source_role='background')
        assert owner.put(path, json=values, headers=_headers(OWNER)).status_code == 403
        assert owner.put(path, json=values, headers={**_headers(OWNER), 'X-CSRF-Token': csrf}).status_code == 200
        for principal in (OTHER, ADMIN):
            client = TestClient(app, base_url='https://testserver')
            token_csrf = _csrf(client.get('/', headers=_headers(principal)).text)
            read = client.get(path, headers={**_headers(principal), 'Accept': 'application/json'})
            assert read.status_code == (200 if principal == ADMIN else 404)
            assert client.put(path, json=dict(values, expected_revision=1), headers={**_headers(principal), 'X-CSRF-Token': token_csrf}).status_code == (403 if principal == ADMIN else 404)
            if principal == ADMIN:
                assert 'source-role-form' not in client.get(path, headers=_headers(principal)).text
        other = bench.create_matter('Synthetic other roles', '', actor)
        assert owner.get(f'/matters/{other.slug}/sources/{token}/role', headers=_headers(OWNER)).status_code == 404
        with fixture_write(bench.workspace):
            bench.workspace.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (actor,))
        assert owner.get(path, headers=_headers(OWNER), follow_redirects=False).status_code in (302, 303, 401, 403, 404)


def test_source_removal_drops_only_its_role_and_keeps_content_free_audit(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(application(tmp_path / 'runtime')) as client:
        bench = client.app.state.workbench
        matter = bench.create_matter('Synthetic role removal', '', ACTOR)
        document, token = add_source(bench, matter)
        path = f'/matters/{matter.slug}/sources/{token}'
        assert client.put(path + '/role', json=dict(expected_revision=0, source_role='background')).status_code == 200
        assert client.post(path + '/remove').status_code == 200
        assert client.get(path + '/role').status_code == 404
        assert not bench.workspace.connection.execute('SELECT * FROM workbench_source_organization WHERE document_id=?', (document.document_id,)).fetchall()
        bundle = client.get(f'/matters/{matter.slug}/export')
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            data = json.loads(archive.read('context/source-roles.json'))
            assert data['sources'] == [] and len(data['audit_events']) == 1
