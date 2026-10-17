"""Synthetic PDF coverage receipts through ingest, search, review and export.

The answer client echoes extracted text; these checks do not qualify model or
OCR accuracy. Real image/OCR selection regressions are tested separately.
"""
from __future__ import annotations

from tests.fixture_write import fixture_write

from dataclasses import asdict
import io
import json
import sys
import time
import zipfile

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from case_intelligence.full_text_review import FullTextReviewLedger
from case_intelligence.pdf_coverage import PDF_COVERAGE_NOTICE, PDF_SAVED_RESULT_NOTICE
from case_intelligence.workbench import create_workbench_app
from tests.test_email_attachment_coverage import EvidenceEchoGenerator
from tests.test_review_tools import ACTOR, CleanScanner, _matter


def generated_native_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 36 750 Td (GeneratedPdfCoverageCanary describes a synthetic meeting.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class CoverageGenerator(EvidenceEchoGenerator):
    def classify_source(self, **kwargs):
        return {'decision': 'not_identified',
                'rationale': 'The criterion was not identified in this extracted range.',
                'evidence_ids': []}


def check_historical_exports(client, bench, matter, conversation, answer):
    """Model pre-correction saved receipts, retaining actual citation evidence."""
    payload = {**answer.payload, 'source_coverage': {
        'mode': 'complete', 'searchable_count': 1, 'total_count': 1,
        'excluded_count': 0, 'notice': ''}}
    saved_payload = json.dumps(payload)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute('UPDATE workbench_message SET payload_json=? WHERE message_id=?',
                                           (saved_payload, answer.message_id))
    slug = matter.slug
    page = client.get(f'/matters/{slug}', params={'conversation': conversation.conversation_id})
    assert page.status_code == 200 and PDF_SAVED_RESULT_NOTICE in page.text
    for suffix in ('/export', f'/messages/{answer.message_id}/export'):
        for format_name in ('markdown', 'docx'):
            artifact = client.get(f'/matters/{slug}/conversations/{conversation.conversation_id}{suffix}',
                                  params={'format': format_name})
            assert artifact.status_code == 200
            if format_name == 'docx':
                with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
                    text = archive.read('word/document.xml').decode()
            else:
                text = artifact.text
            assert PDF_SAVED_RESULT_NOTICE in text

    report = bench.workspace.create_report(matter.matter_id, ACTOR, 'Generated historical PDF answer')
    bench.add_answer_to_report(matter, ACTOR, report.report_id,
                               conversation.conversation_id, answer.message_id, expected_status='draft')
    for format_name in ('markdown', 'docx'):
        artifact = client.get(f'/matters/{slug}/reports/{report.report_id}/export', params={'format': format_name})
        assert artifact.status_code == 200
        if format_name == 'docx':
            with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
                text = archive.read('word/document.xml').decode()
        else:
            text = artifact.text
        assert PDF_SAVED_RESULT_NOTICE in text

    bench.research.close()
    queued, _ = bench.workspace.queue_research_job(matter.matter_id, ACTOR,
        'What does GeneratedPdfCoverageCanary describe?', 'Generated historical PDF research',
        'research-request-' + 'e' * 32, conversation_id=conversation.conversation_id)
    claimed = bench.workspace.claim_research_job('generated-pdf-history-worker')
    assert claimed.job_id == queued.job_id
    result = bench._process_research_job(claimed, lambda: False)
    finished = bench.workspace.finish_research_job(claimed.job_id, result)
    historical_result = json.loads(json.dumps(finished.result).replace(PDF_COVERAGE_NOTICE, ''))
    historical_result['coverage']['mode'] = 'complete'
    saved_result = json.dumps(historical_result)
    with fixture_write(bench.workspace):
        bench.workspace.connection.execute('UPDATE workbench_research_job SET result_json=? WHERE job_id=?',
                                           (saved_result, claimed.job_id))
    page = client.get(f'/matters/{slug}/research', params={'job': claimed.job_id})
    assert page.status_code == 200 and PDF_SAVED_RESULT_NOTICE in page.text
    for format_name in ('markdown', 'docx', 'json'):
        artifact = client.get(f'/matters/{slug}/research/{claimed.job_id}/export', params={'format': format_name})
        assert artifact.status_code == 200
        if format_name == 'docx':
            with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
                text = archive.read('word/document.xml').decode()
        else:
            text = artifact.text
        assert PDF_SAVED_RESULT_NOTICE in text
        if format_name == 'json':
            exported = artifact.json()
            assert exported['presentation_notice'] == PDF_SAVED_RESULT_NOTICE
            assert PDF_SAVED_RESULT_NOTICE not in exported['investigation']['coverage']['notice']
    frozen = bench.workspace.source_catalog_for_export(matter.matter_id, ACTOR)
    job = bench.workspace.research_job(matter.matter_id, ACTOR, claimed.job_id)
    assert PDF_SAVED_RESULT_NOTICE in bench.export_research_work_product(
        matter, job, 'markdown', frozen_source_catalog=frozen).body.decode()
    with bench.workspace._lock:
        assert bench.workspace.connection.execute('SELECT payload_json FROM workbench_message WHERE message_id=?',
            (answer.message_id,)).fetchone()[0] == saved_payload
        assert bench.workspace.connection.execute('SELECT result_json FROM workbench_research_job WHERE job_id=?',
            (claimed.job_id,)).fetchone()[0] == saved_result


@pytest.mark.skipif(sys.platform != 'linux', reason='The bounded PDF extraction subprocess requires Linux resource limits.')
def test_pdf_coverage_survives_search_answer_and_exports_without_reprocessing(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_REVIEW_OCR_MODE', 'off')
    app = create_workbench_app(tmp_path / 'runtime', generator=CoverageGenerator(),
        auth_mode='test', malware_scanner=CleanScanner(), malware_scan_mode='extended')
    with TestClient(app) as client:
        slug = _matter(client, 'Generated PDF coverage')
        bench = app.state.workbench
        matter = bench.matter(slug, ACTOR)
        assert client.post(f'/matters/{slug}/uploads', files=[
            ('files', ('generated.pdf', generated_native_pdf(), 'application/pdf')),
        ]).status_code == 200
        store = bench.source_store(matter)
        document = next(iter(store.documents.values()))
        token = store.action_token(document)
        identity = document.document_id, document.version_id, document.digest, bench._document_content_basis(document)
        readiness = bench.workspace.matter_readiness(matter.matter_id)
        assert readiness.pdf_count == 1 and readiness.can_query and readiness.partial_query, document.message
        assert readiness.searchable_count == 1 and readiness.attention_count == 0
        assert bench.workspace.source_catalog_record(matter.matter_id, document.document_id).state_label == document.message
        source = client.get(f'/matters/{slug}/sources/{token}')
        assert source.status_code == 200 and PDF_COVERAGE_NOTICE in source.text
        assert document.message in source.text
        for route, params in (
            ('', {'mode': 'search', 'q': 'UnextractedPdfCanary'}),
            ('/exact-search', {'words': 'UnextractedPdfCanary', 'search': '1'}),
        ):
            missing = client.get(f'/matters/{slug}{route}', params=params)
            assert missing.status_code == 200 and PDF_COVERAGE_NOTICE in missing.text
            assert ('No sources found' if route else 'No matching record found') in missing.text
        found = client.get(f'/matters/{slug}/exact-search', params={
            'words': 'GeneratedPdfCoverageCanary', 'search': '1'})
        assert found.status_code == 200 and '1 source found' in found.text and 'Page 1' in found.text
        home = client.get(f'/matters/{slug}')
        assert 'You can now ask questions across the full record.' not in home.text
        assert PDF_COVERAGE_NOTICE in home.text

        conversation = bench.workspace.get_conversation(matter.matter_id)
        response = client.post(f'/matters/{slug}/ask', data={
            'conversation': conversation.conversation_id,
            'question': 'What does GeneratedPdfCoverageCanary describe?',
            'request_key': 'answer-request-' + 'd' * 32,
        }, headers={'Accept': 'application/json'})
        assert response.status_code == 202
        deadline = time.monotonic() + 10
        while True:
            status = client.get(response.json()['status_url']).json()
            if status['state'] in {'succeeded', 'failed', 'cancelled'}:
                break
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert status['state'] == 'succeeded'
        answer = bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1]
        assert answer.payload['source_coverage']['mode'] == 'partial'
        assert answer.payload['source_coverage']['excluded_count'] == 0
        assert PDF_COVERAGE_NOTICE in answer.payload['source_coverage']['notice']
        for suffix in ('/export', f'/messages/{answer.message_id}/export'):
            for format_name in ('markdown', 'docx'):
                exported = client.get(f'/matters/{slug}/conversations/{conversation.conversation_id}{suffix}',
                                      params={'format': format_name})
                assert exported.status_code == 200
                if format_name == 'docx':
                    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                        text = archive.read('word/document.xml').decode()
                else:
                    text = exported.text
                assert PDF_COVERAGE_NOTICE in text

        check_historical_exports(client, bench, matter, conversation, answer)

        # Historical receipts remain readable but never gain a completeness
        # claim or changed extraction/citation identity just by being opened.
        with store.mutation_guard():
            document.message = '1 page ready and searchable'
            store._save((document.document_id,))
        def forbid_reextraction(*args, **kwargs):
            raise AssertionError('Reading historical PDF coverage must not re-extract it.')
        monkeypatch.setattr(store, '_extract', forbid_reextraction)
        historical = client.get(f'/matters/{slug}/sources/{token}')
        assert historical.status_code == 200 and PDF_COVERAGE_NOTICE in historical.text
        assert identity == (document.document_id, document.version_id, document.digest, bench._document_content_basis(document))

        foreign_slug = _matter(client, 'Generated unrelated PDF scope')
        foreign = bench.matter(foreign_slug, ACTOR)
        assert bench.workspace.matter_readiness(foreign.matter_id).pdf_count == 0
        assert PDF_COVERAGE_NOTICE not in client.get(f'/matters/{foreign_slug}/exact-search').text
        assert client.get(f'/matters/{foreign_slug}/sources/{token}').status_code == 404

        # A finished full-text run covers extracted characters, while original
        # PDF completeness stays unknown in both the UI and portable ledgers.
        bench.full_review.close()
        _, version = bench.workspace.create_review_criterion(matter.matter_id, ACTOR,
            title='Generated extracted text check', instructions='Identify the amber bicycle.')
        queued = bench.workspace.queue_review_run(matter.matter_id, ACTOR,
            version.criterion_version_id, run_kind='full', review_mode='full_text')
        run = bench.workspace.claim_review_run('generated-pdf-coverage-worker')
        assert run.run_id == queued.run_id
        while (decision := bench.workspace.next_review_decision(run.run_id)) is not None:
            outcome = bench._process_review_decision(run, decision, lambda: False)
            bench._record_review_decision(run, decision, outcome)
        completed = bench._finish_review_run(run)
        assert completed.state == 'succeeded'
        ledger = FullTextReviewLedger(bench.workspace)
        coverage = ledger.coverage(matter.matter_id, ACTOR, run.run_id)
        assert coverage['processed_characters'] == coverage['inventoried_characters'] > 0
        assert PDF_SAVED_RESULT_NOTICE in coverage['notice']
        page = client.get(f'/matters/{slug}/full-review/{run.run_id}/text')
        assert page.status_code == 200 and PDF_COVERAGE_NOTICE in page.text
        for format_name in ('json', 'csv'):
            exported = client.get(f'/matters/{slug}/full-review/{run.run_id}/text/export', params={'format': format_name})
            assert exported.status_code == 200 and PDF_SAVED_RESULT_NOTICE in exported.text
            if format_name == 'json':
                records = exported.json()['records']
                method = next(row for row in records if row['record_type'] == 'method')
                assert PDF_SAVED_RESULT_NOTICE in method['notice']
                source = next(row for row in records if row['record_type'] == 'source')
                assert source['extraction_note'] == '1 page ready and searchable'
        bundle = client.get(f'/matters/{slug}/export')
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert PDF_COVERAGE_NOTICE in archive.read('matter-report.md').decode()
            conversations = json.loads(archive.read('conversations.json'))
            assert all(item['presentation_notice'] == PDF_SAVED_RESULT_NOTICE for item in conversations['conversations'])
            for name in archive.namelist():
                if name.startswith(('conversations/', 'reports/')) and name.endswith('.md'):
                    assert PDF_SAVED_RESULT_NOTICE in archive.read(name).decode()
        assert identity == (document.document_id, document.version_id, document.digest, bench._document_content_basis(document))


@pytest.mark.skipif(sys.platform != 'linux', reason='The bounded PDF extraction subprocess requires Linux resource limits.')
def test_saved_pdf_completeness_caution_survives_last_source_removal(tmp_path, monkeypatch):
    monkeypatch.setenv('CASE_REVIEW_OCR_MODE', 'off')
    app = create_workbench_app(tmp_path / 'runtime', generator=CoverageGenerator(),
        auth_mode='test', malware_scanner=CleanScanner(), malware_scan_mode='extended')
    with TestClient(app) as client:
        slug = _matter(client, 'Generated historical source removal')
        bench = app.state.workbench
        matter = bench.matter(slug, ACTOR)
        assert client.post(f'/matters/{slug}/uploads', files=[
            ('files', ('generated.pdf', generated_native_pdf(), 'application/pdf')),
        ]).status_code == 200
        store = bench.source_store(matter)
        document = next(iter(store.documents.values()))
        assert document.state == 'ready', document.message
        citation = bench._citation(matter, bench._candidate(matter, document, next(document.iter_parsed_units()), 1))
        conversation = bench.workspace.get_conversation(matter.matter_id)
        answer = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
            'assistant', 'GeneratedPdfCoverageCanary describes a synthetic meeting.', {
                'kind': 'generated', 'claims': [{'text': 'GeneratedPdfCoverageCanary describes a synthetic meeting.',
                                               'citations': [asdict(citation)]}],
                'source_coverage': {'mode': 'complete', 'searchable_count': 1, 'total_count': 1,
                                    'excluded_count': 0, 'notice': ''},
            })
        def saved_receipt():
            with bench.workspace._lock:
                return bench.workspace.connection.execute(
                    'SELECT payload_json FROM workbench_message WHERE message_id=?', (answer.message_id,)).fetchone()[0]
        original = saved_receipt()
        assert client.post(f'/matters/{slug}/sources/{store.action_token(document)}/remove', follow_redirects=False).status_code == 303
        assert bench.workspace.matter_readiness(matter.matter_id).pdf_count == 0
        for suffix in ('/export', f'/messages/{answer.message_id}/export'):
            for format_name in ('markdown', 'docx'):
                exported = client.get(f'/matters/{slug}/conversations/{conversation.conversation_id}{suffix}',
                                      params={'format': format_name})
                assert exported.status_code == 200
                if format_name == 'docx':
                    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                        text = archive.read('word/document.xml').decode()
                else:
                    text = exported.text
                assert PDF_SAVED_RESULT_NOTICE in text
        page = client.get(f'/matters/{slug}', params={'conversation': conversation.conversation_id})
        assert page.status_code == 200 and PDF_SAVED_RESULT_NOTICE in page.text
        bundle = client.get(f'/matters/{slug}/export')
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            conversations = json.loads(archive.read('conversations.json'))
            assert conversations['conversations'][0]['presentation_notice'] == PDF_SAVED_RESULT_NOTICE
            for name in archive.namelist():
                if name.startswith('conversations/') and name.endswith('.md'):
                    assert PDF_SAVED_RESULT_NOTICE in archive.read(name).decode()
        assert saved_receipt() == original
