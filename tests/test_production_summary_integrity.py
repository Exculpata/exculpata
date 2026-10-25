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


@pytest.mark.parametrize('label', ['ABC\u200b0002', 'ABC\u202e2000', 'ABC\t0002', '\u0410BC0002',
    '\u0410\u0412\u04210002', '\u0391\u0392\u03a30002', 'A\u034fBC0002', 'A\ufe00BC0002'])
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


def test_partial_read_retains_labels_but_excludes_dates_and_closed_page_coverage(monkeypatch):
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


def _base():
    return [source('a', labels=['ABC0001']), source('c', labels=['ABC0500'])]


@pytest.mark.parametrize('extra', [
    source('k', labels=['CONFIDENTIAL']),
    with_ranges('x', [('QQ0001', 'RR0002')]),
    with_ranges('x', [('A0010', 'A0005')]),
    partial_pdf(pages=2),
], ids=['nonnumeric', 'cross_prefix', 'reversed', 'partial_pdf'])
def test_unrelated_unresolved_input_keeps_other_series_computed(extra):
    s = build_summary('Synthetic', 'm', _base() + [extra])
    assert [g.text for g in facts(s, 'bates_gap')] == ['Missing Bates: ABC0002 to ABC0499']


def test_blocked_series_is_stated_not_silent():
    s = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']), source('b', labels=['ABC0010']),
                                        with_ranges('x', [('ABC0020', 'ABC0005')])])
    assert not facts(s, 'bates_gap')
    assert any('ABC' in f.text and 'not computed' in f.text for f in facts(s, 'bates_series'))
    assert 'not computed' in s.to_markdown()


def test_incomplete_document_labels_stay_in_series():
    s = build_summary('Synthetic', 'm', [source('a', labels=['SYN0001']), partial_pdf(pages=2),
                                        source('c', labels=['SYN0003'])])
    assert not facts(s, 'bates_gap')


def test_incomplete_document_duplicate_label_is_reported():
    s = build_summary('Synthetic', 'm', [partial_pdf(pages=2), source('r', labels=['SYN0002'])])
    assert {l.document_id for l in overlaps(s)[0].links} == {'partial', 'r'}


def test_unlabeled_and_start_only_documents_are_surfaced_beside_gaps():
    s = build_summary('Synthetic', 'm', [source('a', pages=5, labels=['ABC0001']),
                                        source('b', labels=['ABC0006']), source('u'),
                                        source('f', state='failed', message='x')])
    cov = facts(s, 'bates_coverage')
    assert cov and {l.document_id for f in cov for l in f.links} >= {'u', 'f'}
    assert all('may hold unrecorded' in g.text for g in facts(s, 'bates_gap'))


@pytest.mark.parametrize('label', ['abc0001', 'ABC-0001', '\u0410\u0412\u0421\u00a00001'])
def test_lookalike_series_is_flagged(label):
    s = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']), source('b', labels=[label]),
                                        source('c', labels=['ABC0003'])])
    assert facts(s, 'bates_series') and 'differ only' in s.to_markdown() or facts(s, 'bates_unordered')


def test_unordered_label_does_not_silently_hide_other_gaps():
    s = build_summary('Synthetic', 'm', _base() + [source('u', labels=['CONFIDENTIAL'])])
    assert [g.text for g in facts(s, 'bates_gap')] == ['Missing Bates: ABC0002 to ABC0499']
    assert any(f.text == 'Bates series ABC: computed' for f in facts(s, 'bates_series'))
    assert facts(s, 'bates_unordered')


def test_unordered_label_does_not_silently_hide_other_gaps_partial_pdf():
    s = build_summary('Synthetic', 'm', _base() + [partial_pdf(pages=2)])
    assert [g.text for g in facts(s, 'bates_gap')] == ['Missing Bates: ABC0002 to ABC0499']
    assert any(f.text == 'Bates series ABC: computed' for f in facts(s, 'bates_series'))
    assert any('incomplete' in f.text for f in facts(s, 'readability'))


def test_unordered_label_does_not_silently_hide_other_gaps_cross_prefix():
    s = build_summary('Synthetic', 'm', _base() + [with_ranges('x', [('QQ0001', 'RR0002')]),
        source('q', labels=['QQ0004']), source('r', labels=['RR0004'])])
    assert [g.text for g in facts(s, 'bates_gap')] == ['Missing Bates: ABC0002 to ABC0499']
    for prefix in ('QQ', 'RR'):
        assert any(f'Bates series {prefix}: not computed' in f.text and
                   'x' in {l.document_id for l in f.links} for f in facts(s, 'bates_series'))


@pytest.mark.parametrize('label', ['ABC\u200b0002', '\u0410\u0412\u04210002'])
def test_unscopable_label_blocks_all_series_with_cited_reason(label):
    s = build_summary('Synthetic', 'm', _base() + [source('u', labels=[label]),
        source('q1', labels=['QQ0001']), source('q3', labels=['QQ0003'])])
    status = facts(s, 'bates_series')
    global_status = [f for f in status if f.text.startswith('Bates series ALL:')]
    assert len(global_status) == 1 and 'not computed' in global_status[0].text
    assert {l.document_id for l in global_status[0].links} == {'u'}
    for prefix in ('ABC', 'QQ'):
        assert any(f'Bates series {prefix}: not computed' in f.text and
                   'u' in {l.document_id for l in f.links} for f in status)
    assert not facts(s, 'bates_gap')


def test_start_only_coverage_counts_documents_and_retains_page_uncertainty():
    a = source('a', pages=5, labels=['ABC0001', 'ABC0002'])
    s = build_summary('Synthetic', 'm', [a, source('b', labels=['ABC0006'])])
    cov, = facts(s, 'bates_coverage')
    assert cov.text == 'Documents with a start label only: 1' and cov.count == 1
    assert {l.document_id for l in cov.links} == {'a'}
    assert '1 documents have a start label only' in facts(s, 'bates_series')[0].text
    gap, = facts(s, 'bates_gap')
    assert gap.count == 3 and 'may hold unrecorded' in gap.text
    assert {l.document_id for l in gap.links} == {'a', 'b'}


def test_unlabeled_coverage_includes_page_only_failed_and_ocr_documents():
    page_only = replace(source('p'), locators=(SourceLocator(page=1),))
    s = build_summary('Synthetic', 'm', [page_only, source('u'),
        source('f', state='failed'), source('o', state='needs_ocr')])
    cov, = facts(s, 'bates_coverage')
    assert cov.text == 'Documents without a Bates locator: 4' and cov.count == 4
    assert {l.document_id for l in cov.links} == {'p', 'u', 'f', 'o'}


def test_gap_facts_count_toward_bates_finding_limit(monkeypatch):
    monkeypatch.setattr('case_intelligence.production_summary._BATES_FACT_LIMIT', 3, raising=False)
    with pytest.raises(OverflowError, match='Bates finding limit'):
        build_summary('Synthetic', 'm', [source(str(i), labels=[f'ABC{i:04d}']) for i in (1, 3, 5, 7)])


def test_start_only_on_shared_gap_bracket_is_cited():
    s = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        source('b', pages=5, labels=['ABC0001']), source('c', labels=['ABC0006'])])
    gap, = facts(s, 'bates_gap')
    assert 'may hold unrecorded' in gap.text
    assert 'b' in {l.document_id for l in gap.links}


def test_global_blocker_does_not_add_unrelated_start_only_caveat():
    s = build_summary('Synthetic', 'm', _base() + [source('u', pages=5, labels=['ABC\u200b0002'])])
    status, = [f for f in facts(s, 'bates_series') if f.text.startswith('Bates series ABC:')]
    assert 'not computed' in status.text and 'start label only' not in status.text


@pytest.mark.parametrize('kind', ['bates_series', 'bates_coverage', 'bates_unordered', 'bates_overlap'])
def test_every_bates_fact_uses_finding_limit(monkeypatch, kind):
    sources = {'bates_series': [source('a', labels=['ABC0001'])],
               'bates_coverage': [source('a')],
               'bates_unordered': [source('a', labels=['CONFIDENTIAL'])],
               'bates_overlap': [source('a', labels=['CONFIDENTIAL']),
                                 source('b', labels=['CONFIDENTIAL'])]}[kind]
    s = build_summary('Synthetic', 'm', sources)
    assert facts(s, kind)
    limit = len([f for f in s.facts if f.kind.startswith('bates_')]) - 1
    monkeypatch.setattr('case_intelligence.production_summary._BATES_FACT_LIMIT', limit, raising=False)
    with pytest.raises(OverflowError, match='Bates finding limit'):
        build_summary('Synthetic', 'm', sources)


def test_empty_gap_list_does_not_claim_completeness():
    s = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001'])])
    assert not facts(s, 'bates_gap')
    assert 'An empty gap list does not establish completeness.' in s.to_markdown()


@pytest.mark.parametrize('state', ['ready', 'failed', 'needs_ocr'])
def test_unlabeled_documents_change_coverage_only(state):
    before = build_summary('Synthetic', 'm', _base())
    after = build_summary('Synthetic', 'm', _base() + [source('u', state=state)])
    for kind in ('bates_series', 'bates_gap'):
        assert facts(after, kind) == facts(before, kind)
    coverage, = facts(after, 'bates_coverage')
    assert coverage.text == 'Documents without a Bates locator: 1'
    assert {link.document_id for link in coverage.links} == {'u'}


@pytest.mark.parametrize('label', ['ABC0002A', 'ABC0002.001', 'ABC0002-001'])
def test_suffixed_label_does_not_yield_unqualified_gap_at_its_base(label):
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        source('b', labels=[label]), source('c', labels=['ABC0003'])])
    assert '- Missing Bates: ABC0002\n' not in summary.to_markdown()
    assert not facts(summary, 'bates_gap')
    suffix, = facts(summary, 'bates_suffix')
    assert label in suffix.text and 'ABC0002' in suffix.text
    assert {link.document_id for link in suffix.links} == {'b'}
    assert 'Bates series ABC: computed' in summary.to_markdown()


@pytest.mark.parametrize('blocker', [
    with_ranges('x', [('ABC0005', 'XYZ0002')]),
    source('x', labels=['\u0410\u0412\u04210002']),
], ids=['cross-prefix', 'global'])
def test_blocked_series_retains_exact_duplicate_overlap(blocker):
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        source('b', labels=['ABC0001']), blocker])
    overlap, = overlaps(summary)
    assert 'ABC0001' in overlap.text
    assert {link.document_id for link in overlap.links} == {'a', 'b'}
    assert 'Bates series ABC: not computed' in summary.to_markdown()
    assert not facts(summary, 'bates_gap')


@pytest.mark.parametrize('prefix,lookalike', [('ABC', '\u1d00\u0299\u1d04'), ('ibc', '\u0131bc')])
def test_small_caps_and_dotless_i_series_are_related_but_separate(prefix, lookalike):
    summary = build_summary('Synthetic', 'm', [source('a', labels=[prefix + '0001']),
        source('b', labels=[lookalike + '0002']), source('c', labels=[prefix + '0003'])])
    related, = [f for f in facts(summary, 'bates_series') if 'differ only' in f.text]
    assert prefix in related.text and lookalike in related.text
    assert {link.document_id for link in related.links} == {'a', 'b', 'c'}
    for value in (prefix, lookalike):
        assert f'Bates series {value}: computed' in summary.to_markdown()
    assert [f.text for f in facts(summary, 'bates_gap')] == ['Missing Bates: ' + prefix + '0002']


@pytest.mark.parametrize('label', ['ABC0002A', 'ABC0002.001', 'ABC0002-001'])
def test_real_gap_remains_with_suffixed_labels_elsewhere(label):
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        source('b', labels=[label]), source('c', labels=['ABC0005'])])
    gap, = facts(summary, 'bates_gap')
    assert gap.text == 'Missing Bates: ABC0003 to ABC0004' and gap.count == 2
    assert {link.document_id for link in gap.links} == {'b', 'c'}


@pytest.mark.parametrize('suffix', ['A', '.001', '-001'])
def test_multi_prefix_gap_oracle_with_suffixed_labels(suffix):
    from random import Random
    random = Random(20261025)
    for scenario in range(100):
        sources, expected = [], {}
        for prefix in ('ABC', 'XYZ', 'QQ'):
            covered = {1, 20}
            sources.extend([source(prefix + '-first', labels=[prefix + '0001']),
                            source(prefix + '-last', labels=[prefix + '0020'])])
            for number in random.sample(range(2, 20), 6):
                covered.add(number)
                sources.append(source(f'{prefix}-{number}', labels=[f'{prefix}{number:04d}{suffix}']))
            expected[prefix] = set(range(1, 21)) - covered
        sources.extend([source('u', labels=['CONFIDENTIAL']),
                        with_ranges('blocked', [('RR0001', 'SS0002')]), partial_pdf(pages=2)])
        summary = build_summary('Synthetic', 'm', sources)
        actual = {prefix: set() for prefix in expected}
        for gap in facts(summary, 'bates_gap'):
            start, _, end = gap.text.removeprefix('Missing Bates: ').partition(' to ')
            prefix = start[:3] if start.startswith(('ABC', 'XYZ')) else start[:2]
            lower, upper = int(start[len(prefix):]), int((end or start)[len(prefix):])
            actual[prefix].update(range(lower, upper + 1))
            assert gap.count == upper - lower + 1
        assert actual == expected, (scenario, suffix, actual, expected)


@pytest.mark.parametrize('suffix,other', [('A', 'B'), ('.001', '.002'), ('-001', '-002')])
def test_suffixed_coverage_keeps_exact_duplicates_without_inventing_base_overlap(suffix, other):
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        source('b', labels=['ABC0002' + suffix]), source('c', labels=['ABC0002' + other]),
        source('d', labels=['ABC0002' + suffix]), source('base', labels=['ABC0002']),
        source('last', labels=['ABC0005'])])
    overlap, = overlaps(summary)
    assert 'ABC0002' + suffix in overlap.text
    assert {link.document_id for link in overlap.links} == {'b', 'd'}
    assert len(facts(summary, 'bates_suffix')) == 3
    assert [f.text for f in facts(summary, 'bates_gap')] == ['Missing Bates: ABC0003 to ABC0004']


@pytest.mark.parametrize('start,end', [('ABC0002A', 'ABC0002C'),
    ('ABC0002.001', 'ABC0002.003'), ('ABC0002-001', 'ABC0002-003')])
def test_same_base_suffixed_range_retains_cited_coverage(start, end):
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        with_ranges('b', [(start, end)]), source('c', labels=['ABC0005'])])
    gap, = facts(summary, 'bates_gap')
    assert gap.text == 'Missing Bates: ABC0003 to ABC0004'
    assert {link.document_id for link in gap.links} == {'b', 'c'}
    suffix, = facts(summary, 'bates_suffix')
    assert start in suffix.text and end in suffix.text
    assert {link.document_id for link in suffix.links} == {'b'}


def test_suffixed_range_across_bases_is_explicitly_not_computed():
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        with_ranges('b', [('ABC0002A', 'ABC0004C')]), source('c', labels=['ABC0005'])])
    assert not facts(summary, 'bates_gap')
    status, = facts(summary, 'bates_series')
    assert 'ABC: not computed' in status.text and 'suffixed' in status.text
    assert 'b' in {link.document_id for link in status.links}


def test_suffixed_fact_uses_finding_limit(monkeypatch):
    sources = [source('a', labels=['ABC0001', 'ABC0002A'])]
    summary = build_summary('Synthetic', 'm', sources)
    assert facts(summary, 'bates_suffix')
    limit = len([f for f in summary.facts if f.kind.startswith('bates_')]) - 1
    monkeypatch.setattr('case_intelligence.production_summary._BATES_FACT_LIMIT', limit)
    with pytest.raises(OverflowError, match='Bates finding limit'):
        build_summary('Synthetic', 'm', sources)


def test_round_five_markdown_artifact_preserves_gaps_and_source_links(tmp_path):
    summary = build_summary('Synthetic', 'm', [source('a', labels=['ABC0001']),
        source('b', labels=['ABC0002A']), source('c', labels=['ABC0005']),
        source('d', labels=['ABC0001']), source('lookalike', labels=['\u1d00\u0299\u1d040002'])])
    artifact = tmp_path / 'summary.md'
    # fixture-write: Persist a synthetic production summary to verify the exported artifact.
    artifact.write_text(summary.to_markdown(), encoding='utf-8')
    markdown = artifact.read_text(encoding='utf-8')
    assert 'Missing Bates: ABC0003 to ABC0004' in markdown
    assert 'Missing Bates: ABC0002' not in markdown
    assert 'Suffixed Bates labels: ABC0002A (base coverage: ABC0002)' in markdown
    assert 'differ only' in markdown and 'Overlapping Bates labels: ABC0001' in markdown
    rendered = MarkdownIt().render(markdown)
    for key in ('a', 'b', 'c', 'd', 'lookalike'):
        assert f'href="/matters/m/sources/token-{key}"' in rendered
