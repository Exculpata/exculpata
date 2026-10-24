"""Synthetic production accounting and source traceability."""
from dataclasses import replace

import pytest

from case_intelligence.contracts import SourceLocator
from case_intelligence.pilot_uploads import PilotDocument
from case_intelligence.production_summary import ProductionSource, build_summary


def source(key='a', text='', *, media='application/pdf', pages=1, state='ready', message='', labels=()):
    document = PilotDocument(key, key + '.pdf', '', media, 10, state, message,
                             [{'number': 1, 'text': text}] if text else [], version_id='v-' + key,
                             page_count=pages)
    return ProductionSource(document, 'token-' + key, tuple(SourceLocator(bates_start=x) for x in labels))


def facts(summary, kind):
    return [fact for fact in summary.facts if fact.kind == kind]


def test_counts_pages_and_all_contributing_documents():
    summary = build_summary('Synthetic', 'matter-test', [source('a'), source('b', pages=3),
        source('c', media='text/plain', pages=900)])
    assert facts(summary, 'documents')[0].count == 3
    assert len(facts(summary, 'documents')[0].links) == 3
    assert facts(summary, 'pages')[0].count == 4
    assert [f.count for f in facts(summary, 'type')] == [2, 1]
    assert {link.document_id for link in facts(summary, 'pages')[0].links} == {'a', 'b'}


def test_date_range_excludes_invalid_and_ambiguous_dates_and_preserves_support():
    summary = build_summary('Synthetic', 'm', [source('a', '2034-04-20 2034-02-30 01/02/2034'),
        source('b', '2034-04-16'), source('c', 'No date')])
    fact = facts(summary, 'date_range')[0]
    assert fact.text == 'Mentioned ISO date range: 2034-04-16 to 2034-04-20'
    assert {link.document_id for link in fact.links} == {'a', 'b'}
    assert facts(summary, 'undated')[0].count == 1


@pytest.mark.parametrize('labels,expected', [
    (['A001', 'A003'], ['A002']),
    (['A001', 'A004'], ['A002 to A003']),
    (['A001', 'B004'], []),
    (['A001x', 'A004x'], []),
    (['A001', 'A002', 'A003'], []),
    (['A001', 'A001', 'A003'], ['A002']),
    (['A1', 'A1000000000'], ['A2 to A999999999']),
])
def test_bates_gaps(labels, expected):
    summary = build_summary('Synthetic', 'm', [source(str(i), labels=[label]) for i, label in enumerate(labels)])
    gaps = facts(summary, 'bates_gap')
    assert [gap.text.removeprefix('Missing Bates: ') for gap in gaps] == expected
    assert all(len(gap.links) == 2 for gap in gaps)
    if any(label.endswith('x') for label in labels):
        assert facts(summary, 'bates_unordered')


def test_ranges_overlap_without_false_gaps():
    first = replace(source('a'), locators=(SourceLocator(bates_start='A001', bates_end='A005'),))
    summary = build_summary('Synthetic', 'm', [first, source('b', labels=['A003']), source('c', labels=['A007'])])
    gap = facts(summary, 'bates_gap')[0]
    assert gap.text == 'Missing Bates: A006'
    assert {link.document_id for link in gap.links} == {'a', 'c'}


def test_failed_unreadable_and_ocr_failures_remain_visible():
    broken = source('d')
    broken.document.units_file = 'unavailable.jsonl'
    summary = build_summary('Synthetic', 'm', [source('a', state='failed', message='Unreadable file'),
        source('b', state='needs_ocr', message='OCR failed: 1'),
        source('c', 'Native text', message='OCR timed out: 1'), broken])
    assert facts(summary, 'documents')[0].count == 4
    assert {link.document_id for f in facts(summary, 'readability') for link in f.links} == {'a', 'b', 'c', 'd'}
    assert 'Derived text unavailable' in summary.to_markdown()


@pytest.mark.parametrize('filename', ['b.pdf', 'folder/b.pdf'])
def test_references_match_selected_names_only_and_cite_referring_passages(filename):
    present = source('b')
    present.document.display_name = filename
    summary = build_summary('Synthetic', 'm', [source('a', 'Reference: B.PDF\nReference: missing.pdf'), present])
    assert len(facts(summary, 'absent_reference')) == 1
    gap = facts(summary, 'absent_reference')[0]
    assert gap.text == 'Referenced file absent from this production: missing.pdf'
    assert gap.links[0].document_id == 'a' and '?unit=1' in gap.links[0].href


def test_entities_keep_distinct_identities_and_current_passage_support():
    entity = dict(entity_id='e1', entity_type='person', display_name='Alex Example', status='suggested')
    mention = dict(document_id='a', source_version_id='v-a', support_token='synthetic',
                   available=True, review_status='suggested', location='Page 1', excerpt='Alex Example arrived.')
    entries = [(entity, [mention, dict(mention, source_version_id='old'), dict(mention, review_status='dismissed')]),
               (dict(entity, entity_id='e2'), [dict(mention, available=False)])]
    summary = build_summary('Synthetic', 'm', [source('a', labels=['A001'])], entities=entries)
    person = facts(summary, 'person')[0]
    assert person.count == 1 and 'suggested' in person.text
    assert person.links[0].href == '/matters/m?support=synthetic#support-pane'
    assert 'Alex Example arrived.' in summary.to_markdown()
    assert [fact.count for fact in facts(summary, 'entity_unavailable')] == [1, 1]


def test_empty_duplicate_scope_and_safe_markdown():
    assert facts(build_summary('Empty', 'm', []), 'documents')[0].count == 0
    with pytest.raises(ValueError, match='Duplicate'):
        build_summary('Synthetic', 'm', [source('a'), source('a')])
    markdown = build_summary('<script>[x](https://example.test)', 'm', [source('a', 'Reference: <img>.pdf')]).to_markdown()
    assert '<script>' not in markdown and '<img>' not in markdown
    assert '\\[x\\]' in markdown


def test_real_ingestion_and_markdown_artifact_preserve_original(tmp_path):
    import io
    from case_intelligence.pilot_uploads import PilotStore
    store = PilotStore(tmp_path / 'sources')
    try:
        original = b'Alex Example arrived on 2034-04-16.\nBates: SYN001\nReference: absent.txt'
        doc, _ = store.store_stream('synthetic.txt', 'text/plain', io.BytesIO(original))
        summary = build_summary('Synthetic intake', 'm', [ProductionSource(doc, store.action_token(doc))])
        output = tmp_path / 'summary.md'
        # fixture-write: Export the synthetic summary to verify the actual Markdown artifact.
        output.write_text(summary.to_markdown(), encoding='utf-8')
        assert '2034-04-16 to 2034-04-16' in output.read_text()
        assert 'absent.txt' in output.read_text() and 'Bates SYN001' in output.read_text()
        assert store.get_by_action_token(store.action_token(doc)).document_id == doc.document_id
        assert store.source_path(doc.document_id, verify_digest=True).read_bytes() == original
    finally:
        store.close()


def test_sparse_pdf_passage_links_use_extracted_ordinal():
    item = source('a', pages=5)
    item.document.units = [{'number': 5, 'text': '2034-04-16'}]
    link = facts(build_summary('Synthetic', 'm', [item]), 'date_range')[0].links[0]
    assert link.location == 'Page 5' and link.href.endswith('?unit=1')


def test_oversized_production_refuses_partial_summary():
    with pytest.raises(OverflowError, match='document read limit'):
        build_summary('Synthetic', 'm', (source(str(i)) for i in range(10_001)))


def test_existing_entity_service_records_are_consumed(tmp_path):
    from tests.test_entity_workspace import setup
    from tests.test_matter_notebook import ACTOR, _reference
    workspace, matter, service = setup(tmp_path)
    try:
        entity = service.create(matter.matter_id, ACTOR, display_name='Alex Example', support='a' * 40)
        record, mentions, _history, _note = service.detail(matter.matter_id, ACTOR, entity['entity_id'])
        reference = _reference()
        item = source(reference['document_id'])
        item.document.version_id = reference['source_version_id']
        summary = build_summary('Synthetic', matter.slug, [item], entities=[(record, mentions)])
        assert facts(summary, 'person')[0].count == 1
        assert facts(summary, 'person')[0].links[0].excerpt == reference['excerpt']
    finally:
        workspace.close()
