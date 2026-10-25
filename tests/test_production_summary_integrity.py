"""Synthetic regressions for review findings at 8f5ea51."""
from dataclasses import replace
from io import StringIO

import pytest
from markdown_it import MarkdownIt

from case_intelligence.contracts import SourceLocator
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.production_summary import build_summary
from case_intelligence.unit_stream import iter_unit_records
from tests.test_production_summary import facts, source


def with_ranges(key, ranges):
    # Model legacy persisted locators, including the reversal admitted at 8f5ea51.
    return replace(source(key), locators=tuple(SourceLocator.model_construct(
        bates_start=start, bates_end=end) for start, end in ranges))


def overlaps(summary):
    return facts(summary, 'bates_overlap')


def partial_pdf(*, pages):
    item = source('partial', pages=pages, labels=['SYN0002'])
    item.document.units_file = 'synthetic-derived.json'

    def read_units(_name, **kwargs):
        payload = ('{"version":1,"units":[{"number":1,"text":'
                   '"2034-01-01\\nBates: SYN0002\\nReference: missing.pdf"},'
                   '{"number":2,"text":"2099-05-05"')
        for record in iter_unit_records(StringIO(payload), **kwargs):
            yield PilotUnit(**record)

    item.document._units_iterator = read_units
    return item


def test_unequal_width_reversed_locator_is_not_a_clean_gap():
    summary = build_summary("Synthetic", "m", [
        with_ranges("a", [("B1", None)]),
        with_ranges("b", [("B10", "B2")]),
        with_ranges("c", [("B12", None)]),
    ])
    assert facts(summary, "bates_gap") == []
    assert facts(summary, 'bates_unordered')


def test_locator_compares_numeric_bates_ends():
    with pytest.raises(ValueError, match='Bates end must exceed start'):
        SourceLocator(bates_start='B10', bates_end='B2')
    assert SourceLocator(bates_start='B2', bates_end='B10').bates_end == 'B10'
    assert SourceLocator(bates_start='ABC0002', bates_end='ABC00010').bates_end == 'ABC00010'


def test_leading_space_bates_renders_as_overlap():
    summary = build_summary("Synthetic", "m", [
        with_ranges("a", [("ABC0001", None)]),
        with_ranges("b", [(" ABC0001", None)]),
    ])
    assert overlaps(summary)


@pytest.mark.parametrize('first,second', [('ABC 0001', 'ABC\u00a00001'),
                                         ('ABC0001', 'ＡＢＣ０００１'),
                                         ('ABC0001', 'ABC0001 ')])
def test_equivalent_bates_labels_share_normalized_series(first, second):
    summary = build_summary('Synthetic', 'm', [source('a', labels=[first]), source('b', labels=[second])])
    assert overlaps(summary)


@pytest.mark.parametrize('label', ['ABC\u200b0002', 'ABC\u202e2000', 'ABC\t0002', '\u0410BC0002'])
def test_unsafe_or_mixed_script_bates_cannot_publish_clean_gaps(label):
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        source('b', labels=[label]), source('c', labels=['ABC0003'])])
    assert facts(summary, 'bates_unordered')
    assert not facts(summary, 'bates_gap')
    assert '\u200b' not in summary.to_markdown() and '\u202e' not in summary.to_markdown()


def test_partial_unit_read_does_not_state_a_closed_page_gap(tmp_path):
    summary = build_summary("Synthetic", "m", [partial_pdf(pages=5)])
    assert "PDF pages without extracted text" not in summary.to_markdown()
    assert facts(summary, "date_range") == [] or "incomplete" in facts(summary, "date_range")[0].text
    assert facts(summary, 'pages')[0].count == 0
    assert 'incomplete' in summary.to_markdown()
    assert not facts(summary, 'bates_reference') and not facts(summary, 'absent_reference')


def test_partial_read_excludes_dates_labels_and_closed_coverage(monkeypatch):
    def read_units(_name, **kwargs):
        for record in iter_unit_records(StringIO('{"version":1,"units":[{"number":1,"text":"2099-01-01"}]}'), **kwargs):
            yield PilotUnit(**record)
        raise OSError('Synthetic interrupted read')

    partial = partial_pdf(pages=5)
    monkeypatch.setattr(partial.document, '_units_iterator', read_units)
    summary = build_summary('Synthetic', 'm', [partial,
        source('a', '2034-05-01', labels=['SYN0001']), source('b', '2034-05-02', labels=['SYN0003'])])
    date_range, = facts(summary, 'date_range')
    assert '2099' not in date_range.text and 'incomplete' in date_range.text
    assert {link.document_id for link in date_range.links} == {'a', 'b'}
    assert facts(summary, 'pages')[0].count == 2
    assert 'incomplete' in facts(summary, 'pages')[0].text
    assert 'incomplete' in facts(summary, 'undated')[0].text
    assert not facts(summary, 'bates_gap') and not overlaps(summary)


@pytest.mark.parametrize('ranges', [[('ABC0001', None), ('ABC00001', None), ('ABC00003', None)],
                                    [('ABC0001', 'ABC00010'), ('ABC00020', None)]])
def test_bates_gap_uses_series_width(ranges):
    summary = build_summary('Synthetic', 'm', [with_ranges(str(i), [pair]) for i, pair in enumerate(ranges)])
    gap, = facts(summary, 'bates_gap')
    assert gap.text == ('Missing Bates: ABC00002' if len(ranges) == 3 else 'Missing Bates: ABC00011 to ABC00019')


def test_path_and_bates_tokens_do_not_widen_date_range():
    summary = build_summary('Synthetic', 'm', [source('a',
        'folder/0001-01-01/notes\nfolder\\0002-01-01\\notes\n'
        'Bates: ABC-2020-01-01\nBates: 9999-12-31\nSee ABC-2021-01-01\n2034-05-01')])
    assert facts(summary, 'date_range')[0].text == 'Mentioned ISO date range: 2034-05-01 to 2034-05-01'


def test_markdown_document_text_cannot_strike_link_or_head():
    entity = dict(entity_type='person', display_name='# fake heading', status='suggested')
    mention = dict(document_id='a', source_version_id='old', available=False)
    markdown = build_summary('keep ~~stamp~~ http://example.test visible', 'm', [source()],
                             entities=[(entity, [mention])]).to_markdown()
    html = MarkdownIt('commonmark').enable('strikethrough').render(markdown)
    assert '<s>' not in html and '<h1>fake heading' not in html
    assert 'http://example.test' not in markdown
    assert '~~stamp~~' in html and 'http://example.test' in html
    assert 'href="/matters/m/sources/token-a"' in html


def test_shared_bates_range_emits_one_fact_with_all_sources():
    summary = build_summary('Synthetic', 'm', [source(str(i), labels=['ABC0001']) for i in range(450)])
    overlap, = overlaps(summary)
    assert len(overlap.links) == 450 and overlap.count == 450


def test_page_only_locators_do_not_count_toward_bates_limit():
    item = replace(source(), locators=(SourceLocator(page=1),) * 100_001)
    summary = build_summary('Synthetic', 'm', [item])
    assert facts(summary, 'documents')[0].count == 1
    assert not overlaps(summary) and not facts(summary, 'bates_unordered')
