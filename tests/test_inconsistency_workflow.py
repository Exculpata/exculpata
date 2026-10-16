"""Synthetic HTTP authorization, persistence, export and clean-runtime recovery."""
import csv
import io
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_notebook import WEB_ACTOR, _create_matter
from tests.test_statement_comparison import PAIRS


def app(root):
    return create_workbench_app(root, generator=UnavailableGenerator(), auth_mode='test')


def seed(client):
    slug = _create_matter(client, 'Synthetic statement comparison')
    response = client.post(f'/matters/{slug}/uploads', files=[('files',
        ('Synthetic-'+side+'.txt', PAIRS[0][side].encode(), 'text/plain')) for side in ('left', 'right')])
    assert response.status_code == 200
    payload = client.get(f'/matters/{slug}/inconsistencies/export').json()
    assert payload['candidates'], payload
    return slug, payload['candidates'][0]


def test_review_persists_exports_and_clean_restore(tmp_path):
    runtime = tmp_path/'runtime'
    with TestClient(app(runtime)) as client:
        slug, candidate = seed(client)
        prefix = f'/matters/{slug}/inconsistencies'
        page = client.get(prefix)
        assert page.status_code == 200 and '<mark>' in page.text
        assert 'Possible inconsistencies for review.' in page.text
        assert client.post(prefix+'/'+candidate['signature']+'/review', data=dict(action='dismiss', revision=0), follow_redirects=False).status_code == 303
        assert 'inconsistency-card' not in client.get(prefix).text
        assert 'inconsistency-card' in client.get(prefix+'?status=dismissed').text
        dismissed = client.get(prefix+'/export').json()['candidates'][0]
        assert dismissed['status'] == 'dismissed' and dismissed['reviewed_by'] == WEB_ACTOR and dismissed['reviewed_at']
        assert client.post(prefix+'/'+candidate['signature']+'/review', data=dict(action='reviewed', revision=0)).status_code == 409
        assert client.post(prefix+'/'+candidate['signature']+'/review', data=dict(action='reviewed', revision=1)).status_code == 200
        expected = client.get(prefix+'/export').json()
        assert expected['candidates'][0]['status'] == 'reviewed'
        for format_name in ('json', 'markdown', 'csv'):
            response = client.get(prefix+'/export?format='+format_name)
            assert response.status_code == 200 and 'reviewed' in response.text
            assert all(text in response.text.replace('\\', '') for text in ('Synthetic-left.txt', 'Synthetic-right.txt'))
        assert not client.get(prefix+'/export?type=location').json()['candidates']
        witness = candidate['speaker_ids'][0]
        assert client.get(prefix+'/export?witness='+witness).json()['candidates']
        assert not client.get(prefix+'/export?witness=unknown').json()['candidates']
    # Entire runtime is stopped; clean copy includes originals, registry and control DB.
    backup = tmp_path/'backup'
    shutil.copytree(runtime, backup)
    restored = tmp_path/'restored'
    shutil.copytree(backup, restored)
    runtime.rename(tmp_path/'offline-original')
    backup.rename(tmp_path/'offline-backup')
    with TestClient(app(restored)) as client:
        assert client.get(prefix+'/export').json() == expected
        assert client.post(prefix+'/'+candidate['signature']+'/review', data=dict(action='reopen', revision=2)).status_code == 200
        assert client.get(prefix+'/export').json()['candidates'][0]['status'] == 'open'
        workspace = client.app.state.workbench.workspace
        with workspace._lock:
            assert workspace.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not workspace.connection.execute('PRAGMA foreign_key_check').fetchall()


def test_cross_matter_invalid_action_and_current_source_checks(tmp_path):
    with TestClient(app(tmp_path)) as client:
        slug, candidate = seed(client)
        other = _create_matter(client, 'Other synthetic matter')
        assert client.post(f'/matters/{other}/inconsistencies/{candidate["signature"]}/review', data=dict(action='reviewed', revision=0)).status_code == 404
        url = f'/matters/{slug}/inconsistencies/{candidate["signature"]}/review'
        assert client.post(url, data=dict(action='unsupported', revision=0)).status_code == 400
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        store = bench.source_store(matter)
        document = store.get(candidate['left']['document_id'])
        # A source changed out from under the route is exercised without altering source files.
        from unittest.mock import patch
        with patch.object(store, 'ready_documents', return_value=tuple(d for d in store.ready_documents() if d.document_id != document.document_id)):
            assert client.post(url, data=dict(action='reviewed', revision=0)).status_code == 404
            assert not client.get(f'/matters/{slug}/inconsistencies/export').json()['candidates']


def test_csv_formula_injection_is_escaped():
    from case_intelligence.statement_comparison_exports import export_payload
    row = dict(signature='abc', scope='same_witness', difference_type='time_date', note='Review', status='open', reviewed_by=None, reviewed_at=None)
    row.update({side: dict(source_name='=1+1', location='Page 1', excerpt='  @SUM(A1)') for side in ('left','right')})
    result = list(csv.reader(io.StringIO(export_payload(dict(candidates=[row]), 'csv'))))
    assert result[1][7] == "'=1+1" and result[1][9].startswith("'")


def test_protected_csrf_and_revoked_member(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, _app as protected_app, _csrf, _headers, _principal_id
    from tests.test_matter_management import _create_matter as create_protected_matter
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(protected_app(tmp_path), base_url='https://exculpata.example.test') as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = create_protected_matter(client, principal=OWNER, csrf_token=csrf, name='Synthetic protected comparison')
        assert client.post(f'/matters/{slug}/uploads', headers=_headers(OWNER), data={'csrf_token':csrf},
            files=[('files', ('Synthetic-'+s+'.txt', PAIRS[0][s].encode(), 'text/plain')) for s in ('left','right')]).status_code == 200
        prefix = f'/matters/{slug}/inconsistencies'
        candidate = client.get(prefix+'/export', headers=_headers(OWNER)).json()['candidates'][0]
        url = prefix+'/'+candidate['signature']+'/review'
        fields = dict(action='reviewed', revision=0)
        assert client.post(url, headers=_headers(OWNER), data=fields).status_code == 403
        assert client.post(url, headers=_headers(OWNER), data=dict(fields, csrf_token=csrf), follow_redirects=False).status_code == 303
        bench = client.app.state.workbench
        owner = _principal_id(client, OWNER)
        matter = bench.matter(slug, owner)
        client.cookies.clear()
        client.get('/matters/new', headers=_headers(OTHER))
        member = _principal_id(client, OTHER)
        bench.workspace.add_member(matter.matter_id, member, owner)
        opened = client.get(prefix, headers=_headers(OTHER))
        assert opened.status_code == 200
        member_csrf = _csrf(opened.text)
        bench.workspace.revoke_member(matter.matter_id, member, owner)
        for path in (prefix, prefix+'/export', prefix+'/export?format=csv', prefix+'/export?format=markdown'):
            assert client.get(path, headers=_headers(OTHER)).status_code == 404
        assert client.post(url, headers=_headers(OTHER), data=dict(fields, csrf_token=member_csrf, revision=1)).status_code == 404


def test_review_state_in_full_archive_frozen_and_purged(tmp_path):
    import sqlite3
    import zipfile
    import pytest
    from tests.case_lifecycle_web_helpers import claim_bench_exit
    with TestClient(app(tmp_path)) as client:
        slug, candidate = seed(client)
        prefix = f'/matters/{slug}/inconsistencies'
        assert client.post(prefix+'/'+candidate['signature']+'/review', data=dict(action='reviewed', revision=0)).status_code == 200
        bench = client.app.state.workbench
        matter = bench.matter(slug, WEB_ACTOR)
        prepared, lifecycle = claim_bench_exit(bench, matter)
        from case_intelligence.case_lifecycle_support import archive_path
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        with zipfile.ZipFile(archive_path(bench, matter, current)) as archive:
            rows = archive.read('records/workbench_candidate_review.jsonl').decode()
            assert candidate['signature'] in rows and 'reviewed' in rows
        with bench.workspace._lock, pytest.raises(sqlite3.IntegrityError):
            bench.workspace.connection.execute("UPDATE workbench_candidate_review SET status='dismissed' WHERE matter_id=?", (matter.matter_id,))
        bench.execute_matter_purge(prepared, lifecycle)
        with bench.workspace._lock:
            assert not bench.workspace.connection.execute('SELECT * FROM workbench_candidate_review WHERE matter_id=?', (matter.matter_id,)).fetchall()


def test_markdown_source_content_is_escaped():
    from case_intelligence.statement_comparison_exports import export_payload
    citation = dict(source_name='<img src=x>', location='Page 1', excerpt='<script>alert(1)</script>\n# Untrusted heading', document_id='synthetic', source_version_id='v1')
    row = dict(signature='abc', scope='same_witness', difference_type='time_date', note='<script>source text</script>', status='open', reviewed_by=None, reviewed_at=None, left=citation, right=citation)
    body = export_payload(dict(format='exculpata-inconsistency-candidates-v1', candidates=[row], dropped=0, capped=False), 'markdown')
    assert '<script>' not in body and '<img' not in body
    assert '\\# Untrusted heading' in body
