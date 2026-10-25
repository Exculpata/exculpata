import json
import html
import re
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from case_intelligence.exact_search_results import (
    ExactSearchChanged, ExactSearchUnavailable, ExactScanPolicy,
    ReferenceExactSearchBackend, search_documents,
    build_search_query, passage_preview,
)
from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.workbench import create_workbench_app


def document(index, *texts, state="ready", name=None):
    return PilotDocument(
        document_id=f"{index:032x}", display_name=name or f"Synthetic {index:04}.txt",
        stored_name="", media_type="text/plain", size=0, state=state, message="",
        units=[{"number": n, "text": text} for n, text in enumerate(texts, 1)],
        version_id=f"version-{index}",
    )


def scan(documents, query="red", **kwargs):
    return search_documents(documents, query, scope=("synthetic-matter", "", ""), **kwargs)


def test_complete_population_and_stable_pagination_beyond_ranked_limits():
    documents = [document(n, "red bicycle") for n in range(137)]
    documents += [document(200, "red", "blue"), document(201, "red", state="processing"), document(202, "")]
    first = scan(reversed(documents), "red NOT blue")
    assert first.total == 137 and first.population == 140 and first.eligible == 138
    assert first.exclusions == {"Not ready": 1, "No searchable text": 1}
    found = []
    for page in range(1, first.pages + 1):
        result = scan(documents, "red NOT blue", page=page, expected_fingerprint=first.fingerprint)
        assert result.total == 137
        found.extend(item.document_id for item in result.items)
    assert found == [item.document_id for item in documents[:137]]
    assert len(set(found)) == 137


def test_document_scope_phrases_negation_and_explanations():
    documents = [document(1, "red", "bicycle"), document(2, "red bicycle", "blue"), document(3, "red bicycle")]
    assert scan(documents, "red AND bicycle").total == 3
    result = scan(documents, '"red bicycle" NOT blue')
    assert [item.document_id for item in result.items] == [documents[2].document_id]
    assert result.items[0].matching_unit_count == 1
    assert scan([document(4, "neutral")], "NOT red").items[0].passages == ()


@pytest.mark.parametrize("change", ["text", "version", "state", "name", "membership"])
def test_source_mutation_rejects_old_page(change):
    documents = [document(1, "red")]
    first = scan(documents)
    if change == "text":
        documents[0].units[0]["text"] = "blue"
    elif change == "version":
        documents[0].version_id = "replacement"
    elif change == "state":
        documents[0].state = "processing"
    elif change == "name":
        documents[0].display_name = "renamed.txt"
    else:
        documents.append(document(2, "red"))
    with pytest.raises(ExactSearchChanged):
        scan(documents, expected_fingerprint=first.fingerprint)


@pytest.mark.parametrize("limits", [{"max_documents": 1}, {"max_characters": 1}, {"max_seconds": -1}])
def test_budget_failure_never_returns_partial_results_or_exact_total(limits):
    with pytest.raises(ExactSearchUnavailable, match="No exact total or partial results"):
        scan([document(1, "red"), document(2, "red")], **limits)


def test_reference_backend_uses_configurable_product_policy():
    backend = ReferenceExactSearchBackend(ExactScanPolicy(max_documents=1))
    with pytest.raises(ExactSearchUnavailable):
        backend.search([document(1, "red"), document(2, "red")], "red", scope=("synthetic",))


def _file_backed_source(tmp_path, texts):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    store = PilotStore(tmp_path / 'synthetic-streaming-source')
    source, _ = store.store_stream('Synthetic file-backed.txt', 'text/plain', io.BytesIO(b'Synthetic'))
    path = store.derived / source.units_file
    with path.open('w', encoding='utf-8') as stream:
        json.dump({'version': 1, 'units': [{'number': index, 'text': text,
                  'line_start': (index - 1) * 20 + 1, 'line_end': index * 20}
                  for index, text in enumerate(texts, 1)]}, stream, ensure_ascii=False)
    source.page_count = len(texts) * 20
    source.completed_units = source.total_units = len(texts)
    return source, path


def _section_backed_source(tmp_path, kind, *, store=None):
    """Exercise the real ingestion adapters and their file-backed projections."""
    from contextlib import nullcontext
    import io
    import sys
    from unittest.mock import patch
    import zipfile
    from case_intelligence.pilot_uploads import DOCUMENT_MEDIA_TYPES, PilotStore
    from tests.test_review_tools import CleanScanner, _xlsx
    from tests.test_upload_pilot import _docx_bytes
    if kind == 'docx':
        data = _docx_bytes(*(['neutral'] * 12 + ['cancelled']))
    elif kind == 'eml':
        data = b'Subject: Synthetic neutral\r\nContent-Type: text/plain; charset=utf-8\r\n\r\ncancelled\r\n'
    elif kind in {'csv', 'tsv'}:
        data = ('neutral\n' * 100 + 'cancelled\n').encode()
    else:
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(_xlsx())) as original, zipfile.ZipFile(output, 'w') as archive:
            for name in original.namelist():
                content = original.read(name)
                if name == 'xl/worksheets/sheet1.xml':
                    rows = ''.join(f'<row r="{index}"><c r="A{index}" t="inlineStr"><is><t>{text}</t></is></c></row>'
                                   for index, text in enumerate(['neutral'] * 100 + ['cancelled'], 1))
                    content = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                               f'<sheetData>{rows}</sheetData></worksheet>').encode()
                archive.writestr(name, content)
        data = output.getvalue()
    if store is None:
        store = PilotStore(tmp_path / 'synthetic-sections', malware_scanner=CleanScanner(), malware_scan_mode='extended')
    extraction = nullcontext()
    if kind == 'docx' and sys.platform == 'darwin':
        # macOS cannot apply the helper's Linux address-space resource limit.
        # Keep the production section parser and ingestion/projection adapter;
        # Linux runs the actual extraction subprocess without this test shim.
        from case_intelligence.docx_extract import DocxSection
        from case_intelligence.docx_extract_helper import _sections
        def native_sections(path):
            with zipfile.ZipFile(path) as archive:
                return tuple(DocxSection(**section) for section in _sections(archive.read('word/document.xml')))
        extraction = patch('case_intelligence.docx_extract.extract_docx_sections', side_effect=native_sections)
    with extraction:
        source, _ = store.store_stream(f'Synthetic sections.{kind}', DOCUMENT_MEDIA_TYPES['.' + kind], io.BytesIO(data))
    assert source.state == 'ready' and source.page_count == 2 and not source.units
    assert scan([source], 'cancelled').total == 1
    return source, store.derived / source.units_file


@pytest.mark.parametrize('kind', ['docx', 'eml', 'csv', 'tsv', 'xlsx'])
@pytest.mark.parametrize('damage', ['missing-last', 'missing-all', 'duplicate', 'out-of-range', 'reordered', 'count-type', 'count-mismatch'])
def test_section_backed_projection_requires_complete_count_and_numbering(tmp_path, kind, damage):
    source, path = _section_backed_source(tmp_path, kind)
    payload = json.loads(path.read_text())
    if damage == 'missing-last':
        payload['units'].pop()
    elif damage == 'missing-all':
        payload['units'].clear()
    elif damage == 'duplicate':
        payload['units'][1]['number'] = 1
    elif damage == 'out-of-range':
        payload['units'][1]['number'] = 3
    elif damage == 'reordered':
        payload['units'].reverse()
    elif damage == 'count-type':
        source.page_count = '2'
    else:
        source.page_count = 3
    path.write_text(json.dumps(payload))
    for query in ('cancelled', 'NOT cancelled'):
        with pytest.raises(ExactSearchUnavailable, match='No exact total'):
            scan([document(1, 'neutral'), source], query)


def test_docx_result_labels_and_links_match_source_review_sections(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test')
    with TestClient(app) as client:
        bench = app.state.workbench
        matter = bench.create_matter('Synthetic DOCX sections', '', 'development-taylor-morgan')
        source, _ = _section_backed_source(tmp_path, 'docx', store=bench.source_store(matter))
        bench.workspace.reconcile_source_organizations(matter.matter_id, ((source.document_id, 'upload', source.display_name),))
        response = client.get(f'/matters/{matter.slug}/exact-search', params={'q': 'cancelled'})
        assert response.status_code == 200
        link = re.search(r'class="find-location" href="([^"]+)">Section 2</a>', response.text)
        assert link and '?unit=2' in link[1]
        review = client.get(html.unescape(link[1]))
        assert review.status_code == 200 and 'Section 2 of 2' in review.text


@pytest.mark.parametrize('params,text', [
    ({'exclude': 'cancelled', 'search': '1'}, b'neutral'),
    ({'q': 'NOT cancelled'}, b'neutral'),
    ({'q': 'NOT (red AND bicycle)'}, b'red'),
    ({'q': 'NOT red OR NOT bicycle'}, b'red'),
], ids=['plain', 'advanced', 'negated-combination', 'negated-alternatives'])
def test_exclusion_only_route_explains_absent_words(tmp_path, params, text):
    import io
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test')
    with TestClient(app) as client:
        bench = app.state.workbench
        matter = bench.create_matter('Synthetic absent words', '', 'development-taylor-morgan')
        store = bench.source_store(matter)
        source, _ = store.store_stream('Synthetic neutral.txt', 'text/plain', io.BytesIO(text))
        bench.workspace.reconcile_source_organizations(matter.matter_id, ((source.document_id, 'upload', source.display_name),))
        response = client.get(f'/matters/{matter.slug}/exact-search', params=params)
        assert response.status_code == 200 and '1 source found' in response.text
        assert 'This source matched because a word, phrase, or combination excluded by the search was absent.' in response.text
        assert 'matches the exclusions' not in response.text


def test_production_text_projection_rejects_reversed_line_range(tmp_path):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    store = PilotStore(tmp_path / 'synthetic-line-range')
    source, _ = store.store_stream('Synthetic lines.txt', 'text/plain', io.BytesIO(b'neutral\nred bicycle'))
    assert scan([source]).items[0].previews[0]['location'] == 'Lines 1–2'
    path = store.derived / source.units_file
    payload = json.loads(path.read_text())
    payload['units'][0].update(line_start=20, line_end=10)
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([document(1, 'red'), source])


def test_file_backed_near_character_limit_preserves_proof_without_materializing(tmp_path, monkeypatch):
    texts = ['red ' + 'neutral ' * 5000, 'bicycle ' + 'neutral ' * 5000, 'depot']
    source, _ = _file_backed_source(tmp_path, texts)
    expected = scan([document(1, *texts)], 'red AND bicycle NOT missing')
    monkeypatch.setattr(source, '_units_loader', lambda name: pytest.fail('whole-file source loader invoked'))
    result = scan([source], 'red AND bicycle NOT missing', max_characters=sum(map(len, texts)), max_seconds=30)
    assert result.total == expected.total == 1
    assert result.items[0].passage_positions == expected.items[0].passage_positions == (1, 2)
    assert result.items[0].matching_unit_count == 2


def test_file_backed_character_overflow_stops_before_large_tail(tmp_path):
    source, path = _file_backed_source(tmp_path, ['red ' * 150_000] * 20)
    reader, reads = source._units_iterator, []
    def iterator(name, **kwargs):
        charge = kwargs['read_check']
        def read(count):
            reads.append(count)
            charge(count)
        yield from reader(name, **{**kwargs, 'read_check': read})
    source._units_iterator = iterator
    with pytest.raises(ExactSearchUnavailable, match='No exact total or partial results'):
        scan([source], max_characters=1_000_000, max_seconds=30)
    assert 0 < sum(reads) < 1_400_000 < path.stat().st_size


def test_file_backed_slow_read_expires_before_json_decode(tmp_path, monkeypatch):
    from case_intelligence import exact_search_results, pilot_uploads
    source, _ = _file_backed_source(tmp_path, ['red ' * 30_000])
    now = [0.0]
    monkeypatch.setattr(exact_search_results.time, 'monotonic', lambda: now[0])
    original = pilot_uploads.os.fdopen
    class SlowStream:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, count):
            data = self.stream.read(count)
            now[0] += 6
            return data
    def open_slow_stream(*args, **kwargs):
        return SlowStream(original(*args, **kwargs))

    monkeypatch.setattr(pilot_uploads.os, 'fdopen', open_slow_stream)
    monkeypatch.setattr(json.JSONDecoder, 'raw_decode', lambda *args: pytest.fail('decoded after slow read exceeded deadline'))
    with pytest.raises(ExactSearchUnavailable, match='No exact total or partial results'):
        scan([source])


@pytest.mark.parametrize('closed', [False, True])
def test_file_backed_oversized_record_is_rejected_before_decode(tmp_path, monkeypatch, closed):
    source, path = _file_backed_source(tmp_path, ['red'])
    path.write_text('{"version":1,"units":[{"number":1,"text":"' + 'x' * 200_000 + ('"}]}' if closed else ''))
    original = json.JSONDecoder.raw_decode
    def decode(self, raw, *args, **kwargs):
        assert len(raw) <= 1024, 'oversized record reached JSON decoder'
        return original(self, raw, *args, **kwargs)
    monkeypatch.setattr(json.JSONDecoder, 'raw_decode', decode)
    with pytest.raises(ExactSearchUnavailable, match='No exact total or partial results'):
        scan([source], max_record_chars=1024)


def test_file_backed_serialized_input_budget_charges_whitespace_before_decode(tmp_path):
    source, path = _file_backed_source(tmp_path, ['red'])
    path.write_text(' ' * 200_000 + path.read_text())
    with pytest.raises(ExactSearchUnavailable, match='No exact total or partial results'):
        scan([source], max_serialized_characters=65_536)


def test_file_backed_invalid_tail_suppresses_already_matched_sources(tmp_path):
    source, path = _file_backed_source(tmp_path, ['red'])
    path.write_text(path.read_text() + ' trailing synthetic corruption')
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([document(1, 'red'), source])


@pytest.mark.parametrize('field,value', [('start_ms', 1100), ('end_ms', 2200)])
def test_timestamp_only_media_change_invalidates_pagination(tmp_path, field, value):
    import hashlib
    import io
    from dataclasses import replace
    from case_intelligence.pilot_uploads import PilotStore, PilotUnit
    store = PilotStore(tmp_path / 'synthetic-media-projection')
    source, _ = store.store_stream('Synthetic media projection.txt', 'text/plain', io.BytesIO(b'red bicycle'))
    source.media_type = 'audio/wav'
    source.duration_ms = 10_000
    unit = PilotUnit(1, 'red bicycle', start_ms=1000, end_ms=2000,
                     excerpt_digest=hashlib.sha256(b'red bicycle').hexdigest())
    store.install_media_transcript(source.document_id, [unit])
    assert source.units_file and not source.units
    sources = [source] + [document(index, 'red') for index in range(30)]
    first = scan(sources)
    original_version = source.version_id
    store.install_media_transcript(source.document_id, [replace(unit, **{field: value})])
    assert source.version_id == original_version
    with pytest.raises(ExactSearchChanged):
        scan(sources, page=2, expected_fingerprint=first.fingerprint)


def _installed_transcript_source(tmp_path):
    import io
    from types import SimpleNamespace
    from case_intelligence.media_evidence import transcript_units
    from case_intelligence.pilot_uploads import PilotStore
    store = PilotStore(tmp_path / 'synthetic-complete-transcript')
    source, _ = store.store_stream('Synthetic transcript.txt', 'text/plain', io.BytesIO(b'Synthetic media source'))
    source.media_type, source.duration_ms = 'audio/wav', 2000
    units = transcript_units([SimpleNamespace(start_ms=start, end_ms=start + 1000,
        current_text=text, speaker_cluster='speaker-1', speaker_display_name='', speaker_identity_state='unconfirmed')
        for start, text in [(0, 'neutral opening'), (1000, 'cancelled bicycle')]])
    store.install_media_transcript(source.document_id, units)
    assert source.page_count == 2 and source.units_file and not source.units
    return source, store.derived / source.units_file


def test_installed_transcript_missing_valid_tail_invalidates_positive_and_negation_searches(tmp_path):
    source, path = _installed_transcript_source(tmp_path)
    assert scan([source], 'cancelled').total == 1
    assert scan([source], 'NOT cancelled').total == 0
    payload = json.loads(path.read_text())
    payload['units'].pop()
    path.write_text(json.dumps(payload))
    # The remaining record is valid, consecutively numbered and starts at zero;
    # only the production installer's retained segment count exposes the loss.
    for query in ('cancelled', 'NOT cancelled'):
        with pytest.raises(ExactSearchUnavailable, match='No exact total'):
            scan([document(1, 'cancelled'), source], query)


@pytest.mark.parametrize('query', ['cancelled', 'NOT cancelled'])
def test_installed_transcript_empty_projection_cannot_claim_exact_total(tmp_path, query):
    source, path = _installed_transcript_source(tmp_path)
    payload = json.loads(path.read_text())
    payload['units'] = []
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([document(1, 'cancelled'), source], query)


@pytest.mark.parametrize('query', ['cancelled', 'introduced', 'NOT cancelled'])
def test_stored_excerpt_digest_rejects_altered_text(tmp_path, query):
    source, path = _installed_transcript_source(tmp_path)
    payload = json.loads(path.read_text())
    payload['units'][1]['text'] = 'introduced bicycle'
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([document(1, 'cancelled'), source], query)


@pytest.mark.parametrize('count', [None, True, 2.0, '2', 0, -1, 1, 3])
def test_installed_timed_transcript_requires_an_exact_integer_segment_count(tmp_path, count):
    source, _ = _installed_transcript_source(tmp_path)
    source.page_count = count
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], 'cancelled')


@pytest.mark.parametrize('field,value', [
    ('number', 'one'), ('number', []), ('number', None), ('number', True),
    ('start_ms', '1000'), ('end_ms', []), ('line_start', False),
    ('line_end', 'two'), ('location_label', {}), ('text', ['red']),
])
def test_corrupt_file_backed_locator_is_unavailable_without_partial_totals(tmp_path, field, value):
    source, path = _file_backed_source(tmp_path, ['red'])
    source.media_type, source.page_count = 'application/pdf', 1
    payload = json.loads(path.read_text())
    payload['units'][0][field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([document(1, 'red'), source])


def test_missing_reader_never_becomes_an_empty_source_exclusion():
    source = document(100)
    source.units_file = 'a' * 32 + '.json'
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([document(1, 'red'), source])


@pytest.mark.parametrize('index,changes', [
    (0, {'number': 99}),
    (0, {'start_ms': 2000, 'end_ms': 1000}),
    (0, {'end_ms': 1000}),
    (1, {'start_ms': 500}),
    (1, {'end_ms': 7001}),
    (0, {'start_ms': None}),
    (0, {'end_ms': None}),
    (1, {'start_ms': None, 'end_ms': None}),
])
def test_corrupt_file_backed_media_relationships_are_unavailable(tmp_path, index, changes):
    source, path = _file_backed_source(tmp_path, ['red bicycle', 'red depot'])
    source.media_type, source.duration_ms = 'audio/wav', 5000
    source.page_count = 2
    payload = json.loads(path.read_text())
    for unit, start in zip(payload['units'], (1000, 2000)):
        unit.update(start_ms=start, end_ms=start + 1000)
    path.write_text(json.dumps(payload))
    assert scan([source]).total == 1
    payload['units'][index].update(changes)
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([document(1, 'red'), source])


def test_media_validation_preserves_untimed_legacy_units_and_pdf_page_numbers():
    legacy = document(1, 'red bicycle')
    legacy.media_type = 'audio/wav'
    legacy.page_count = 99  # Untimed legacy metadata is not a segment count.
    legacy.units[0].update(number=5, line_start=0, line_end=1000)
    result = scan([legacy])
    assert result.total == 1 and result.items[0].previews[0]['start_ms'] == 0
    pdf = document(2, 'red on page two', 'red on page one')
    pdf.media_type, pdf.page_count = 'application/pdf', 2
    pdf.units[0]['number'], pdf.units[1]['number'] = 2, 1
    assert scan([pdf]).total == 1
    timed = document(3, 'red bicycle', 'red depot')
    timed.media_type, timed.duration_ms = 'audio/wav', 5000
    timed.page_count = 2
    timed.units[0].update(start_ms=1000, end_ms=2000)
    # Equal starts and overlapping segments remain valid; the producer permits
    # an endpoint up to and including its two-second duration allowance.
    timed.units[1].update(start_ms=1000, end_ms=7000)
    assert scan([timed]).total == 1


def test_corrupt_locator_route_returns_unavailable_instead_of_server_error(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test')
    with TestClient(app) as client:
        bench = app.state.workbench
        matter = bench.create_matter('Synthetic corrupt locator', '', 'development-taylor-morgan')
        valid, corrupt = document(1, 'red'), document(2, 'red')
        corrupt.media_type, corrupt.page_count = 'application/pdf', 1
        corrupt.units[0]['number'] = 'one'
        store = bench.source_store(matter)
        store.documents.update({source.document_id: source for source in (valid, corrupt)})
        bench.workspace.reconcile_source_organizations(matter.matter_id,
            tuple((source.document_id, 'upload', source.display_name) for source in (valid, corrupt)))
        response = client.get(f'/matters/{matter.slug}/exact-search', params={'q': 'red'})
        assert response.status_code == 503
        assert 'finish this search' in response.text
        assert '1 source found' not in response.text and '0 sources found' not in response.text


def test_frozen_known_answer_corpus_runs_through_result_service():
    corpus = json.loads((Path(__file__).parent / "fixtures/synthetic/product-foundation/v1/corpus.json").read_text())
    documents = []
    for index, item in enumerate(corpus["documents"]):
        if item["matter_id"] == corpus["matter_id"]:
            source = document(index, *(unit["text"] for unit in item["units"]), state=item["state"])
            source.document_id = item["document_id"]
            documents.append(source)
    for case in corpus["exact_search_cases"]:
        assert {item.document_id for item in scan(documents, case["query"]).items} == set(case["expected_document_ids"])


def test_route_authorizes_before_scan_and_never_uses_ranked_retriever(tmp_path, monkeypatch):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic exact matter", "descriptor": "Synthetic"}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        bench = app.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        store = bench.source_store(matter)
        sources = [document(n, "red bicycle") for n in range(31)]
        sources.append(document(40, "red", state="processing"))
        store.documents.update({item.document_id: item for item in sources})
        bench.workspace.reconcile_source_organizations(matter.matter_id,
            tuple((item.document_id, "upload", item.display_name) for item in sources))
        selected = bench.workspace.create_source_set(matter.matter_id, "Synthetic subset",
            [sources[0].document_id], matter.owner_id)
        collection = bench.workspace.create_source_collection(matter.matter_id,
            "Synthetic collection", "upload", matter.owner_id)
        bench.workspace.move_sources_to_collection(matter.matter_id,
            [sources[1].document_id], collection.collection_id, matter.owner_id)
        outsider = bench.workspace.upsert_principal("preview", "synthetic-outsider",
            "Synthetic Outsider", "outsider@example.test")
        foreign = bench.create_matter("Synthetic foreign matter", "Synthetic canary", outsider.principal_id)
        foreign_document = document(100, "red SECRET-CANARY")
        bench.source_store(foreign).documents[foreign_document.document_id] = foreign_document
        monkeypatch.setattr(bench, "_retriever", lambda *args, **kwargs: pytest.fail("exact search called ranked retrieval"))
        result = client.get(f"/matters/{slug}/exact-search", params={"q": "red"})
        assert result.status_code == 200
        assert "31 sources found" in result.text and "1 still preparing or need attention" in result.text
        assert "Next →</a>" in result.text
        plain = client.get(f"/matters/{slug}/exact-search", params={"words": "red bicycle", "search": "1"})
        assert plain.status_code == 200 and "31 sources found" in plain.text
        assert 'value="red bicycle"' in plain.text and '<mark>red</mark>' in plain.text
        assert "open" not in re.search(r'<details class="find-advanced"([^>]*)>', plain.text).group(1)
        next_href = html.unescape(re.search(r'href="([^"]+)">Next →</a>', plain.text).group(1))
        assert "words=red+bicycle" in next_href
        assert "Page 2 of 2" in client.get(next_href).text
        empty = client.get(f"/matters/{slug}/exact-search", params={"search": "1"})
        assert empty.status_code == 400 and "Enter a name, word, or phrase" in empty.text
        advanced = client.get(f"/matters/{slug}/exact-search", params={"search": "1", "advanced": "1",
            "q": "red", "words": "missing", "source_set": selected.source_set_id})
        assert advanced.status_code == 200 and "1 source found" in advanced.text
        simple_again = client.get(f"/matters/{slug}/exact-search", params={"search": "1", "advanced": "0",
            "q": "missing", "words": "red"})
        assert simple_again.status_code == 200 and "31 sources found" in simple_again.text
        scoped = client.get(f"/matters/{slug}/exact-search", params={"q": "red", "source_set": selected.source_set_id})
        assert scoped.status_code == 200 and "1 source found" in scoped.text
        intersection = client.get(f"/matters/{slug}/exact-search", params={"q": "red",
            "source_set": selected.source_set_id, "collection": collection.collection_id})
        assert intersection.status_code == 200 and "0 sources found" in intersection.text
        assert "SECRET-CANARY" not in result.text
        assert client.get(f"/matters/{foreign.slug}/exact-search", params={"q": "red"}).status_code == 404
        assert client.get(f"/matters/{foreign.slug}/exact-search", params={"q": "red",
            "source_set": selected.source_set_id}).status_code == 404
        invalid = client.get(f"/matters/{slug}/exact-search", params={"q": "red AND"})
        assert invalid.status_code == 400 and "character" in invalid.text
        assert "0 sources found" not in invalid.text
        missing = client.get(f"/matters/{slug}/exact-search", params={"q": "red", "source_set": "missing"})
        assert missing.status_code == 404
        assert client.get("/matters/m-000000000000/exact-search", params={"q": "red"}).status_code == 404
        fingerprint = bench.exact_search(matter, "red").fingerprint
        store.documents[sources[0].document_id].units[0]["text"] = "blue"
        changed = client.get(f"/matters/{slug}/exact-search", params={"q": "red", "page": 2, "fingerprint": fingerprint})
        assert changed.status_code == 409 and "Search again" in changed.text


def test_unreadable_ready_source_invalidates_the_whole_scan():
    broken = document(2)
    broken.units_file = "synthetic-units.json"
    def fail(_):
        raise RuntimeError("synthetic unavailable source")
    broken._units_loader = fail
    with pytest.raises(ExactSearchUnavailable, match="No exact total"):
        scan([document(1, "red"), broken])


def test_matching_checks_deadline_inside_long_units(monkeypatch):
    from case_intelligence.exact_search import parse_query
    checks = 0
    def stop():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise ExactSearchUnavailable("synthetic deadline")
    with pytest.raises(ExactSearchUnavailable):
        parse_query('"red bicycle"').matches_units(["neutral " * 10000], budget_check=stop)
    assert checks == 3


def test_plain_search_fields_do_not_require_or_interpret_boolean_syntax():
    documents = [document(1, "red bicycle", "returned to the depot"),
                 document(2, "red bicycle", "cancelled"), document(3, "and or not")]
    result = scan(documents, build_search_query("red bicycle", "returned to the depot", "cancelled blue"))
    assert [item.document_id for item in result.items] == [documents[0].document_id]
    literal = scan(documents, build_search_query("AND OR NOT"))
    assert [item.document_id for item in literal.items] == [documents[2].document_id]
    assert scan(documents, build_search_query(exclude="cancelled")).total == 2


def test_preview_finds_late_words_preserves_text_and_keeps_markup_inert():
    from case_intelligence.exact_search import parse_query
    unit = document(1, "neutral " * 100 + '<script>depot</script> nearby').parsed_units()[0]
    preview = passage_preview(unit, parse_query("depot"))
    text = "".join(piece for piece, _ in preview["pieces"])
    assert '<script>depot</script>' in text
    assert ("depot", True) in preview["pieces"]
    assert text.startswith("…")


def test_scan_releases_workspace_lock_after_capturing_scope(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic concurrent search", "descriptor": ""}, follow_redirects=False)
        bench = app.state.workbench
        matter = bench.matter(response.headers["location"].split("/")[2], "development-taylor-morgan")
        entered, release, workspace_read = threading.Event(), threading.Event(), threading.Event()
        errors = []

        class PausingBackend:
            def search(self, documents, query, **kwargs):
                entered.set()
                assert release.wait(5)
                return ReferenceExactSearchBackend().search(documents, query, **kwargs)

        bench.exact_search_backend = PausingBackend()
        def search():
            try:
                bench.exact_search(matter, "red")
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=search)
        reader = threading.Thread(target=lambda: (bench.workspace.source_sets(matter.matter_id), workspace_read.set()))
        worker.start()
        try:
            assert entered.wait(2)
            reader.start()
            assert workspace_read.wait(2), "Text matching blocked the global workspace lock"
        finally:
            release.set()
            worker.join(5)
            if reader.ident is not None:
                reader.join(5)
        assert not errors


def test_phrase_preview_anchors_on_complete_phrase_and_preserves_unicode():
    from case_intelligence.exact_search import parse_query
    for prefix, phrase in [("red", "red bicycle"), ("cafe\u0301", "cafe\u0301 bicycle"), ("İ", "İ bicycle")]:
        unit = document(1, prefix + " isolated. " + "neutral " * 100 + phrase + " nearby").parsed_units()[0]
        preview = passage_preview(unit, parse_query('"' + phrase + '"'))
        text = "".join(piece for piece, _ in preview["pieces"])
        assert phrase in text and "isolated" not in text
        assert (phrase, True) in preview["pieces"]


def test_repeated_pdf_pages_do_not_produce_exact_results(tmp_path):
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic skipped page", "descriptor": ""}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        bench = app.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        source = document(1, "opening", "target bicycle", "following extracted page")
        source.media_type = "application/pdf"
        source.page_count = 2
        source.units[1]["number"] = 1
        source.units[2]["number"] = 2
        bench.source_store(matter).documents[source.document_id] = source
        with pytest.raises(ExactSearchUnavailable, match="No exact total"):
            bench.exact_search(matter, "bicycle")
        page = client.get(f"/matters/{slug}/exact-search", params={"words": "bicycle"})
        assert 'class="find-location"' not in page.text
        assert page.status_code == 503


def test_advanced_form_keyboard_submission_has_its_own_explicit_mode(tmp_path):
    from html.parser import HTMLParser
    class FormParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = {}
            self.active = None
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                assert self.active is None, "Forms must not be nested"
                self.active = {**attrs, "inputs": []}
                self.forms[attrs.get("id")] = self.active
            elif tag == "input" and self.active is not None:
                self.active["inputs"].append(attrs)
        def handle_endtag(self, tag):
            if tag == "form":
                self.active = None
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic expression keyboard", "descriptor": ""}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        bench = app.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        source = document(1, "red bicycle")
        bench.source_store(matter).documents[source.document_id] = source
        page = client.get(f"/matters/{slug}/exact-search", params={"words": "missing"})
        parser = FormParser()
        parser.feed(page.text)
        expression = parser.forms["find-expression-form"]
        assert any(item.get("id") == "find-query" for item in expression["inputs"])
        assert expression["id"] == "find-expression-form"
        # Enter submits successful controls without requiring a clicked button.
        params = {item["name"]: item.get("value", "") for item in expression["inputs"]}
        params["q"] = "red AND bicycle"
        result = client.get(expression["action"], params=params)
        assert result.status_code == 200 and "1 source found" in result.text
        assert "words" not in params and params["advanced"] == "1"


def test_boolean_preview_uses_only_a_satisfied_branch_even_after_many_false_starts():
    source = document(1, "red", "red again", "red once more", "green explains this match")
    result = scan([source], "(red AND bicycle) OR green")
    assert result.total == 1 and result.items[0].matching_unit_count == 1
    assert result.items[0].passage_positions == (4,)
    assert result.items[0].previews[0]["pieces"][1] == ("green", True)
    source = document(2, "red isolated. " + "neutral " * 120 + "green")
    preview = scan([source], "(red AND bicycle) OR green").items[0].previews[0]
    assert "isolated" not in "".join(text for text, _ in preview["pieces"])
    assert ("green", True) in preview["pieces"]


def test_partial_pdf_coverage_is_excluded_including_negation_and_invalidates_old_pages():
    partial = document(1, "red")
    partial.media_type, partial.page_count = "application/pdf", 2
    partial.message = "1 of 2 pages ready and searchable; 1 page needs OCR"
    complete = document(2, "red", "neutral")
    complete.media_type, complete.page_count = "application/pdf", 2
    for query in ("red", "red NOT blue", "NOT blue"):
        result = scan([partial, complete], query)
        assert result.total == result.eligible == 1 and result.population == 2
        assert result.exclusions == {"Incomplete page coverage": 1}
        assert result.items[0].document_id == complete.document_id
    previous = scan([complete])
    complete.page_count = 3
    with pytest.raises(ExactSearchChanged):
        scan([complete], expected_fingerprint=previous.fingerprint)
    complete.page_count = 0
    assert scan([complete]).exclusions == {"Incomplete page coverage": 1}


def test_preview_work_consumes_the_same_search_deadline(monkeypatch):
    import case_intelligence.exact_search_results as module
    original_preview = module.passage_preview
    clock = {"preview": False, "checks": 0}
    def monotonic():
        if clock["preview"]:
            clock["checks"] += 1
            return clock["checks"] * .25
        return 0
    def preview(*args, **kwargs):
        clock["preview"] = True
        return original_preview(*args, **kwargs)
    monkeypatch.setattr(module.time, "monotonic", monotonic)
    monkeypatch.setattr(module, "passage_preview", preview)
    source = document(1, "term0 " + "neutral " * 20000)
    query = " OR ".join(f"term{index}" for index in range(40))
    with pytest.raises(ExactSearchUnavailable, match="No exact total or partial results"):
        scan([source], query, max_seconds=1)
    assert clock["preview"] and clock["checks"] == 5


def test_media_timestamp_links_and_partial_page_warning_are_visible(tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace
    from case_intelligence.media_evidence import transcript_units
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        response = client.post("/matters", data={"name": "Synthetic transcript locator", "descriptor": ""}, follow_redirects=False)
        slug = response.headers["location"].split("/")[2]
        bench = app.state.workbench
        matter = bench.matter(slug, "development-taylor-morgan")
        media = document(1, "opening", "red bicycle appears at this moment")
        media.media_type = "audio/wav"
        media.duration_ms = 46000
        media.page_count = 2
        segments = [SimpleNamespace(start_ms=start, end_ms=start + 1000,
            current_text=text, speaker_cluster="speaker-1", speaker_display_name="",
            speaker_identity_state="unconfirmed") for start, text in
            [(0, "opening"), (45000, "red bicycle appears at this moment")]]
        media.units = [asdict(unit) for unit in transcript_units(segments)]
        partial = document(2, "red")
        partial.media_type, partial.page_count = "application/pdf", 2
        bench.source_store(matter).documents.update({item.document_id: item for item in (media, partial)})
        result = client.get(f"/matters/{slug}/exact-search", params={"words": "red"})
        assert result.status_code == 200 and "1 source found" in result.text
        assert "have incomplete page coverage and were left out" in result.text
        match = re.search(r'<a class="find-location" href="([^"]+)">', result.text)
        assert match and match.group(1).endswith("&amp;start_ms=45000&amp;unit=2#segment-2")
        assert "00:45–00:46" in result.text and "Lines 45000" not in result.text


def test_boolean_preview_diversifies_support_for_each_required_positive_term():
    source = document(1, "red", "red again", "red once more", "bicycle")
    item = scan([source], "red AND bicycle").items[0]
    assert item.passage_positions[:2] == (1, 4)
    assert item.matching_unit_count == 4


def test_concurrent_exact_requests_are_rejected_before_worker_dispatch(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        created = client.post("/matters", data={"name": "Synthetic admission", "descriptor": ""}, follow_redirects=False)
        slug = created.headers["location"].split("/")[2]
        entered, release = threading.Event(), threading.Event()
        bench = app.state.workbench
        original = bench.exact_search
        calls = []
        def blocked(*args, **kwargs):
            calls.append(1)
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(bench, "exact_search", blocked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.get, f"/matters/{slug}/exact-search?q=red")
            assert entered.wait(3)
            try:
                for _ in range(8):
                    rejected = client.get(f"/matters/{slug}/exact-search?q=red")
                    assert rejected.status_code == 429
                    assert rejected.headers["Retry-After"] == "1"
                assert client.get("/health").status_code == 200
                assert len(calls) == 1 and not first.done()
            finally:
                release.set()
            assert first.result(timeout=3).status_code == 200
        assert client.get(f"/matters/{slug}/exact-search?q=red").status_code == 200
        assert len(calls) == 2


def test_foreign_matter_admission_stays_private_after_membership_revocation(tmp_path, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        bench = app.state.workbench
        owner = bench.workspace.upsert_principal("preview", "synthetic-owner",
            "Synthetic Owner", "owner@example.test")
        matter = bench.create_matter("Synthetic restricted matter", "", owner.principal_id)
        actor = "development-taylor-morgan"
        url = f"/matters/{matter.slug}/exact-search?q=red"
        entered, release = threading.Event(), threading.Event()
        original_search, original_matter = bench.exact_search, bench.matter
        calls = []

        def authorize(*args, **kwargs):
            # Synchronous membership lookup must remain off the event loop.
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            return original_matter(*args, **kwargs)

        def blocked(*args, **kwargs):
            calls.append(1)
            entered.set()
            assert release.wait(5)
            return original_search(*args, **kwargs)

        monkeypatch.setattr(bench, "matter", authorize)
        monkeypatch.setattr(bench, "exact_search", blocked)
        idle = client.get(url)
        assert idle.status_code == 404
        bench.workspace.add_member(matter.matter_id, actor, owner.principal_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.get, url)
            assert entered.wait(3)
            try:
                bench.workspace.revoke_member(matter.matter_id, actor, owner.principal_id)
                for _ in range(4):
                    busy = client.get(url)
                    assert busy.status_code == idle.status_code == 404
                    assert busy.content == idle.content
                    assert "Retry-After" not in busy.headers
                assert len(calls) == 1 and not first.done()
                assert client.get("/health").status_code == 200
            finally:
                release.set()
            assert first.result(timeout=3).status_code == 200
        assert client.get(url).status_code == 404
        assert len(calls) == 1


def test_non_scanning_pages_remain_available_while_matter_is_busy(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test")
    with TestClient(app) as client:
        created = client.post("/matters", data={"name": "Synthetic busy form", "descriptor": ""}, follow_redirects=False)
        slug = created.headers["location"].split("/")[2]
        url = f"/matters/{slug}/exact-search"
        bench = app.state.workbench
        original = bench.exact_search
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return original(*args, **kwargs)
        monkeypatch.setattr(bench, "exact_search", blocked)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.get, url, params={"q": "red"})
            assert entered.wait(3)
            try:
                for params, status in [({}, 200), ({"search": "1"}, 400),
                    ({"q": "red AND"}, 400), ({"q": "red", "page_size": "7"}, 400),
                    ({"q": "red", "page": "0"}, 422), ({"q": "red", "source_set": "missing"}, 404)]:
                    response = client.get(url, params=params)
                    assert response.status_code == status, (params, response.text)
                    assert "Retry-After" not in response.headers
            finally:
                release.set()
            assert first.result(timeout=3).status_code == 200


@pytest.mark.parametrize('start,expected', [(0, '00:00–00:01'), (45000, '00:45–00:46')])
def test_production_transcript_projection_displays_timestamps(start, expected):
    from dataclasses import asdict
    from types import SimpleNamespace
    from case_intelligence.exact_search import parse_query
    from case_intelligence.media_evidence import transcript_units
    segments = [SimpleNamespace(start_ms=start, end_ms=start + 1000, current_text="red bicycle", speaker_cluster="speaker-1", speaker_display_name="", speaker_identity_state="unconfirmed")]
    unit = transcript_units(segments)[0]
    preview = passage_preview(unit, parse_query("red"))
    assert preview["location"] == expected
    assert preview["start_ms"] == start
    source = document(1)
    source.media_type, source.duration_ms = 'audio/wav', start + 1000
    source.page_count = 1
    source.units = [asdict(unit)]
    assert scan([source]).items[0].previews[0]['location'] == expected


@pytest.mark.parametrize('offset', [0, 45000])
def test_legacy_media_result_links_seek_to_offset_and_extracted_position(tmp_path, offset):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test')
    with TestClient(app) as client:
        response = client.post('/matters', data={'name': 'Synthetic legacy media seek'}, follow_redirects=False)
        slug = response.headers['location'].split('/')[2]
        bench = app.state.workbench
        matter = bench.matter(slug, 'development-taylor-morgan')
        media = document(1, 'opening', 'red bicycle', 'red depot')
        media.version_id = 'a' * 32
        media.media_type, media.duration_ms = 'audio/wav', 60000
        for number, unit, start in zip((5, 8, 12), media.units, (0, offset, 50000)):
            unit.update(number=number, line_start=start, line_end=start + 1000)
        bench.source_store(matter).documents[media.document_id] = media
        bench._sync_source_catalog(matter, [media])
        result = client.get(f'/matters/{slug}/exact-search', params={'words': 'red'})
        assert result.status_code == 200 and '1 source found' in result.text
        links = re.findall(r'<a class="find-location" href="([^"]+)">', result.text)
        assert len(links) == 2
        assert links[0].endswith(f'&amp;start_ms={offset}&amp;unit=2#segment-2')
        assert links[1].endswith('&amp;start_ms=50000&amp;unit=3#segment-3')
        opened = client.get(html.unescape(links[0]))
        assert opened.status_code == 200
        assert f'data-media-review data-start-ms="{offset}"' in opened.text
        assert 'Return to review context' in opened.text


@pytest.mark.parametrize('query', ['cancelled', 'neutral NOT cancelled'])
def test_missing_middle_production_text_chunk_invalidates_exact_total(tmp_path, query):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    from tests.test_review_tools import CleanScanner
    store = PilotStore(tmp_path / 'synthetic-text', malware_scanner=CleanScanner())
    data = ('neutral\n' * 20 + 'cancelled\n' * 20 + 'neutral\n').encode()
    source, _ = store.store_stream('Synthetic chunks.txt', 'text/plain', io.BytesIO(data))
    assert source.state == 'ready' and source.page_count == 41
    path = store.derived / source.units_file
    payload = json.loads(path.read_text())
    assert [unit['number'] for unit in payload['units']] == [1, 2, 3]
    assert scan([source], 'cancelled').total == 1
    del payload['units'][1]
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], query)


@pytest.mark.parametrize('offset', [0, 45000])
def test_media_search_links_open_matching_page_despite_overlapping_segments(tmp_path, monkeypatch, offset):
    from dataclasses import replace
    from types import SimpleNamespace
    from case_intelligence.workspace_store import TranscriptSegmentRecord
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test')
    with TestClient(app) as client:
        response = client.post('/matters', data={'name': 'Synthetic paged transcript'}, follow_redirects=False)
        slug = response.headers['location'].split('/')[2]
        bench = app.state.workbench
        matter = bench.matter(slug, 'development-taylor-morgan')
        media = document(1, *(['neutral'] * 200 + ['red bicycle', 'red depot']))
        media.version_id = 'a' * 32
        media.media_type, media.duration_ms = 'audio/wav', 60000
        for unit in media.units:
            unit.update(line_start=offset, line_end=offset + 1000)
        bench.source_store(matter).documents[media.document_id] = media
        bench._sync_source_catalog(matter, [media])
        segments = tuple(TranscriptSegmentRecord(
            segment_id=f'synthetic-segment-{index}', transcript_id='synthetic-transcript',
            matter_id=matter.matter_id, ordinal=index, external_segment_id=str(index),
            start_ms=offset, end_ms=offset + 1000, speaker_cluster='', model_text=unit['text'],
            translated_text=None, confidence=None, low_confidence=0, overlap=1,
            current_text=unit['text'], current_revision=0, speaker_display_name='',
            speaker_identity_state='unconfirmed', speaker_revision=0, created_at='2026-01-01T00:00:00Z',
        ) for index, unit in enumerate(media.units, 1))
        original = bench.media_review
        monkeypatch.setattr(bench, 'media_review', lambda *args, **kwargs: replace(original(*args, **kwargs), segments=segments, transcript=SimpleNamespace(segment_count=len(segments), review_state='machine_draft', warnings=(), quality={})))
        result = client.get(f'/matters/{slug}/exact-search', params={'words': 'red'})
        links = re.findall(r'<a class="find-location" href="([^"]+)">', result.text)
        assert len(links) == 2
        for ordinal, link in zip((201, 202), links):
            opened = client.get(html.unescape(link))
            assert opened.status_code == 200
            assert f'id="segment-{ordinal}"' in opened.text
            assert 'id="segment-1"' not in opened.text
            assert f'data-media-review data-start-ms="{offset}"' in opened.text
        assert 'Return to review context' in opened.text


@pytest.mark.parametrize('query', ['cancelled', 'neutral NOT cancelled'])
def test_missing_final_production_text_chunk_invalidates_exact_total(tmp_path, query):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    from tests.test_review_tools import CleanScanner
    store = PilotStore(tmp_path / 'synthetic-tail', malware_scanner=CleanScanner())
    source, _ = store.store_stream('Synthetic tail.txt', 'text/plain', io.BytesIO(('neutral\n' * 40 + 'cancelled\n').encode()))
    assert scan([source], 'cancelled').total == 1
    path = store.derived / source.units_file
    payload = json.loads(path.read_text()); del payload['units'][-1]; path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], query)


@pytest.mark.parametrize('blank', ['', ' \n\t'])
@pytest.mark.parametrize('query', ['red', 'NOT red'])
def test_blank_timed_transcript_with_matching_digest_invalidates_scan(blank, query):
    import hashlib
    source = document(1, blank)
    source.media_type, source.duration_ms, source.page_count = 'audio/wav', 1000, 1
    source.units[0].update(start_ms=0, end_ms=1000, excerpt_digest=hashlib.sha256(blank.encode()).hexdigest())
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], query)


def test_media_seek_with_producer_timestamp_slack_opens_and_uses_transcript_count(tmp_path):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test')
    with TestClient(app) as client:
        response = client.post('/matters', data={'name': 'Synthetic longest recording'}, follow_redirects=False)
        slug = response.headers['location'].split('/')[2]
        bench = app.state.workbench
        matter = bench.matter(slug, 'development-taylor-morgan')
        media = document(1, 'red bicycle', 'red depot')
        media.version_id = 'a' * 32
        media.media_type, media.duration_ms, media.page_count = 'audio/wav', 43200000, 2
        for unit in media.units:
            unit.update(start_ms=43200001, end_ms=43201000)
        bench.source_store(matter).documents[media.document_id] = media
        bench._sync_source_catalog(matter, [media])
        result = client.get(f'/matters/{slug}/exact-search', params={'words': 'red'})
        links = re.findall(r'<a class="find-location" href="([^\"]+)">', result.text)
        assert len(links) == 2
        for link in links:
            opened = client.get(html.unescape(link))
            assert opened.status_code == 200
            assert 'data-media-review data-start-ms="43199999"' in opened.text
        assert '2 matching transcript passages' in result.text
        assert '2 matching pages or sections' not in result.text


@pytest.mark.parametrize('blank_tail', [False, True])
def test_production_txt_chunk_count_survives_backup_and_clean_restore(tmp_path, blank_tail):
    import io
    import shutil
    from case_intelligence.pilot_uploads import PilotStore
    from tests.test_review_tools import CleanScanner
    root = tmp_path / 'synthetic-count-source'
    store = PilotStore(root, malware_scanner=CleanScanner())
    text = 'neutral\n' * 20 + '\n' * 20 + 'red bicycle\n' + ('\n' * 40 if blank_tail else '')
    source, _ = store.store_stream('Synthetic sparse text.txt', 'text/plain', io.BytesIO(text.encode()))
    assert source.completed_units == source.total_units == 2
    assert scan([source], 'red').total == 1
    identity = source.document_id
    store.close()
    backup = tmp_path / 'synthetic-count-backup'
    shutil.copytree(root, backup)
    payload = json.loads((root / 'derived' / source.units_file).read_text())
    del payload['units'][-1]
    (root / 'derived' / source.units_file).write_text(json.dumps(payload))
    reopened = PilotStore(root)
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([reopened.get(identity)], 'red')
    reopened.close()
    restored_root = tmp_path / 'synthetic-count-restored'
    shutil.copytree(backup, restored_root)
    restored = PilotStore(restored_root)
    restored_source = restored.get(identity)
    assert restored_source.total_units == 2 and scan([restored_source], 'red').total == 1
    restored.close()


def test_legacy_txt_line_progress_requires_a_verifiable_tail(tmp_path):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    from tests.test_review_tools import CleanScanner
    store = PilotStore(tmp_path / 'synthetic-legacy-lines', malware_scanner=CleanScanner())
    source, _ = store.store_stream('Synthetic legacy lines.txt', 'text/plain', io.BytesIO(('neutral\n' * 40 + 'red\n').encode()))
    source.completed_units = source.total_units = source.page_count
    assert scan([source], 'red').total == 1
    path = store.derived / source.units_file
    payload = json.loads(path.read_text()); del payload['units'][-1]; path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], 'NOT red')


@pytest.mark.parametrize('line_count', [0, -1, True, '41'])
def test_production_txt_rejects_invalid_original_line_count(tmp_path, line_count):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    from tests.test_review_tools import CleanScanner
    store = PilotStore(tmp_path / 'synthetic-invalid-lines', malware_scanner=CleanScanner())
    source, _ = store.store_stream('Synthetic invalid lines.txt', 'text/plain', io.BytesIO(b'red bicycle'))
    source.page_count = line_count
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], 'NOT cancelled')


@pytest.mark.parametrize('query', ['red', 'NOT missing'])
@pytest.mark.parametrize('index,start,end', [(0, 999, 1000), (1, 1, 20), (0, 2, 20), (0, 1, 19), (2, 41, 42), (0, None, None)])
def test_production_txt_rejects_impossible_retained_line_ranges(tmp_path, query, index, start, end):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    from tests.test_review_tools import CleanScanner
    store = PilotStore(tmp_path / 'synthetic-line-ranges', malware_scanner=CleanScanner())
    source, _ = store.store_stream('Synthetic ranges.txt', 'text/plain', io.BytesIO(('red\n' * 41).encode()))
    assert source.total_units == 3 and source.page_count == 41
    assert scan([source], query).total == 1
    path = store.derived / source.units_file
    payload = json.loads(path.read_text())
    payload['units'][index].update(line_start=start, line_end=end)
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], query)


@pytest.mark.parametrize('query', ['red', 'NOT missing'])
@pytest.mark.parametrize('extra_text', ['red', 'synthetic injected red'])
def test_file_backed_pdf_duplicate_page_invalidates_exact_scan(tmp_path, query, extra_text):
    import hashlib
    source, path = _file_backed_source(tmp_path, ['red', 'neutral'])
    source.media_type, source.page_count = 'application/pdf', 2
    assert scan([source], query).total == 1
    payload = json.loads(path.read_text())
    payload['units'].append({'number': 1, 'text': extra_text, 'excerpt_digest': hashlib.sha256(extra_text.encode()).hexdigest()})
    path.write_text(json.dumps(payload))
    with pytest.raises(ExactSearchUnavailable, match='No exact total'):
        scan([source], query)


@pytest.mark.parametrize('media_type', ['audio/wav', 'video/mp4', 'application/pdf'])
def test_many_matching_passages_use_media_specific_overflow_guidance(tmp_path, media_type):
    app = create_workbench_app(tmp_path / 'runtime', auth_mode='test')
    with TestClient(app) as client:
        response = client.post('/matters', data={'name': 'Synthetic overflow guidance'}, follow_redirects=False)
        slug = response.headers['location'].split('/')[2]
        bench = app.state.workbench
        matter = bench.matter(slug, 'development-taylor-morgan')
        source = document(1, *['red bicycle'] * 4)
        source.media_type, source.page_count = media_type, 4
        source.version_id = 'a' * 32
        bench.source_store(matter).documents[source.document_id] = source
        bench._sync_source_catalog(matter, [source])
        page = client.get(f'/matters/{slug}/exact-search', params={'words': 'red'})
        assert page.status_code == 200
        expected = 'transcript passage' if media_type != 'application/pdf' else 'page or section'
        assert 'Open the source to review every matching ' + expected + '.' in page.text
