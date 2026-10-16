"""Synthetic production summary behavior and authorization boundaries."""
import json
from pathlib import Path
import time

import pytest

from case_intelligence.production_summary import ProductionSummaryService, NOTICE
from case_intelligence.production_summary_exports import export_summary
from tests.test_briefing import workbench, upload_path, ready, assemble
from tests.test_automatic_discovery import upload
from tests.test_matter_notebook import WEB_ACTOR
from tests.test_demo import demo_script


def summarize(bench, matter, **kwargs):
    return ProductionSummaryService(bench, matter, WEB_ACTOR).summarize(matter.matter_id, generated_at='2034-04-20T00:00:00Z', **kwargs)


def test_scope_counts_dates_references_failures_and_passages(workbench):
    client, bench, matter, _ = workbench
    first = upload_path(client, bench, matter, 'production/statement.txt', b'Person: Alex Example; Organization: Amber Cooperative; 2034-04-17. See Exhibit 12.\nBates: ABC0001 to ABC0003', 'a')
    upload_path(client, bench, matter, 'production/second.txt', b'Alex Example arrived on 2034-04-19. Bates: ABC0006\nAttachment B', 'b')
    upload_path(client, bench, matter, 'other/exhibit-12.txt', b'Synthetic exhibit dated 2034-04-18.', 'c')
    bench.run_automatic_discovery_once()
    whole = summarize(bench, matter)
    scoped = summarize(bench, matter, scope=first['receipt_id'])
    assert whole.totals['documents'] == 3
    assert scoped.totals['documents'] == 1
    assert whole.totals['pages'] == 0  # Text line counts must not masquerade as pages.
    assert whole.date_range['earliest'] == '2034-04-17'
    assert whole.date_range['latest'] == '2034-04-19'
    assert whole.date_range['undated_count'] == 0
    assert scoped.date_range['latest'] == '2034-04-17'
    assert {row['reference_text'] for row in whole.gaps['referenced_absent']} == {'Attachment B'}
    assert {row['reference_text'] for row in scoped.gaps['referenced_absent']} == {'Exhibit 12'}
    assert whole.gaps['bates_missing'][0]['start'] == '0004'
    assert whole.gaps['bates_missing'][0]['end'] == '0005'
    assert whole.people and whole.organizations
    assert scoped.people[0]['mentions'] == 1
    for row in whole.people + whole.organizations:
        assert '#support-pane' in row['link']
        assert client.get(row['link']).status_code == 200
    assert summarize(bench, matter) == whole
    for suffix in ('', '/export?format=json', '/export?format=markdown'):
        response = client.get(f'/matters/{matter.slug}/production-summary' + suffix)
        assert response.status_code == 200, response.text[:500]
        assert response.headers['cache-control'] == 'no-store'
        assert NOTICE in response.text
    for row in whole.by_type:
        assert client.get(row['link']).status_code == 200
    assert client.get(f'/matters/{matter.slug}/production-summary/export?format=pdf').status_code == 409


def test_failed_extraction_matches_briefing_and_receipt_scope(workbench):
    client, bench, matter, _ = workbench
    receipt = upload_path(client, bench, matter, 'failed.txt', b'Synthetic readable source.', 'a')
    [document] = bench.source_store(matter).documents.values()
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute("UPDATE workbench_ingest_job SET state='failed',message='Synthetic index failure' WHERE matter_id=? AND document_id=?", (matter.matter_id, document.document_id))
    result = summarize(bench, matter)
    assert sum(item['count'] for item in result.gaps['unreadable']) == assemble(bench, matter).section('unread').total == 1
    assert 'Synthetic index failure' in result.gaps['unreadable'][0]['reason']
    assert len(summarize(bench, matter, scope=receipt['receipt_id']).gaps['unreadable']) == 1


def test_summary_authorization_receipt_and_job_scope(workbench):
    client, bench, matter, _ = workbench
    from tests.test_matter_notebook import _create_matter
    other_slug = _create_matter(client, 'Other synthetic matter')
    other = bench.matter(other_slug, WEB_ACTOR)
    receipt = upload_path(client, bench, other, 'secret.txt', b'Synthetic separate scope.', 'a')
    for suffix in ('', '/export?format=json'):
        separator = '&' if '?' in suffix else '?'
        assert client.get(f'/matters/{matter.slug}/production-summary{suffix}{separator}scope={receipt["receipt_id"]}').status_code == 404
    response = client.post(f'/matters/{matter.slug}/production-summary', follow_redirects=False)
    assert response.status_code == 303
    url = response.headers['location']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(url)
        assert status.status_code == 200
        if 'data-summary-job="completed"' in status.text:
            break
        time.sleep(.02)
    else:
        pytest.fail('Summary job did not complete')
    assert client.get(url.replace(matter.slug, other.slug)).status_code == 404
    with pytest.raises(KeyError):
        ProductionSummaryService(bench, matter, 'unrelated-synthetic-actor').summarize(matter.matter_id)


def stable_snapshot(result):
    return dict(format=result.format, totals={key: result.totals[key] for key in ('documents','pages','bytes')},
        by_type=[{key: row[key] for key in ('type','documents','pages')} for row in result.by_type],
        date_range={key: result.date_range[key] for key in ('earliest','latest','undated_count')},
        people=[dict(name=row['name'], mentions=row['mentions']) for row in result.people],
        organizations=[dict(name=row['name'], mentions=row['mentions']) for row in result.organizations],
        unreadable=[dict(name=row['name'], reason=row['reason']) for row in result.gaps['unreadable']])


def test_harbor_street_golden(demo_script, tmp_path):
    with demo_script.seed_demo(tmp_path / 'production-demo') as demo:
        result = ProductionSummaryService(demo.app.state.workbench, demo.matter, demo.actor_id).summarize(demo.matter.matter_id)
        expected = json.loads((Path(__file__).parent / 'snapshots/production_summary_harbor_street.json').read_text())
        assert stable_snapshot(result) == expected
        assert result.totals['documents'] == 40
        assert len(result.gaps['unreadable']) == 3  # Two PDF coverage notices and one extraction failure.
        assert any('40-' in row['name'] for row in result.gaps['unreadable'])
        assert sum(row['count'] for row in result.gaps['unreadable']) == assemble(demo.app.state.workbench, demo.matter).section('unread').total
        for row in result.people:
            assert demo.client.get(row['link']).status_code == 200
        output = export_summary(result, 'markdown').body.decode()
        for phrase in ('withheld', 'missing from the government', 'incomplete production'):
            assert phrase not in output.casefold()


def test_limits_refuse_partial_summary(workbench, monkeypatch):
    from case_intelligence import production_summary as module
    client, bench, matter, _ = workbench
    upload(client, matter.slug, 'source.txt', b'Synthetic date 2034-04-17.')
    ready(bench, matter)
    monkeypatch.setattr(module, 'MAX_CHARACTERS', 1)
    from case_intelligence.workspace_store import WorkspaceProblem
    with pytest.raises(WorkspaceProblem, match='no partial summary'):
        summarize(bench, matter)


def test_eval_hook_loads():
    payload = json.loads((Path(__file__).parent / 'fixtures/synthetic/workflow_eval/production_summary.json').read_text())
    assert payload['questions']
    assert all(row['question'] and row['expected'] is not None for row in payload['questions'])


def test_seeded_pdf_gaps_and_missing_exhibit(workbench):
    client, bench, matter, _ = workbench
    root = Path(__file__).parent / 'fixtures/synthetic/production_gaps'
    for path in sorted(root.glob('*.pdf')):
        response = client.post(f'/matters/{matter.slug}/uploads', files=[('files', (path.name, path.read_bytes(), 'application/pdf'))])
        assert response.status_code in (200, 303), response.text
    ready(bench, matter)
    result = summarize(bench, matter)
    assert result.totals['documents'] == 3
    assert result.totals['pages'] == 3
    assert [(row['prefix'],row['start'],row['end']) for row in result.gaps['bates_missing']] == [('SYN','0004','0005')]
    assert [(row['start'],row['occurrences']) for row in result.gaps['bates_duplicates']] == [('0003',2)]
    assert result.gaps['referenced_absent'][0]['reference_text'] == 'Exhibit 12'
    assert client.get(result.gaps['referenced_absent'][0]['citation']['link']).status_code == 200


def test_document_dates_reuse_briefing_validation(workbench):
    from case_intelligence.briefing import RecordedDocumentDate
    client, bench, matter, _ = workbench
    for index in range(3):
        upload(client, matter.slug, f'record-{index}.txt', b'Synthetic undated record.')
    ready(bench, matter)
    documents = sorted(bench.source_store(matter).documents.values(), key=lambda row: row.display_name)
    inventory = tuple(RecordedDocumentDate(row.document_id, row.version_id, f'2034-04-{index + 16}') for index, row in enumerate(documents))
    result = summarize(bench, matter, inventory=inventory)
    assert result.date_range['undated_count'] == 0
    assert result.date_range['earliest'] == '2034-04-16'
    assert result.date_range['latest'] == '2034-04-18'
    bad = RecordedDocumentDate(documents[1].document_id, documents[1].version_id, '2034-04-19')
    result = summarize(bench, matter, inventory=(*inventory, bad))
    assert result.date_range['undated_count'] == 1


def test_invalid_dates_xss_and_markdown_stay_inert(workbench):
    client, bench, matter, _ = workbench
    upload(client, matter.slug, 'synthetic.txt', b'2034-02-31 and 03/04/2034. See Exhibit 12.\nAttachment: [sample](javascript:alert).pdf')
    ready(bench, matter)
    result = summarize(bench, matter)
    assert result.date_range['earliest'] is None
    assert result.date_range['undated_count'] == 1
    output = export_summary(result, 'markdown').body.decode()
    assert '[sample](javascript:' not in output
    page = client.get(f'/matters/{matter.slug}/production-summary')
    assert page.status_code == 200
    assert 'href="javascript:' not in page.text


def test_unrecorded_and_skipped_receipts_keep_scope(workbench):
    from tests.test_intake_receipt_http import descriptor, selection
    from case_intelligence.intake_receipts import IntakeReceipts
    client, bench, matter, _ = workbench
    first = selection(client, matter.slug, [descriptor('opaque.bin', 25)], [], key='c' * 32)
    second = selection(client, matter.slug, [descriptor('other.bin', 25)], [], key='d' * 32)
    result = summarize(bench, matter, scope=first['receipt_id'])
    assert len(result.gaps['unreadable']) == 1
    assert 'opaque.bin' in result.gaps['unreadable'][0]['reason']
    assert 'other.bin' not in json.dumps(result.to_dict())
    receipts = IntakeReceipts(bench.workspace)
    pending = receipts.create(matter.matter_id, WEB_ACTOR, selection_key='e'*32, selection_fingerprint='e'*64,
        selected_count=2, eligible_indexes=[], collection_name='Synthetic interrupted selection')
    result = summarize(bench, matter, scope=pending['receipt_id'])
    assert result.gaps['unreadable'][0]['count'] == 2
    assert result.totals['documents'] == 0


def test_read_error_is_explicit_and_caps_reported(workbench, monkeypatch):
    from case_intelligence import production_summary as module
    from case_intelligence.pilot_uploads import PilotDocument
    client, bench, matter, _ = workbench
    upload(client, matter.slug, 'source.txt', b'Synthetic source with See Exhibit 1. See Exhibit 2.')
    ready(bench, matter)
    monkeypatch.setattr(module, 'MAX_DISPLAY', 1)
    result = summarize(bench, matter)
    assert len(result.gaps['referenced_absent']) == 1
    assert result.capped['referenced_absent']['omitted'] == 1
    def failed(*args, **kwargs):
        raise OSError('synthetic unreadable derived stream')
    monkeypatch.setattr(PilotDocument, 'iter_parsed_units', failed)
    result = summarize(bench, matter)
    assert result.gaps['unreadable'][0]['reason'] == 'Extracted text could not be read.'


def test_post_requires_real_csrf_and_exports_reauthorize(tmp_path):
    import re
    from fastapi.testclient import TestClient
    from case_intelligence.workbench import create_workbench_app
    from case_intelligence.identity import LOGIN_CHALLENGE_COOKIE
    with TestClient(create_workbench_app(tmp_path / 'runtime', auth_mode='preview')) as client:
        client.get('/auth/login')
        assert client.post('/auth/login', data={'identity_subject': 'taylor-morgan',
            'login_challenge': client.cookies.get(LOGIN_CHALLENGE_COOKIE), 'next': '/'}, follow_redirects=False).status_code == 303
        csrf = re.search(r'data-csrf-token="([0-9a-f]{64})"', client.get('/').text).group(1)
        created = client.post('/matters', data={'name':'Synthetic protected summary','descriptor':'','csrf_token':csrf}, follow_redirects=False)
        slug = created.headers['location'].split('/')[2]
        root = f'/matters/{slug}/production-summary'
        assert client.post(root).status_code == 403
        started = client.post(root, data={'csrf_token':csrf}, follow_redirects=False)
        assert started.status_code == 303
        assert client.get(root + '/export?format=json').status_code == 200
        bench = client.app.state.workbench
        with bench.workspace._lock, bench.workspace.connection:
            bench.workspace.connection.execute('UPDATE workbench_principal SET active=0 WHERE principal_id=?', (WEB_ACTOR,))
        assert client.get(root + '/export?format=json', follow_redirects=False).status_code in {303,401,403,404}
        assert client.get(started.headers['location'], follow_redirects=False).status_code in {303,401,403,404}


def test_email_attachment_reference_uses_extraction_metadata(workbench, monkeypatch):
    from email.message import EmailMessage
    client, bench, matter, _ = workbench
    from tests.test_review_tools import CleanScanner
    monkeypatch.setattr(bench.source_store(matter), 'malware_scanner', CleanScanner())
    email = EmailMessage()
    email['From'] = 'alex@example.test'
    email['To'] = 'jordan@example.test'
    email['Subject'] = 'Synthetic attachment reference'
    email.set_content('Please review the attached synthetic exhibit.')
    email.add_attachment(b'Synthetic attachment text.', maintype='application', subtype='octet-stream', filename='exhibit-12.txt')
    response = client.post(f'/matters/{matter.slug}/uploads', files=[('files', ('message.eml', email.as_bytes(), 'message/rfc822'))])
    assert response.status_code in (200,303), response.text
    ready(bench, matter)
    result = summarize(bench, matter)
    assert [row['reference_text'] for row in result.gaps['referenced_absent']] == ['exhibit-12.txt']
    assert client.get(result.gaps['referenced_absent'][0]['citation']['link']).status_code == 200


def test_repeated_labels_do_not_invent_duplicates_and_reference_case_is_preserved(workbench):
    client, bench, matter, _ = workbench
    upload(client, matter.slug, 'first.txt', b'Bates: ABC0001. Bates: ABC0001.\nBates: abc-0001.\nSee Record 12. Refer to Record 13. See exhibit 14. ATTACHED AS attachment 15.')
    ready(bench, matter)
    result = summarize(bench, matter)
    assert result.gaps['bates_duplicates'] == []
    references = result.gaps['referenced_absent']
    assert {row['reference_text'] for row in references} == {'Record 12', 'Record 13', 'exhibit 14', 'attachment 15'}
    assert all(client.get(row['citation']['link']).status_code == 200 for row in references)
    upload(client, matter.slug, 'second.txt', b'Bates: ABC0001.')
    ready(bench, matter)
    [duplicate] = summarize(bench, matter).gaps['bates_duplicates']
    assert duplicate['occurrences'] == 2
    assert len(duplicate['links']) == 2


def metadata_pdf(labels, text=b'Synthetic source with labels recorded only in PDF metadata.'):
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject, DecodedStreamObject
    writer = PdfWriter()
    entries = ArrayObject()
    for index, label in enumerate(labels):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 12 Tf 50 740 Td (' + text + b') Tj ET')
        page[NameObject('/Contents')] = writer._add_object(content)
        entries.extend([NumberObject(index), DictionaryObject({NameObject('/P'): TextStringObject(label)})])
    writer._root_object[NameObject('/PageLabels')] = DictionaryObject({NameObject('/Nums'): entries})
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_uploaded_pdf_metadata_reaches_summary_and_original_page_links(workbench):
    client, bench, matter, _ = workbench
    for name, labels in [('first.pdf', ['SYN0001', 'SYN0004']), ('second.pdf', ['SYN0004'])]:
        response = client.post(f'/matters/{matter.slug}/uploads', files=[('files', (name, metadata_pdf(labels), 'application/pdf'))])
        assert response.status_code in (200, 303)
    ready(bench, matter)
    assert all('Bates:' not in unit.text for doc in bench.source_store(matter).documents.values() for unit in doc.parsed_units())
    response = client.get(f'/matters/{matter.slug}/production-summary/export?format=json')
    assert response.status_code == 200
    result = response.json()
    [gap] = result['gaps']['bates_missing']
    assert (gap['start'], gap['end']) == ('0002', '0003')
    [duplicate] = result['gaps']['bates_duplicates']
    assert duplicate['occurrences'] == 2
    assert {link.rsplit('#page=', 1)[1] for link in duplicate['links']} == {'1', '2'}
    for link in gap['links'] + duplicate['links']:
        source = client.get(link)
        assert source.status_code == 200
        assert source.content.startswith(b'%PDF')
    markdown = client.get(f'/matters/{matter.slug}/production-summary/export?format=markdown').text
    assert '/content#page=2' in markdown


def test_pdf_metadata_cannot_be_read_from_changed_original(workbench):
    client, bench, matter, _ = workbench
    response = client.post(f'/matters/{matter.slug}/uploads', files=[('files', ('source.pdf', metadata_pdf(['SYN0001']), 'application/pdf'))])
    assert response.status_code in (200, 303)
    ready(bench, matter)
    store = bench.source_store(matter)
    [document] = store.documents.values()
    path = store.source_path(document.document_id)
    path.write_bytes(path.read_bytes().replace(b'SYN0001', b'SYN9999'))
    result = summarize(bench, matter)
    assert not result.gaps['bates_missing']
    assert any('Bates coverage is incomplete' in row['reason'] for row in result.gaps['unreadable'])


def test_pdf_metadata_budget_refuses_partial_summary(workbench, monkeypatch):
    from case_intelligence import production_summary as module
    from case_intelligence.workspace_store import WorkspaceProblem
    client, bench, matter, _ = workbench
    response = client.post(f'/matters/{matter.slug}/uploads', files=[('files', ('source.pdf', metadata_pdf(['SYN0001', 'SYN0004']), 'application/pdf'))])
    assert response.status_code in (200, 303)
    ready(bench, matter)
    monkeypatch.setattr(module, 'MAX_METADATA_BYTES', 1)
    with pytest.raises(WorkspaceProblem, match='no partial summary'):
        summarize(bench, matter)


def test_bates_cap_disclosures_survive_service_and_exports(workbench, monkeypatch):
    from case_intelligence import production_summary as module
    client, bench, matter, _ = workbench
    upload(client, matter.slug, 'ranges.txt', b'Bates: A1 to A100. Bates: A2 to A99. Bates: A3 to A98. Bates: A4 to A97.')
    ready(bench, matter)
    monkeypatch.setattr(module, 'MAX_DISPLAY', 1)
    result = summarize(bench, matter)
    assert len(result.gaps['bates_duplicates']) == 1
    assert result.capped['bates_duplicates'] == dict(total=5, shown=1, omitted=4)
    assert '4 omitted of 5' in export_summary(result, 'markdown').body.decode()


def test_pdf_text_and_metadata_share_physical_page_identity(workbench):
    client, bench, matter, _ = workbench
    response = client.post(f'/matters/{matter.slug}/uploads', files=[('files', ('source.pdf', metadata_pdf(['SYN0001', 'SYN0001'], b'Synthetic record. Bates: SYN0001. Bates: SYN0001.'), 'application/pdf'))])
    assert response.status_code in (200, 303)
    ready(bench, matter)
    [duplicate] = summarize(bench, matter).gaps['bates_duplicates']
    assert duplicate['occurrences'] == 2  # Two physical pages, not six label observations.
    assert len(duplicate['links']) == 2
