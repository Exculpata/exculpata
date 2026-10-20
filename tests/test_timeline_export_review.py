"""Synthetic regressions for the timeline export review batch."""
import csv
from dataclasses import replace
import io
import json
import unicodedata

import pytest

from case_intelligence.timeline_builder import build_timeline
from case_intelligence.timeline_coverage import CoverageLimits, collect_timeline_units
from case_intelligence.timeline_exports import export_timeline
from tests.test_timeline_builder import unit
from tests.test_timeline_coverage import document
from tests.test_timeline_routes import timeline_client
from tests.test_matter_notebook import WEB_ACTOR


@pytest.mark.parametrize('prefix', [chr(i) for i in range(32)] + [
    '\ufeff', '\u200b', '\u200c', '\u200d', '\u2060', '|', '%', '\uff1d', '\uff0b',
    '\uff0d', '\uff20', '\u2212', ' ', ' \t\r\n'])
def test_csv_presentation_precedes_formula_guard(prefix):
    payload = build_timeline([unit('Alex Example delivered the parcel on 2034-04-18.')]).payload()
    value = prefix + '=Alex Example delivered the parcel on 2034-04-18.'
    payload['entries'][0]['description'] = value
    payload['entries'][0]['citations'][0]['excerpt'] = value
    payload['coverage_notice'] = value
    rows = list(csv.DictReader(io.StringIO(export_timeline(payload, 'csv'))))
    metadata, citation = rows
    for text in (metadata['coverage_notice'], citation['description'], citation['excerpt']):
        assert text.startswith("'")
        assert not any(unicodedata.category(c) in {'Cc', 'Cf'} for c in text)
        assert not text[1:].startswith((' ', '\t', '\r', '\n'))
    assert citation['description'] == citation['excerpt']


def test_sub_source_excerpt_and_description_are_both_guarded():
    payload = build_timeline([unit('\x1a=Alex Example delivered the parcel on 2034-04-18.')]).payload()
    citation = list(csv.DictReader(io.StringIO(export_timeline(payload, 'csv'))))[1]
    assert citation['description'].startswith("'=Alex")
    assert citation['excerpt'].startswith("'=Alex")


@pytest.mark.parametrize('separator', ['\u2028', '\u2029'])
def test_csv_unicode_separators_are_presented_as_spaces(separator):
    payload = build_timeline([unit('2034-04-18.')]).payload()
    value = 'Synthetic' + separator + '=text'
    payload['entries'][0]['description'] = value
    payload['entries'][0]['citations'][0]['excerpt'] = value
    payload['coverage_notice'] = value
    body = export_timeline(payload, 'csv')
    assert separator not in body
    assert 'Synthetic =text' in body


def test_csv_bare_whitespace_has_same_presentation_for_excerpt_and_description():
    payload = build_timeline([unit(' \t  Alex Example delivered the parcel on 2034-04-18.')]).payload()
    citation = list(csv.DictReader(io.StringIO(export_timeline(payload, 'csv'))))[1]
    assert citation['description'] == citation['excerpt']
    assert citation['excerpt'].startswith('Alex')


@pytest.mark.parametrize('field', ['notice', 'coverage_notice'])
def test_markdown_notices_are_escaped(field):
    payload = build_timeline([]).payload()
    payload[field] = '<script>alert("synthetic")</script>\n=HYPERLINK("synthetic")'
    body = export_timeline(payload, 'markdown')
    assert '<script>' not in body and '&lt;script&gt;' in body
    assert '\n=HYPERLINK' not in body and '\\=HYPERLINK' in body


def test_timeline_export_schema_is_v2():
    assert build_timeline([]).payload()['format'] == 'exculpata-timeline-draft-v2'


@pytest.mark.parametrize('kind', ['units', 'documents'])
def test_exact_coverage_ceiling_is_complete(kind):
    sources = ([document([{'number': i+1, 'text': 'Synthetic'} for i in range(3)])]
               if kind == 'units' else [document([{'number': 1, 'text': 'Synthetic'}]) for _ in range(1000)])
    limits = replace(CoverageLimits(), units=3) if kind == 'units' else CoverageLimits()
    units, total, complete = collect_timeline_units(sources, lambda d, u, n: u, limits=limits)
    assert complete and total == len(units) == (3 if kind == 'units' else 1000)


@pytest.mark.parametrize('format_name', ['csv', 'markdown', 'json'])
def test_export_bytes_do_not_depend_on_clock(timeline_client, monkeypatch, format_name):
    from case_intelligence import timeline_routes
    client, slug = timeline_client
    original = timeline_routes.collect_timeline_units
    monkeypatch.setattr(timeline_routes, 'collect_timeline_units',
                        lambda *a, **kw: original(*a, **kw, clock=lambda: 0))
    url = f'/matters/{slug}/timeline/draft/export?format={format_name}'
    frozen = client.get(url)
    ticks = iter(range(0, 10000000, 10))
    monkeypatch.setattr(timeline_routes, 'collect_timeline_units',
                        lambda *a, **kw: original(*a, **kw, clock=lambda: next(ticks)))
    slow = client.get(url)
    assert frozen.status_code == slow.status_code == 200
    assert frozen.content == slow.content


@pytest.mark.parametrize('failure', ['corrupt', 'oversized_inline'])
def test_corrupt_middle_source_is_named_and_later_sources_remain(timeline_client, monkeypatch, failure):
    client, slug = timeline_client
    bench = client.app.state.workbench
    store = bench.source_store(bench.matter(slug, WEB_ACTOR))
    first, middle, last = list(store.ready_documents())
    first = replace(first, units=[{'number': 1, 'text': 'First synthetic event on 2034-04-18.'}], units_file='')
    middle = replace(middle, display_name='Unreadable synthetic middle.txt', units=[], units_file='e' * 32 + '.json',
                     _units_iterator=store._iter_units)
    (store.derived / middle.units_file).write_text('{broken')
    if failure == 'oversized_inline':
        middle = replace(middle, units=[{'number': 1, 'text': 'x' * 70000}], units_file='')
    last = replace(last, units=[{'number': 1, 'text': 'Later synthetic event on 2034-04-20.'}], units_file='')
    monkeypatch.setattr(store, 'ready_documents', lambda: (first, middle, last))
    prefix = f'/matters/{slug}/timeline/draft'
    payload = client.get(prefix + '/export').json()
    assert any('Later synthetic event' in e['description'] for e in payload['entries'])
    assert payload['total_unit_count'] is None
    assert payload['coverage_records'] == [{'source_id': middle.document_id, 'source_name': middle.display_name,
                                            'status': 'unreadable'}]
    assert middle.display_name in payload['coverage_notice']
    for endpoint in ('', '/export?format=csv', '/export?format=markdown'):
        response = client.get(prefix + endpoint)
        assert response.status_code == 200
        assert middle.display_name in response.text
    csv_rows = list(csv.DictReader(io.StringIO(client.get(prefix + '/export?format=csv').text)))
    assert any(r['row_type'] == 'coverage' and r['coverage_status'] == 'unreadable' for r in csv_rows)
