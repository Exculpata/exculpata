"""Synthetic single-line metadata and export compatibility regressions for #331."""
from copy import deepcopy
import csv
import io
import json

import pytest

from case_intelligence.timeline_exports import export_timeline


SEPARATORS = pytest.mark.parametrize('separator', ['\n', '\r', '\r\n', '\u2028', '\u2029'],
                                     ids=['lf', 'cr', 'crlf', 'line', 'paragraph'])


def payload(value='Synthetic'):
    return {
        'format': 'exculpata-timeline-draft-v2',
        'notice': value, 'coverage_notice': value, 'capped': False,
        'unit_count': 1, 'total_unit_count': 1,
        'coverage_records': [{'source_id': 'source-a', 'source_name': value, 'status': 'unreadable'}],
        'entries': [{
            'date_label': value, 'precision': 'day', 'uncertainty': 'None',
            'description': value, 'conflict_group': None,
            'citations': [{'source_id': 'source-a', 'document_title': value,
                           'locator': {'label': value}, 'excerpt': value}],
        }],
        'undated': [],
    }


@SEPARATORS
@pytest.mark.parametrize('bucket', ['entries', 'undated'])
@pytest.mark.parametrize('field', ['citation_source', 'coverage_source', 'date_label',
                                  'locator', 'notice', 'coverage_notice'])
def test_markdown_metadata_stays_on_one_line(separator, bucket, field):
    data = payload()
    entry = data['entries'][0]
    citation = entry['citations'][0]
    if bucket == 'undated':
        data['undated'], data['entries'] = data['entries'], []
    value = 'x' + separator + '- injected' + separator + '# heading'
    expected = r'x - injected \# heading'
    if field == 'citation_source':
        citation['document_title'] = value
        expected = 'Source: source-a · ' + expected
    elif field == 'coverage_source':
        data['coverage_records'][0]['source_name'] = value
        expected = 'Source coverage: ' + expected + ' (source-a)'
    elif field == 'date_label':
        entry['date_label'] = value
        expected = '## ' + expected
    elif field == 'locator':
        citation['locator'] = {'label': value}
        escaped_separator = separator.encode('unicode_escape').decode().replace('\\', '\\\\')
        expected = ('Locator: {&quot;label&quot;: &quot;x' + escaped_separator +
                    '- injected' + escaped_separator + r'\# heading&quot;}')
    else:
        data[field] = value

    lines = export_timeline(data, 'markdown').splitlines()
    assert any(line.startswith(expected) for line in lines), repr(expected)
    assert not any(line.startswith(('- injected', '# heading', r'\# heading')) for line in lines)


def test_markdown_source_cannot_inject_a_list_item():
    data = payload()
    data['entries'][0]['citations'][0]['document_title'] = 'x\n- injected'
    data['coverage_records'][0]['source_name'] = 'x\n- injected'

    lines = export_timeline(data, 'markdown').splitlines()

    assert 'Source: source-a · x - injected' in lines
    assert any(line.startswith('Source coverage: x - injected (source-a)') for line in lines)
    assert not any(line.startswith('- injected') for line in lines)


def test_markdown_preserves_multiline_excerpts_and_descriptions():
    data = payload()
    value = 'First *line*\n\nSecond <line>\r\nThird\rFourth\u2028Fifth\u2029Sixth'
    entry = data['entries'][0]
    entry['description'] = value
    entry['citations'][0]['excerpt'] = value

    body = export_timeline(data, 'markdown')

    assert ('First \\*line\\*\n\nSecond &lt;line&gt;\r\nThird\rFourth\u2028Fifth\u2029Sixth\n') in body
    assert body.endswith('> First \\*line\\*\n> \n> Second &lt;line&gt;\r\n> Third\rFourth\u2028Fifth\u2029Sixth\n')


@SEPARATORS
def test_csv_and_json_are_unchanged_by_markdown_metadata_presentation(separator):
    value = 'x' + separator + '- injected'
    data = payload(value)
    original = deepcopy(data)
    csv_separator = '\n' if separator in ('\n', '\r', '\r\n') else ' '
    csv_value = 'x' + csv_separator + '- injected'
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(('date_label', 'precision', 'uncertainty', 'description', 'source', 'locator',
                     'excerpt', 'conflict_group', 'row_type', 'notice', 'coverage_notice', 'capped',
                     'unit_count', 'total_unit_count', 'coverage_status', 'format'))
    writer.writerow([''] * 8 + ['metadata', csv_value, csv_value, 'False', 1, 1, '', data['format']])
    writer.writerow([''] * 4 + ['source-a · ' + csv_value] + [''] * 3 +
                    ['coverage', '', csv_value, '', '', '', 'unreadable', data['format']])
    writer.writerow([csv_value, 'day', 'None', csv_value, 'source-a · ' + csv_value,
                     json.dumps({'label': value}), csv_value, '', 'citation'] + [''] * 6 + [data['format']])
    expected_csv = stream.getvalue()
    expected_json = json.dumps(original, ensure_ascii=False, indent=2)

    assert export_timeline(data, 'csv') == expected_csv
    assert export_timeline(data, 'json') == expected_json
    export_timeline(data, 'markdown')
    assert data == original
    assert export_timeline(data, 'csv') == expected_csv
    assert export_timeline(data, 'json') == expected_json
