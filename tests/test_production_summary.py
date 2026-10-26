"""Final HTTP contracts for the approved 378c production summary."""
import csv
from dataclasses import asdict, replace
import hashlib
import html
import io
from itertools import count
import json
from pathlib import Path
from types import SimpleNamespace
import unicodedata
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from case_intelligence import pilot_uploads, production_summary as summary
from case_intelligence.bates_gaps import BatesDocument, compute_bates_gaps
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.timeline_exports import md_inline
from case_intelligence.workbench import create_workbench_app
from tests.test_matter_notebook import _create_matter, WEB_ACTOR
from tests.test_review_tools import CleanScanner
from tests.test_workflow_quality_handoff import pdf_bytes

HEADER = ('Missing means no supplied label could cover the number. Possible means a label might. '
          'An empty list does not show the production is complete.')

@pytest.fixture(scope='module')
def production_client(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
        patch.setenv('CASE_INTELLIGENCE_OCR_MODE', 'disabled')
        serial = count(1)
        patch.setattr(pilot_uploads, 'uuid', SimpleNamespace(uuid4=lambda: UUID(int=next(serial))))
        with TestClient(create_workbench_app(tmp_path_factory.mktemp('production'), auth_mode='test',
                                            generator=UnavailableGenerator(), malware_scanner=CleanScanner())) as client:
            yield client


def production(client, files, rows):
    slug = _create_matter(client, 'Synthetic production')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(('path', 'begin_bates', 'end_bates'))
    writer.writerows(rows)
    raw = stream.getvalue().encode()
    uploads = [*files, ('load.csv', raw, 'text/csv')]
    for start in range(0, len(uploads), 10):
        uploaded = client.post(f'/matters/{slug}/uploads', files=[('files', f) for f in uploads[start:start+10]])
        assert uploaded.status_code == 200, uploaded.text
    bench = client.app.state.workbench
    matter = bench.matter(slug, WEB_ACTOR)
    store = bench.source_store(matter)
    docs = list(store.documents.values())
    load = next(d for d in docs if d.relative_path == 'load.csv')
    return SimpleNamespace(client=client, slug=slug, store=store, docs=docs, load=load, raw=raw,
                           rows=[dict(zip(('path', 'begin_bates', 'end_bates'), r)) for r in rows],
                           url=f'/matters/{slug}/production-summary', matter=matter)


def acceptance(client, extra=()):
    files = [(name, pdf_bytes(['Synthetic page.'] * pages), 'application/pdf') for name, pages in
             [('first_[a].pdf', 3), ('next.pdf', 1), ('suffix.pdf', 1)]]
    files += [('mail.eml', b'From: sender@example.test\nSubject: Synthetic\n\n', 'message/rfc822'),
              ('failed.pdf', b'%PDF-1.4\nmalformed synthetic file', 'application/pdf'),
              ('unreadable.pdf', pdf_bytes(['']), 'application/pdf')]
    return production(client, files, [('first_[a].pdf', 'ABC0001', 'ABC0003'), ('next.pdf', 'ABC0005', ''),
                                     ('suffix.pdf', 'ABC0007.A', ''), ('mail.eml', 'XYZ0001', ''), *extra])


def response(p, format=None, selected=True):
    params = {'load_file': p.store.action_token(p.load)} if selected else {}
    if format:
        params['format'] = format
    return p.client.get(p.url + ('/export' if format else ''), params=params)


def link(p, doc):
    return f'/matters/{p.slug}/sources/{p.store.action_token(doc)}'


def expected(p):
    real = lambda r: any('0' <= c <= '9' for c in unicodedata.normalize('NFKC', r['begin_bates']))
    return tuple(BatesDocument(d.document_id, d.version_id,
        tuple((r['begin_bates'], r['end_bates'] or None) for r in p.rows if r['path'] == d.relative_path and real(r)),
        d.page_count if d.media_type == 'application/pdf' else None,
        d.state == 'ready' and any(r['path'] == d.relative_path for r in p.rows)
        and all(real(r) for r in p.rows if r['path'] == d.relative_path))
        for d in p.docs if d.document_id != p.load.document_id)


def fact_payload(p):
    return json.loads(json.dumps([asdict(f) for f in compute_bates_gaps(expected(p))]))


def test_T1_page_and_exports(production_client):
    p = acceptance(production_client)
    assert [(d.state, d.page_count) for d in p.docs[:6]] == [('ready', 3), ('ready', 1),
        ('ready', 1), ('ready', 1), ('failed', 0), ('needs_ocr', 1)]
    for format in (None, 'markdown'):
        result = response(p, format)
        assert result.status_code == 200
        text = html.unescape(result.text)
        assert HEADER in text and 'Possible missing Bates ABC0004' in text
        assert 'Bates series ABC: gaps outside ABC0001 to ABC0005 not computed' in text
        assert 'Missing Bates' not in text.replace('Possible missing Bates', '')
        gap = next(line for line in text.splitlines() if 'Possible missing Bates ABC0004' in line)
        for doc in p.docs[:2]:
            assert link(p, doc) in gap and doc.version_id in gap
        for doc in p.docs[4:6]:
            assert link(p, doc) in text and doc.version_id in text
        assert 'failed' in text and ('needs_ocr' in text if format is None else md_inline('needs_ocr') in text)
    payload = response(p, 'json')
    assert payload.status_code == 200 and payload.json()['facts'] == fact_payload(p)
    assert payload.json()['load_file']['sha256'] == hashlib.sha256(p.raw).hexdigest()
    assert summary.parse_load_file(p.raw) == p.rows
    assert summary.map_documents(p.docs, p.rows, p.load.document_id) == expected(p)
    page = response(p)
    assert 'name="load_file"' in page.text and p.store.action_token(p.load) in page.text
    assert p.url in p.client.get(f'/matters/{p.slug}').text
    for doc in p.docs:
        original = p.client.get(link(p, doc) + '/content')
        assert original.status_code == 200 and hashlib.sha256(original.content).hexdigest() == doc.digest


def test_T2_markdown_golden(production_client):
    p = acceptance(production_client)
    result = response(p, 'markdown')
    assert result.status_code == 200
    text = result.text.replace(p.slug, 'synthetic')
    for i, doc in enumerate(p.docs, 1):
        text = text.replace(p.store.action_token(doc), f'source-{i}').replace(doc.version_id, f'v{i}')
    assert text.encode() == (Path(__file__).parent / 'fixtures/production_summary/golden.md').read_bytes()


@pytest.mark.parametrize('selected', [False, True])
def test_T4_no_load_or_absent_path(production_client, selected):
    p = acceptance(production_client, [('absent.pdf', 'ABC0004', '')])
    result = response(p, 'markdown', selected)
    assert result.status_code == 200
    if not selected:
        assert result.text.count('Bates gaps not computed: no load file selected') == 1
        assert 'Missing Bates' not in result.text and 'Possible missing Bates' not in result.text
    else:
        row = next(line for line in result.text.splitlines() if 'Load file row 6 names no document' in line)
        assert 'in this production' in row and link(p, p.load) in row
        assert 'Possible missing Bates ABC0004' in result.text


@pytest.mark.parametrize('begin', ['', ' ', 'N/A', 'SLIPSHEET'])
def test_T4b_placeholder(production_client, begin):
    test_T4c_failed_document(production_client, begin)
    p = acceptance(production_client)
    assert summary.map_documents([p.docs[3]], p.rows, p.load.document_id)[0].page_count is None


def test_T4c_failed_document(production_client, begin='ABC0005'):
    files = [(name, b'%PDF-1.4\nmalformed synthetic file' if name == 'b.pdf' and begin == 'ABC0005'
              else pdf_bytes(['Synthetic page.']), 'application/pdf') for name in ('a.pdf', 'b.pdf', 'c.pdf')]
    p = production(production_client, files, [('a.pdf', 'ABC0001', ''), ('b.pdf', begin, ''), ('c.pdf', 'ABC0003', '')])
    assert p.docs[1].state == ('failed' if begin == 'ABC0005' else 'ready')
    result = response(p, 'markdown')
    assert result.status_code == 200 and 'Possible missing Bates ABC0002' in result.text
    assert '- Missing Bates' not in result.text and (begin == 'ABC0005' or 'Bates labels not read' not in result.text)


def test_T5_invisible_label(production_client):
    p = production(production_client, [('label.txt', b'Synthetic label source.', 'text/plain')],
                   [('label.txt', 'ABC\u200b0001', '')])
    result = response(p, 'markdown')
    assert result.status_code == 200
    assert md_inline('[U+200B]') in result.text and 'Bates series ALL: not computed' in result.text
    assert '\u200b' not in result.text
    payload = response(p, 'json').json()
    assert payload['load_rows'][0]['begin_bates'] == 'ABC\u200b0001'
    assert payload['facts'] == fact_payload(p)


@pytest.mark.parametrize('change', ['document', 'load_file', 'citation'])
@pytest.mark.parametrize('format', [None, 'markdown', 'json'])
def test_T6_snapshot_conflict(production_client, monkeypatch, change, format):
    p = acceptance(production_client)
    def changed(docs):
        facts = compute_bates_gaps(docs)
        if change == 'citation':
            return (replace(facts[0], cited_ids=(('absent', 'version'),)), *facts[1:])
        doc = p.load if change == 'load_file' else p.docs[0]
        altered = replace(doc, digest='f' * 64) if change == 'load_file' else replace(doc, version_id='f' * 32)
        monkeypatch.setitem(p.store.documents, doc.document_id, altered)
        return facts
    monkeypatch.setattr(summary, 'compute_bates_gaps', changed)
    result = response(p, format)
    assert result.status_code == 409
    assert 'Possible missing Bates ABC0004' not in result.text


def test_T7_membership_audit_lease_and_limit(production_client, monkeypatch):
    p = acceptance(production_client)
    assert response(p).status_code == 200
    assert p.client.post(f'/matters/{p.slug}/uploads', files=[
        ('files', ('later.txt', b'Synthetic later source.', 'text/plain'))]).status_code == 200
    assert 'later.txt' in response(p, 'markdown').text
    assert response(p, 'markdown').status_code == 200
    bench = p.client.app.state.workbench
    assert any(e.action == 'production_summary.export' and e.outcome == 'success'
               for e in bench.workspace.audit_events(p.matter.matter_id))
    identity = p.client.app.state.identity
    context = identity.resolve(None)
    with monkeypatch.context() as patch:
        patch.setattr(identity, 'resolve', lambda *a, **k: replace(context,
                      principal=bench.workspace.get_principal('development-jordan-lee')))
        for format in (None, 'markdown', 'json'):
            assert response(p, format).status_code == 404
    from case_intelligence.workspace_store import WorkspaceProblem
    def denied(*args, **kwargs):
        raise WorkspaceProblem('Synthetic lease denial')
    with monkeypatch.context() as patch:
        patch.setattr(bench, 'begin_matter_response', denied)
        assert response(p, 'markdown').status_code == 409
    monkeypatch.setattr('case_intelligence.assertion_repository.MAX_STORAGE_BYTES', 1)
    for format in ('markdown', 'json'):
        result = response(p, format)
        assert result.status_code == 400 and result.content == b''
