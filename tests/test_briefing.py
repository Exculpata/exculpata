"""Synthetic source-backed briefing, including provenance and complete pagination."""
from dataclasses import FrozenInstanceError, replace
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from case_intelligence.ask_router import classify
from case_intelligence.briefing import (
    Briefing, BriefingLine, BriefingLink, BriefingSection, RecordedDocumentDate,
    SuggestedQuestion, _questions, build_briefing,
)
from case_intelligence.intake_receipts import IntakeReceipts
from case_intelligence.workspace_store import WorkspaceProblem
from case_intelligence.workbench import create_workbench_app
from tests.test_automatic_discovery import upload
from tests.test_intake_receipt_http import descriptor, selection, upload as start_upload, put
from tests.test_matter_notebook import WEB_ACTOR, _create_matter


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    monkeypatch.delenv('CASE_INTELLIGENCE_AUTOMATIC_DISCOVERY', raising=False)
    runtime = tmp_path / 'runtime'
    with TestClient(create_workbench_app(runtime, auth_mode='test', background_ingestion=True)) as client:
        slug = _create_matter(client)
        bench = client.app.state.workbench
        yield client, bench, bench.matter(slug, WEB_ACTOR), runtime


def assemble(bench, matter, **kwargs):
    return build_briefing(matter, WEB_ACTOR, workspace=bench.workspace,
        entity_service=bench.entity_service(matter), intake_receipts=IntakeReceipts(bench.workspace),
        source_metadata=bench.source_store(matter).get, **kwargs)


def ready(bench, matter):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if bench.workspace.matter_readiness(matter.matter_id).processing_count == 0:
            return
        time.sleep(0.02)
    pytest.fail('Synthetic source processing did not finish')


def upload_path(client, bench, matter, path, body, key):
    files = [descriptor(path, len(body))]
    receipt = selection(client, matter.slug, files, [0], key=key * 32)
    session = start_upload(client, matter.slug, receipt, files, [0])
    put(client, session['items'][0], body)
    assert client.post(session['items'][0]['finalize_url']).status_code == 200
    ready(bench, matter)
    return receipt


def lines(briefing, section, kind=None):
    return [line for line in briefing.section(section).lines if kind is None or line.kind == kind]


def assert_links_open(client, matter, briefing):
    for line in [line for section in briefing.sections for line in section.lines] + list(briefing.coverage) + list(briefing.questions):
        assert line.links, line
        for link in line.links:
            url = urlsplit(link.href)
            assert not url.scheme and not url.netloc
            assert url.path == f'/matters/{matter.slug}' or url.path.startswith(f'/matters/{matter.slug}/')
            response = client.get(link.href)
            assert response.status_code == 200, (line, link, response.text[:200])
            assert 'text/html' in response.headers['content-type']
            if '/sources/' in url.path:
                assert link.label in response.text
            if 'support' in parse_qs(url.query):
                assert 'support-pane' in response.text


def test_all_sections_counts_recorded_dates_suggestions_and_source_links(workbench):
    client, bench, matter, _runtime = workbench
    upload_path(client, bench, matter, 'North/first.txt',
        b'Alex Example met Jordan Sample on 2024-05-06. Amber Cooperative opened.\nAlex Example returned on 2024-05-06.', 'a')
    upload_path(client, bench, matter, 'North/nested/second.txt',
        b'Alex Example met Riley Placeholder on 2024-05-07.', 'b')
    upload(client, matter.slug, 'Root record.txt', b'Jordan Sample returned on 2024-05-06.')
    ready(bench, matter)
    assert bench.run_automatic_discovery_once() == 3
    service = bench.entity_service(matter)
    entities, _ = service.list(matter.matter_id, WEB_ACTOR)
    place = next(item for item in entities if item['display_name'] == 'Amber Cooperative')
    service.update(matter.matter_id, WEB_ACTOR, place['entity_id'], expected_revision=place['revision'],
        display_name='Amber Cooperative', entity_type='place', status='suggested')
    documents = sorted(bench.source_store(matter).documents.values(), key=lambda item: item.display_name)
    inventory = [RecordedDocumentDate(documents[0].document_id, documents[0].version_id, '2023-01-02'),
                 RecordedDocumentDate(documents[1].document_id, documents[1].version_id, '2025-06-07')]
    briefing = assemble(bench, matter, inventory=inventory)
    assert [section.key for section in briefing.sections] == ['arrived', 'people_places', 'dates', 'unread']
    assert [(line.value, line.count) for line in lines(briefing, 'arrived', 'file_type')] == [('TXT', 3)]
    assert [(line.value, line.count) for line in lines(briefing, 'arrived', 'folder')] == [('', 1), ('North', 2)]
    assert lines(briefing, 'arrived', 'earliest_document_date')[0].value == '2023-01-02'
    assert lines(briefing, 'arrived', 'latest_document_date')[0].value == '2025-06-07'
    assert [(line.value, line.count) for line in lines(briefing, 'people_places', 'person')] == [
        ('Alex Example', 3), ('Jordan Sample', 2), ('Riley Placeholder', 1)]
    assert [(line.value, line.count) for line in lines(briefing, 'people_places', 'place')] == [('Amber Cooperative', 1)]
    assert [(line.value, line.count) for line in lines(briefing, 'dates', 'mentioned_date')] == [('2024-05-06', 3), ('2024-05-07', 1)]
    assert all(line.suggested and line.text.startswith('Suggested ') for section in ('people_places', 'dates') for line in lines(briefing, section))
    assert briefing.section('unread').total == 0
    assert [question.text for question in briefing.questions] == [
        'What do the records say about Alex Example?',
        'What do the records say about Amber Cooperative?',
        'What do the records say about the mentioned date 2024-05-06?',
        'What do the records say about Jordan Sample?',
        'What do the records say about the mentioned date 2024-05-07?',
    ]
    assert all(question.label == 'Suggested' for question in briefing.questions)
    input_links = {line.links for section in briefing.sections for line in section.lines}
    assert all(question.links in input_links for question in briefing.questions)
    assert assemble(bench, matter, inventory=inventory) == briefing
    assert_links_open(client, matter, briefing)


def test_empty_matter_has_every_section_and_no_invented_dates(workbench):
    client, bench, matter, _runtime = workbench
    briefing = assemble(bench, matter)
    assert len(briefing.sections) == 4
    assert all(section.total == 0 for section in briefing.sections)
    assert lines(briefing, 'arrived', 'document_dates_unavailable')
    assert all(lines(briefing, section, 'empty') for section in ('people_places', 'dates', 'unread'))
    assert briefing.questions == ()
    assert_links_open(client, matter, briefing)


def test_document_dates_reject_wrong_provenance_invalid_stale_and_conflicting_metadata(workbench):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, 'Synthetic dated memo.txt', b'A memo states 2020-01-01.')
    ready(bench, matter)
    bench.run_automatic_discovery_once()
    [document] = bench.source_store(matter).documents.values()
    base = RecordedDocumentDate(document.document_id, document.version_id, '2024-01-01')
    for inventory in ((), (replace(base, basis='filesystem_mtime'),),
                      (replace(base, value='2024-02-30'),), (replace(base, version_id='f' * 32),),
                      (replace(base, document_id='0' * 32),), (base, replace(base, value='2024-01-02'))):
        briefing = assemble(bench, matter, inventory=inventory)
        assert lines(briefing, 'arrived', 'document_dates_unavailable')
        assert not lines(briefing, 'arrived', 'earliest_document_date')
    briefing = assemble(bench, matter, inventory=(base, base))
    assert lines(briefing, 'arrived', 'earliest_document_date')[0].value == '2024-01-01'


def test_failed_extraction_and_skipped_intake_keep_recorded_reasons(workbench):
    client, bench, matter, _runtime = workbench
    upload_path(client, bench, matter, 'Generated/failed.txt', b'Alex Example in a readable source.', 'a')
    [document] = bench.source_store(matter).documents.values()
    # Extraction may mark a source ready before the durable indexing job fails.
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute("UPDATE workbench_ingest_job SET state='failed',message='Synthetic index could not be built' WHERE matter_id=? AND document_id=?", (matter.matter_id, document.document_id))
    assert bench.workspace.source_catalog_record(matter.matter_id, document.document_id).tone == 'ready'
    receipt = selection(client, matter.slug, [descriptor('Unsupported/opaque.bin', 25)], [], key='b' * 32)
    briefing = assemble(bench, matter)
    assert briefing.section('unread').total == 2
    assert briefing.questions == ()
    texts = [line.text for line in lines(briefing, 'unread')]
    assert sum('Synthetic index could not be built' in text for text in texts) == 1
    assert any('Browser-reported selection review:' in text and 'Unsupported/opaque.bin' in text for text in texts)
    intake_line = lines(briefing, 'unread', 'intake_incomplete')[0]
    assert intake_line.links[0].href == f"/matters/{matter.slug}/intake/{receipt['receipt_id']}?page=1"
    assert_links_open(client, matter, briefing)


def test_ranking_reads_all_date_pages_and_preserves_ambiguous_dates_and_ties(workbench):
    client, bench, matter, _runtime = workbench
    body = '\n'.join([f'Person: Generated Person{index:02d}; stated 2024-01-{index + 1:02d}.' for index in range(30)]
        + ['Person: Generated Person29; stated 2024-01-30.'] * 4
        + ['Ambiguous date 03/04/2026.'] * 2)
    upload(client, matter.slug, 'Generated long chronology.txt', body.encode())
    ready(bench, matter)
    bench.run_automatic_discovery_once()
    briefing = assemble(bench, matter, max_ranked=2)
    assert [(line.value, line.count) for line in lines(briefing, 'people_places', 'person')] == [
        ('Generated Person29', 5), ('Generated Person00', 1)]
    assert briefing.section('people_places').total == 30
    assert briefing.section('people_places').omitted == 28
    assert [(line.value, line.count) for line in lines(briefing, 'dates', 'mentioned_date')] == [
        ('2024-01-30', 5), ('03/04/2026', 2)]
    assert 'calendar order unresolved' in lines(briefing, 'dates', 'mentioned_date')[1].text
    assert briefing.section('dates').total == 31
    assert briefing.section('dates').omitted == 29
    assert 'all 36 retained' in next(line.text for line in briefing.coverage if line.kind == 'date_coverage')
    assert assemble(bench, matter, max_ranked=2) == briefing
    assert_links_open(client, matter, briefing)


def test_only_automatic_suggested_people_are_ranked_and_current_sample_wins(workbench):
    client, bench, matter, _runtime = workbench
    for name in ('A historical.txt', 'B current.txt'):
        upload(client, matter.slug, name, b'Alex Example arrived.')
    ready(bench, matter)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    manual = service.create(matter.matter_id, WEB_ACTOR, display_name='Manual Person', status='suggested')
    # An answer-triggered extraction identity is not automatic background discovery.
    with service.repository.transaction(matter.matter_id, WEB_ACTOR) as repo:
        repo.mark_extracted(matter.matter_id, manual['entity_id'], 'synthetic-extractor')
    store = bench.source_store(matter)
    documents = sorted(store.documents.values(), key=lambda doc: doc.display_name)
    with store.mutation_guard():
        documents[0].version_id = 'f' * 32
        bench._sync_source_catalog(matter, tuple(store.documents.values()))
    briefing = assemble(bench, matter)
    [suggestion] = lines(briefing, 'people_places', 'person')
    assert suggestion.value == 'Alex Example' and suggestion.count == 2
    token = parse_qs(urlsplit(suggestion.links[0].href).query)['support'][0]
    reference = bench.notebook_reference_from_support(matter, token)
    assert reference['document_id'] == documents[1].document_id
    assert 'including retained historical support' in next(line.text for line in briefing.coverage if line.kind == 'suggestion_coverage')
    assert [question.text for question in briefing.questions] == ['What do the records say about Alex Example?']
    assert_links_open(client, matter, briefing)
    with store.mutation_guard():
        documents[1].version_id = 'e' * 32
        bench._sync_source_catalog(matter, tuple(store.documents.values()))
    stale = assemble(bench, matter)
    [suggestion] = lines(stale, 'people_places', 'person')
    assert all('support=' not in link.href for link in suggestion.links)
    assert '/entities/' in suggestion.links[0].href
    assert stale.questions == ()


def test_catalog_streams_beyond_one_page_and_counts_root_and_nested_folders(workbench, monkeypatch):
    import io
    client, bench, matter, _runtime = workbench
    store = bench.source_store(matter)
    for index in range(105):
        filename = f'Synthetic record {index:03d}.txt'
        path = filename if index < 5 else f'Folder{index % 2}/nested/{filename}'
        store.store_stream(filename, 'text/plain', io.BytesIO(f'Synthetic row {index}.'.encode()), relative_path=path)
    def no_library_pages(*args, **kwargs):
        pytest.fail('Briefing must not recompute whole-library facets for each source page')
    monkeypatch.setattr(bench.workspace, 'source_catalog_page', no_library_pages)
    monkeypatch.setattr(bench.workspace, 'ingest_job', no_library_pages)
    monkeypatch.setattr(bench.workspace, 'media_job', no_library_pages)
    queries = []
    bench.workspace.connection.set_trace_callback(queries.append)
    briefing = assemble(bench, matter)
    bench.workspace.connection.set_trace_callback(None)
    job_reads = [query for query in queries if query.startswith('SELECT j.* FROM workbench_')]
    assert len(job_reads) == 2
    assert briefing.section('arrived').total == 105
    assert [(line.value, line.count) for line in lines(briefing, 'arrived', 'folder')] == [('', 5), ('Folder0', 50), ('Folder1', 50)]
    assert lines(briefing, 'arrived', 'file_type')[0].count == 105
    assert all(len(line.links) <= 4 for line in lines(briefing, 'arrived'))


def test_receipt_pages_and_display_omissions_are_complete_and_source_linked(workbench):
    client, bench, matter, _runtime = workbench
    files = [descriptor(f'Unsupported/item-{index:03d}.bin', 25) for index in range(101)]
    receipt = selection(client, matter.slug, files, [], key='a' * 32)
    briefing = assemble(bench, matter, max_incomplete=100)
    assert briefing.section('unread').total == 101
    assert briefing.section('unread').omitted == 1
    assert len(lines(briefing, 'unread')) == 100
    # Use a late row with a name that sorts first to show the second receipt page.
    with bench.workspace._lock, bench.workspace.connection:
        bench.workspace.connection.execute("UPDATE workbench_intake_item SET relative_path='AAA/late.bin' WHERE receipt_id=? AND ordinal=100", (receipt['receipt_id'],))
    first = lines(assemble(bench, matter, max_incomplete=1), 'unread')[0]
    assert first.links[0].href.endswith('?page=2')
    page = client.get(first.links[0].href)
    assert page.status_code == 200 and 'AAA/late.bin' in page.text
    assert first.text.count('Browser-reported selection review:') == 1


def test_receipts_beyond_recent_default_and_cancelled_legacy_uploads_are_counted(workbench):
    client, bench, matter, _runtime = workbench
    receipts = IntakeReceipts(bench.workspace)
    for index in range(101):
        receipt = receipts.create(matter.matter_id, WEB_ACTOR, selection_key=f'{index:032x}',
            selection_fingerprint='b' * 64, selected_count=1, eligible_indexes=[], collection_name=f'Generated selection {index:03d}')
        receipts.append(matter.matter_id, WEB_ACTOR, receipt['receipt_id'], start=0,
            files=[descriptor(f'Unsupported/item-{index:03d}.bin', 25)], reviewed_states=['unsupported'],
            document_limit=1024, media_limit=2048, malware_scan_mode='off', scanner_ready=True)
        receipts.seal(matter.matter_id, WEB_ACTOR, receipt['receipt_id'])
    session, items = bench.workspace.create_upload_session(matter.matter_id, WEB_ACTOR, 'Generated cancelled upload',
        [dict(display_name='Cancelled.txt', relative_path='Cancelled.txt', media_type='text/plain', expected_size=50)])
    bench.workspace.cancel_upload_session(matter.matter_id, WEB_ACTOR, session.upload_session_id)
    briefing = assemble(bench, matter)
    assert briefing.section('unread').total == 102
    assert briefing.section('unread').omitted == 52
    [cancelled] = lines(briefing, 'unread', 'upload_incomplete')
    assert 'Cancelled.txt' in cancelled.text and 'Upload cancelled' in cancelled.text
    review = client.get(cancelled.links[0].href)
    assert review.status_code == 200 and 'text/html' in review.headers['content-type']
    assert 'Cancelled.txt' in review.text and 'Upload cancelled' in review.text
    assert session.upload_session_id in cancelled.links[0].href
    assert items[0].upload_item_id in cancelled.links[0].href


@pytest.mark.parametrize('notice', [
    '1 of 2 pages searchable; scanned pages remain unreadable. Complete page reading is not established.',
    'This source has no searchable text.',
])
def test_searchable_sources_keep_recorded_incomplete_coverage(workbench, notice):
    client, bench, matter, _runtime = workbench
    if 'pages' in notice:
        from tests.test_pdf_coverage_surfaces import generated_native_pdf
        response = client.post(f'/matters/{matter.slug}/uploads',
            files={'files': ('Generated partial source.pdf', generated_native_pdf(), 'application/pdf')},
            follow_redirects=False)
        assert response.status_code in (200, 303), response.text
    else:
        upload(client, matter.slug, 'Generated partial source.txt', b'Synthetic searchable passage.')
    ready(bench, matter)
    store = bench.source_store(matter)
    [document] = store.documents.values()
    with store.mutation_guard():
        document.message = notice
        bench._sync_source_catalog(matter, tuple(store.documents.values()))
    catalog = bench.workspace.source_catalog_record(matter.matter_id, document.document_id)
    assert catalog.tone == 'ready' and catalog.state_label == notice
    readiness = bench.workspace.matter_readiness(matter.matter_id)
    assert readiness.searchable_count == 1 and readiness.attention_count == 0
    briefing = assemble(bench, matter)
    assert briefing.section('unread').total == 1
    [line] = lines(briefing, 'unread', 'source_incomplete')
    assert notice in line.text
    assert_links_open(client, matter, briefing)


def test_legacy_upload_review_checks_membership_matter_session_and_item(tmp_path, monkeypatch):
    from tests.test_matter_management import ADMIN, OWNER, OTHER, _app, _csrf, _headers, _principal_id
    from tests.test_matter_management import _create_matter as create_authorized
    monkeypatch.setenv('CASE_INTELLIGENCE_STORAGE_RESERVE_GIB', '0')
    with TestClient(_app(tmp_path), base_url='https://recordbench.example.test') as client:
        csrf = _csrf(client.get('/matters/new', headers=_headers(OWNER)).text)
        slug = create_authorized(client, principal=OWNER, csrf_token=csrf, name='Synthetic upload review')
        bench = client.app.state.workbench
        owner_id = _principal_id(client, OWNER)
        client.get('/matters/new', headers=_headers(OTHER))
        other_id = _principal_id(client, OTHER)
        matter = bench.matter(slug, owner_id)
        session, items = bench.workspace.create_upload_session(matter.matter_id, owner_id, 'Generated failed collection',
            [dict(display_name='Failed.txt', relative_path='Failed.txt', media_type='text/plain', expected_size=20)])
        bench.workspace.cancel_upload_session(matter.matter_id, owner_id, session.upload_session_id)
        href = f'/matters/{slug}/upload-review/{session.upload_session_id}/{items[0].upload_item_id}'
        # Membership permits reading a teammate's upload without granting its
        # actor-owned transfer/cancellation API to that teammate.
        bench.workspace.add_member(matter.matter_id, other_id, owner_id)
        response = client.get(href, headers=_headers(OTHER))
        assert response.status_code == 200 and 'Failed.txt' in response.text
        assert 'Generated failed collection' in response.text and 'Upload cancelled' in response.text
        assert client.get(f'/matters/{slug}/upload-sessions/{session.upload_session_id}', headers=_headers(OTHER)).status_code == 404
        assert client.get(href, headers=_headers(ADMIN)).status_code == 404
        bench.workspace.revoke_member(matter.matter_id, other_id, owner_id)
        assert client.get(href, headers=_headers(OTHER)).status_code == 404
        assert client.get(href.replace(items[0].upload_item_id, 'upload-item-' + '0' * 32), headers=_headers(OWNER)).status_code == 404


def test_read_is_authorized_and_writes_nothing_or_reads_no_extracted_text(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, 'Generated memo.txt', b'Alex Example arrived on 2024-01-02.')
    ready(bench, matter)
    bench.run_automatic_discovery_once()
    service = bench.entity_service(matter)
    def forbidden(*args, **kwargs):
        pytest.fail('Briefing must not call models or load extracted text')
    monkeypatch.setattr(service, 'validate_references', forbidden)
    monkeypatch.setattr(bench.entity_unit_reader, 'iter_selected', forbidden)
    before = bench.workspace.connection.total_changes
    briefing = build_briefing(matter, WEB_ACTOR, workspace=bench.workspace, entity_service=service,
        intake_receipts=IntakeReceipts(bench.workspace))
    assert briefing.section('arrived').total == 1
    assert len(briefing.questions) == 2
    assert bench.workspace.connection.total_changes == before
    with pytest.raises(KeyError):
        build_briefing(matter, 'generated-foreign-actor', workspace=bench.workspace,
            entity_service=service, intake_receipts=IntakeReceipts(bench.workspace))
    with pytest.raises(KeyError):
        assemble(bench, replace(matter, slug='m-' + '0' * 12))


def test_external_change_and_short_receipt_page_refuse_partial_briefing(workbench, monkeypatch):
    client, bench, matter, _runtime = workbench
    selection(client, matter.slug, [descriptor('Unsupported/one.bin', 25), descriptor('Unsupported/two.bin', 25)], [], key='a' * 32)
    receipts = IntakeReceipts(bench.workspace)
    items = receipts.items
    monkeypatch.setattr(receipts, 'items', lambda *args, **kwargs: items(*args, **kwargs)[:-1])
    with pytest.raises(WorkspaceProblem, match='intake inventory changed'):
        build_briefing(matter, WEB_ACTOR, workspace=bench.workspace, entity_service=bench.entity_service(matter), intake_receipts=receipts)
    service = bench.entity_service(matter)
    date_draft = service.date_draft
    def changed(*args, **kwargs):
        result = date_draft(*args, **kwargs)
        with sqlite3.connect(bench.workspace.path) as other:
            other.execute("UPDATE workbench_matter SET descriptor='Synthetic concurrent edit' WHERE matter_id=?", (matter.matter_id,))
        return result
    monkeypatch.setattr(service, 'date_draft', changed)
    with pytest.raises(WorkspaceProblem, match='Matter records changed'):
        build_briefing(matter, WEB_ACTOR, workspace=bench.workspace, entity_service=service, intake_receipts=IntakeReceipts(bench.workspace))


def test_read_limit_refuses_a_partial_inventory(workbench, monkeypatch):
    import case_intelligence.briefing as module
    client, bench, matter, _runtime = workbench
    for index in range(2):
        upload(client, matter.slug, f'Generated {index}.txt', f'Row {index}.'.encode())
    ready(bench, matter)
    monkeypatch.setattr(module, 'MAX_ROWS', 1)
    with pytest.raises(WorkspaceProblem, match='no partial briefing'):
        assemble(bench, matter)


def test_mixed_file_types_and_inventory_extraction_reason(workbench, monkeypatch):
    import io
    from types import SimpleNamespace
    from case_intelligence.malware_scan import MalwareScanResult
    client, bench, matter, _runtime = workbench
    store = bench.source_store(matter)
    monkeypatch.setattr(store, 'malware_scanner', SimpleNamespace(scan=lambda _path: MalwareScanResult('clean', 'synthetic')))
    table, _ = store.store_stream('Generated table.csv', 'text/csv', io.BytesIO(b'Label,Value\nSynthetic,1\n'))
    document, _ = store.store_stream('Generated memo.txt', 'text/plain', io.BytesIO(b'Synthetic memo.'))
    with store.mutation_guard():
        document.state = 'needs_ocr'
        document.message = 'Synthetic original extraction reason'
        bench._sync_source_catalog(matter, tuple(store.documents.values()))
    briefing = assemble(bench, matter)
    assert [(line.value, line.count) for line in lines(briefing, 'arrived', 'file_type')] == [('SPREADSHEET', 1), ('TXT', 1)]
    assert 'Synthetic original extraction reason' in lines(briefing, 'unread', 'source_incomplete')[0].text
    assert_links_open(client, matter, briefing)


def test_date_read_limit_accepts_boundary_and_refuses_overage(workbench, monkeypatch):
    import case_intelligence.briefing as module
    client, bench, matter, _runtime = workbench
    upload(client, matter.slug, 'Generated dates.txt', b'Date 2024-01-02. Date 2024-01-03.')
    ready(bench, matter)
    bench.run_automatic_discovery_once()
    # The exact threshold is configurable here without thousands of fixtures;
    # both sides exercise the real paginated EntityService, not fake rows.
    monkeypatch.setattr(module, 'MAX_DATE_MENTIONS', 2)
    briefing = assemble(bench, matter)
    assert briefing.section('dates').total == 2
    assert [question.text for question in briefing.questions] == [
        'What do the records say about the mentioned date 2024-01-02?',
        'What do the records say about the mentioned date 2024-01-03?',
    ]
    monkeypatch.setattr(module, 'MAX_DATE_MENTIONS', 1)
    with pytest.raises(WorkspaceProblem, match='complete found-date briefing limit'):
        assemble(bench, matter)


QUESTION_SLUG = 'm-' + '1' * 12


def question_line(kind, value, count=1, *, links=None, suggested=True):
    links = (BriefingLink('Open generated passage', f'/matters/{QUESTION_SLUG}?support=generated-token#support-pane'),) if links is None else links
    return BriefingLine(kind, value, 'Suggested synthetic input', links, count, suggested)


def question_sections(rows):
    rows = tuple(rows)
    return tuple(BriefingSection(key, key, tuple(line for line in rows if line.kind in kinds))
        for key, kinds in (('people_places', ('person', 'place')), ('dates', ('mentioned_date',))))


def test_question_selection_diversity_frequency_ties_and_five_item_cap():
    rows = [question_line('place', 'West Depot', 2), question_line('person', 'Zed Example', 8),
        question_line('mentioned_date', '2024-05-06', 20), question_line('person', 'Alice Example', 8),
        question_line('mentioned_date', '2024-05-07', 1), question_line('place', 'Central Depot', 6),
        question_line('person', 'Riley Sample', 2)]
    sections = question_sections(rows)
    questions = _questions(QUESTION_SLUG, sections)
    assert [question.text for question in questions] == [
        'What do the records say about Alice Example?',
        'What do the records say about Central Depot?',
        'What do the records say about the mentioned date 2024-05-06?',
        'What do the records say about Zed Example?',
        'What do the records say about West Depot?',
    ]
    assert _questions(QUESTION_SLUG, question_sections(reversed(rows))) == questions
    assert questions == _questions(QUESTION_SLUG, sections)
    assert all(question.label == 'Suggested' and classify(question.text).kind == 'question' for question in questions)
    assert Briefing('generated', QUESTION_SLUG, sections, ()).questions == ()
    with pytest.raises(FrozenInstanceError):
        questions[0].text = 'Changed'


def test_question_deduplication_crosses_kinds_and_whitespace_before_filling_cap():
    rows = [question_line('person', 'Alex Example', 10), question_line('place', 'Alex Example', 9),
        question_line('place', ' alex   EXAMPLE ', 8), question_line('place', 'Central Depot', 7),
        question_line('person', 'Alex   Example', 6), question_line('person', 'Blair Sample', 5),
        question_line('mentioned_date', '03/04/2026', 2), question_line('place', 'West Depot', 1)]
    questions = _questions(QUESTION_SLUG, question_sections(rows))
    assert [question.text for question in questions] == [
        'What do the records say about Alex Example?',
        'What do the records say about Central Depot?',
        'What do the records say about the mentioned date 03/04/2026?',
        'What do the records say about Blair Sample?',
        'What do the records say about West Depot?',
    ]
    assert len({' '.join(question.text.split()).casefold() for question in questions}) == 5


def test_questions_skip_invalid_text_and_search_syntax_without_rewriting_names():
    prefix, suffix = 'What do the records say about ', '?'
    boundary = 'A' * (2000 - len(prefix) - len(suffix))
    invalid = ('', '   ', 'Alex\nExample', 'Alex\x00Example', boundary + 'B',
               'Alex "Synthetic" Example', 'Alex AND Blair', 'Alex NEAR/5 Blair')
    valid = ('Anne-Marie O’Example, Jr.', boundary)
    rows = [question_line('person', value, 10) for value in invalid]
    rows += [question_line('person', value, 1) for value in valid]
    questions = _questions(QUESTION_SLUG, question_sections(rows))
    assert [question.text for question in questions] == [prefix + value + suffix for value in sorted(valid)]
    assert len(questions[0].text) == 2000
    assert all(classify(question.text).kind == 'question' for question in questions)


@pytest.mark.parametrize('href', [
    f'/matters/{QUESTION_SLUG}/chronology',
    f'/matters/{QUESTION_SLUG}/entities/generated-entity',
    '/matters/m-222222222222?support=generated-token#support-pane',
    f'/matters/{QUESTION_SLUG}?support=#support-pane',
    f'/matters/{QUESTION_SLUG}?support=%20#support-pane',
    f'/matters/{QUESTION_SLUG}?support=generated-token',
    f'https://example.test/matters/{QUESTION_SLUG}?support=generated-token#support-pane',
])
def test_questions_require_available_same_matter_passage_support(href):
    rows = [question_line('person', 'Unavailable Example', 20, links=(BriefingLink('Generated reference', href),)),
        question_line('place', 'Unsuggested Depot', 20, suggested=False),
        question_line('person', 'Zero Count', 0), question_line('person', 'No Links', links=()),
        question_line('mentioned_date', '03/04/2026')]
    questions = _questions(QUESTION_SLUG, question_sections(rows))
    assert questions == (SuggestedQuestion('Suggested',
        'What do the records say about the mentioned date 03/04/2026?', rows[-1].links),)
