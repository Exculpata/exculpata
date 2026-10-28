"""Synthetic HTTP contracts for the approved 378c2 summary wiring."""
from dataclasses import replace
import hashlib
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from case_intelligence import assertion_repository, production_summary as summary
from case_intelligence.bates_gaps import compute_bates_gaps
from case_intelligence.workspace_store import WorkspaceProblem
from tests.test_compact_matter_navigation import navigation
from tests.test_production_summary import (
    HEADER, acceptance, fact_payload, link, production, production_client,  # noqa: F401
)

ENDPOINTS = ('', '/export?format=markdown', '/export?format=json')


def request_summary(p, endpoint='', *, load_file=None):
    return p.client.get(f'/matters/{p.slug}/production-summary' + endpoint
                        + ('&' if '?' in endpoint else '?')
                        + urlencode({'load_file': p.store.action_token(p.load) if load_file is None else load_file}))


def exports(p):
    return [event for event in export_events(p) if event.outcome == 'success']


def export_events(p):
    return [event for event in p.client.app.state.workbench.workspace.audit_events(p.matter.matter_id)
            if event.action == 'production_summary.export']


def test_T1_page_and_exports(production_client):
    p = acceptance(production_client)
    page, markdown, data = [request_summary(p, endpoint) for endpoint in ENDPOINTS]
    for response in (page, markdown, data):
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
    for response in (page, markdown):
        assert HEADER in response.text
        assert 'Possible missing Bates ABC0004' in response.text
        assert 'Bates series ABC: gaps outside ABC0001 to ABC0005 not computed' in response.text
        assert 'Missing: 0' in response.text and 'Possible: 1' in response.text
        for doc in p.docs[:2] + p.docs[4:6]:
            assert link(p, doc) in response.text and doc.version_id in response.text
        assert 'failed.pdf' in response.text and 'unreadable.pdf' in response.text
    assert 'name="load_file"' in page.text and 'load.csv' in page.text
    assert f'value="{p.store.action_token(p.load)}" selected' in page.text
    tabs = navigation(page.text, 'Case file sections')
    assert next(t for t in tabs if t['text'] == 'Production summary')['current'] == 'page'
    assert data.json()['facts'] == fact_payload(p)
    assert data.json()['load_file']['sha256'] == hashlib.sha256(p.raw).hexdigest()
    assert markdown.headers['content-type'].startswith('text/markdown')
    assert data.headers['content-type'].startswith('application/json')
    assert 'production-summary.md' in markdown.headers['content-disposition']
    assert 'production-summary.json' in data.headers['content-disposition']
    assert len(exports(p)) == 2
    unselected = p.client.get(f'/matters/{p.slug}/production-summary')
    assert unselected.status_code == 200
    assert unselected.text.count('Bates gaps not computed: no load file selected') == 1
    uploaded = p.client.post(f'/matters/{p.slug}/uploads',
                            files=[('files', ('new.txt', b'Synthetic new source.', 'text/plain'))])
    assert uploaded.status_code == 200
    fresh = request_summary(p, '/export?format=json')
    assert fresh.status_code == 200 and 'Documents: 8' in fresh.text


@pytest.mark.parametrize('endpoint', ENDPOINTS[:2])
def test_T4_no_load_file_counts(production_client, endpoint):
    p = acceptance(production_client)
    response = request_summary(p, endpoint, load_file='')
    assert response.status_code == 200
    assert response.text.count('Bates gaps not computed: no load file selected') == 1
    assert 'Missing: 0' not in response.text and 'Possible: 0' not in response.text


@pytest.mark.parametrize('change', ['document', 'load_file', 'load_file_bytes', 'citation'])
@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_T6_snapshot_conflict(production_client, monkeypatch, change, endpoint):
    p = acceptance(production_client)
    def changed(docs):
        facts = compute_bates_gaps(docs)
        if change == 'citation':
            return (replace(facts[0], cited_ids=(('absent', 'version'),)), *facts[1:])
        if change == 'load_file_bytes':
            # fixture-write: corrupt this synthetic source after gap computation to test the final digest check.
            p.store.source_path(p.load.document_id).write_bytes(p.raw.replace(b'ABC0005', b'ABC0006'))
            return facts
        doc = p.load if change == 'load_file' else p.docs[0]
        altered = replace(doc, digest='f' * 64) if change == 'load_file' else replace(doc, version_id='f' * 32)
        monkeypatch.setitem(p.store.documents, doc.document_id, altered)
        return facts
    monkeypatch.setattr(summary, 'compute_bates_gaps', changed)
    response = request_summary(p, endpoint)
    assert response.status_code == 409
    assert 'Possible missing Bates' not in response.text and 'content-disposition' not in response.headers
    assert not export_events(p)


def test_T7_nonmember(tmp_path, monkeypatch):
    from tests.test_matter_management import OWNER, OTHER, _app, _csrf, _headers
    from tests.test_matter_management import _create_matter
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path), base_url='https://exculpata.example.test') as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = _create_matter(client, principal=OWNER, csrf_token=csrf, name='Synthetic protected production')
        prefix = f'/matters/{slug}/production-summary'
        assert client.get(prefix, headers=_headers(OWNER)).status_code == 200
        workspace = client.app.state.workbench.workspace
        matter = workspace.get_active_matter(slug)
        client.cookies.clear()
        for endpoint in ENDPOINTS:
            response = client.get(prefix + endpoint, headers=_headers(OTHER))
            assert response.status_code == 404 and 'Synthetic protected production' not in response.text
            assert 'content-disposition' not in response.headers
        assert not [event for event in workspace.audit_events(matter.matter_id)
                    if event.action == 'production_summary.export']


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_T7_membership_revoked_mid_request(production_client, monkeypatch, endpoint):
    p = acceptance(production_client)
    workspace = p.client.app.state.workbench.workspace
    membership = workspace.membership
    calls = 0
    def revoked(matter_id, actor):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise KeyError('Synthetic membership revoked')
        return membership(matter_id, actor)
    monkeypatch.setattr(workspace, 'membership', revoked)
    response = request_summary(p, endpoint)
    assert response.status_code == 404
    assert 'content-disposition' not in response.headers
    assert 'Possible missing Bates' not in response.text
    assert exports(p) == []
    assert export_events(p) == []
    assert calls >= 2


@pytest.fixture
def load_file_selection(production_client):
    p = acceptance(production_client)
    bad_csv = b'wrong_header\nSynthetic private load row\n'
    response = p.client.post(f'/matters/{p.slug}/uploads',
                             files=[('files', ('bad.csv', bad_csv, 'text/csv'))])
    assert response.status_code == 200
    bad = next(doc for doc in p.store.documents.values() if doc.relative_path == 'bad.csv')
    other = production(production_client, [('other.txt', b'Synthetic other source.', 'text/plain')],
                       [('other.txt', 'OTHER0001', '')])
    return p, other, {'garbage': 'zzz', 'other_matter': other.store.action_token(other.load),
                      'non_csv': p.store.action_token(p.docs[0]), 'bad_header': p.store.action_token(bad)}


@pytest.mark.parametrize('selection,status', [('garbage', 409), ('other_matter', 409),
                                            ('non_csv', 400), ('bad_header', 400)])
@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_load_file_selection_rejected(load_file_selection, selection, status, endpoint):
    p, other, tokens = load_file_selection
    response = request_summary(p, endpoint, load_file=tokens[selection])
    assert response.status_code == status
    assert 'content-disposition' not in response.headers
    for text in ('Possible missing Bates', 'Documents:', 'Synthetic production',
                 'Synthetic private load row', 'Synthetic other source.', 'OTHER0001'):
        assert text not in response.text
    for source in (p, other):
        for doc in source.store.documents.values():
            assert doc.display_name not in response.text
            assert doc.version_id not in response.text
        assert export_events(source) == []


def test_unsupported_export_format(production_client):
    p = acceptance(production_client)
    response = request_summary(p, '/export?format=xml')
    assert response.status_code == 422
    assert 'content-disposition' not in response.headers
    assert export_events(p) == []


@pytest.mark.parametrize('format', ['markdown', 'json'])
def test_T7_oversized_export(production_client, monkeypatch, format):
    p = acceptance(production_client)
    monkeypatch.setattr(assertion_repository, 'MAX_STORAGE_BYTES', 1)
    response = request_summary(p, '/export?format=' + format)
    assert response.status_code == 400 and response.content == b''
    assert 'content-disposition' not in response.headers and not exports(p)


@pytest.mark.parametrize('endpoint', ENDPOINTS)
def test_response_lease_denial(production_client, monkeypatch, endpoint):
    p = acceptance(production_client)
    def denied(*args, **kwargs):
        raise WorkspaceProblem('Synthetic lease denial')
    monkeypatch.setattr(p.client.app.state.workbench, 'begin_matter_response', denied)
    response = request_summary(p, endpoint)
    assert response.status_code == 409 and 'Possible missing Bates' not in response.text
    assert not exports(p)
