"""Synthetic single-line metadata and export compatibility regressions for #331."""
from copy import deepcopy
import csv
import io
import json

import pytest
from markdown_it import MarkdownIt

from case_intelligence.timeline_exports import export_timeline, md_block_quoted, md_inline


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
        expected = ('Locator: {"label": "x' + escaped_separator +
                    '- injected' + escaped_separator + r'\# heading"}')
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

    quoted = ('> First \\*line\\*\n> \n> Second &lt;line&gt;\n'
              '> Third\n> Fourth\n> Fifth\n> Sixth\n')
    assert body.count(quoted) == 2
    assert body.endswith(quoted)


def string_leaf_paths(value, path=()):
    if isinstance(value, str):
        yield path
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from string_leaf_paths(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from string_leaf_paths(child, path + (index,))


PAYLOAD_FIELDS = list(string_leaf_paths(payload()))
NOT_RENDERED = {
    ('format',),  # Serialization schema identifier, omitted from Markdown.
    ('entries', 0, 'precision'),  # Markdown presents date_label and uncertainty instead.
}
RENDERED_FIELDS = [
    ('notice',), ('coverage_notice',), ('capped',),
    ('coverage_records', 0, 'source_id'), ('coverage_records', 0, 'source_name'),
    ('coverage_records', 0, 'status'),
    ('entries', 0, 'date_label'),
    ('entries', 0, 'uncertainty'), ('entries', 0, 'conflict_group'),
    ('entries', 0, 'description'), ('entries', 0, 'citations', 0, 'source_id'),
    ('entries', 0, 'citations', 0, 'document_title'),
    ('entries', 0, 'citations', 0, 'locator', 'label'),
    ('entries', 0, 'citations', 0, 'excerpt'),
]
PAYLOAD_SUFFIXES = ['\n', '\r', '\r\n', '\u2028', '\u2029', '\n---', '\n===',
                    '\n- ', '\n1. ', '\n\n    code', '\n[a]: http://x',
                    '\u0085', '\x0b', '\x0c', '\n```', '\n~~~', '\n<div>x</div>', '\n+ ', '\\']
FIELDS = pytest.mark.parametrize('path', RENDERED_FIELDS,
                                ids=['.'.join(map(str, path)) for path in RENDERED_FIELDS])
SUFFIXES = pytest.mark.parametrize('suffix', PAYLOAD_SUFFIXES,
                                  ids=['lf', 'cr', 'crlf', 'line', 'paragraph', 'dash', 'equals',
                                       'bullet', 'ordered', 'code', 'reference',
                                       'nel', 'vt', 'ff', 'backtick-fence', 'tilde-fence',
                                       'html', 'plus', 'backslash'])


def test_every_string_leaf_has_explicit_markdown_coverage():
    leaves = set(PAYLOAD_FIELDS)
    covered = set(RENDERED_FIELDS)
    assert not leaves - covered - NOT_RENDERED, 'Add new string leaves to RENDERED_FIELDS or NOT_RENDERED'
    assert NOT_RENDERED <= leaves
    assert not covered & NOT_RENDERED


def set_field(data, path, value):
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def markdown_blocks(data):
    env = {}
    tokens = MarkdownIt('commonmark').parse(export_timeline(data, 'markdown'), env)
    assert not env.get('references')
    blocks = []
    depth = 0
    for token in tokens:
        if depth == 0:
            blocks.append([])
        blocks[-1].append(token)
        depth += token.nesting
    return blocks


def block_shape(block):
    return [(token.type, token.tag, token.nesting) for token in block
            if token.type != 'inline' and not (
                block[0].type == 'blockquote_open' and token.type.startswith('paragraph_'))]


def block_text(block):
    return [(token.content, [(child.type, child.content) for child in token.children or []])
            for token in block if token.type == 'inline']


@FIELDS
@pytest.mark.parametrize('bucket', ['entries', 'undated'])
def test_quotes_and_apostrophes_stay_literal_in_every_rendered_field(path, bucket):
    data = payload()
    value = 'O\'Brien & "Co"'
    set_field(data, path, value)
    if bucket == 'undated':
        data['undated'], data['entries'] = data['entries'], []
    expected = json.dumps({'label': value}) if 'locator' in path else value
    escaped = expected.replace('&', '&amp;').replace('\\', '\\\\')

    body = export_timeline(data, 'markdown')

    assert escaped in body
    assert '&#x27;' not in body and '&quot;' not in body
    assert any(expected in child.content for block in markdown_blocks(data)
               for token in block for child in token.children or [] if child.type == 'text')


@pytest.mark.parametrize('helper', [md_inline, md_block_quoted])
@pytest.mark.parametrize(('value', 'expected'), [(None, ''), (0, '0'), (42, '42')])
def test_markdown_helpers_coerce_non_strings(helper, value, expected):
    assert helper(value) == ('> ' if helper is md_block_quoted else '') + expected


@FIELDS
@pytest.mark.parametrize('bucket', ['entries', 'undated'])
@pytest.mark.parametrize('value', [None, 0, 42])
def test_markdown_export_accepts_non_string_fields(path, bucket, value):
    data = payload()
    set_field(data, path, value)
    if bucket == 'undated':
        data['undated'], data['entries'] = data['entries'], []

    body = export_timeline(data, 'markdown')

    assert body.startswith('# Draft timeline\n')
    if value == 42:
        assert '42' in body


def test_source_coverage_uses_em_dash():
    assert 'Source coverage: Synthetic (source-a) — unreadable\n' in export_timeline(payload(), 'markdown')


@FIELDS
@SUFFIXES
@pytest.mark.parametrize('bucket', ['entries', 'undated'])
def test_payload_cannot_escape_its_markdown_slot(path, suffix, bucket):
    baseline = payload()
    set_field(baseline, path, 'SlotMarker')
    data = deepcopy(baseline)
    set_field(data, path, 'SlotMarker' + suffix)
    if bucket == 'undated':
        for item in (baseline, data):
            item['undated'], item['entries'] = item['entries'], []

    expected = markdown_blocks(baseline)
    actual = markdown_blocks(data)

    assert any('SlotMarker' in token.content for block in expected for token in block)
    assert [block_shape(block) for block in actual] == [block_shape(block) for block in expected]
    for old, new in zip(expected, actual):
        if not any('SlotMarker' in token.content for token in old):
            assert block_text(new) == block_text(old)
        else:
            assert old[0].type in {'paragraph_open', 'blockquote_open', 'heading_open'}
            assert any('SlotMarker' in token.content for token in new)
            if path[-1] in {'description', 'excerpt'}:
                assert new[0].type == 'blockquote_open'


@FIELDS
def test_single_line_fields_cannot_add_hard_breaks(path):
    if path[-1] in {'description', 'excerpt'}:
        return
    data = payload()
    set_field(data, path, 'SlotMarker  \t ')

    body = export_timeline(data, 'markdown')
    assert not any(line.endswith((' ', '\t')) for line in body.splitlines() if line)
    assert not any(child.type == 'hardbreak' for block in markdown_blocks(data)
                   for token in block for child in token.children or [])


@FIELDS
@pytest.mark.parametrize('value', ['- SlotMarker', '+ SlotMarker', '1. SlotMarker',
                                  '    SlotMarker', '\tSlotMarker', '```SlotMarker',
                                  '# SlotMarker', '[a]: http://x'])
def test_payload_block_prefixes_stay_literal(path, value):
    baseline = payload()
    data = deepcopy(baseline)
    set_field(data, path, value)

    assert [block_shape(block) for block in markdown_blocks(data)] == [
        block_shape(block) for block in markdown_blocks(baseline)]


@SEPARATORS
@pytest.mark.parametrize('field', ['description', 'excerpt'])
def test_multiline_separators_cannot_release_list_items(separator, field):
    data = payload()
    path = (('entries', 0, 'description') if field == 'description' else
            ('entries', 0, 'citations', 0, 'excerpt'))
    set_field(data, path, 'SlotMarker' + separator + '- INJ')
    blocks = markdown_blocks(data)

    assert [block_shape(block) for block in blocks] == [
        block_shape(block) for block in markdown_blocks(payload())]
    injected = [block for block in blocks if any('INJ' in token.content for token in block)]
    assert len(injected) == 1
    assert injected[0][0].type == 'blockquote_open'


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


def test_csv_and_json_bytes_preserve_all_payload_suffixes():
    value = 'O\'Brien & "Co"' + ''.join(PAYLOAD_SUFFIXES)
    data = payload()
    for path in PAYLOAD_FIELDS + [('entries', 0, 'conflict_group')]:
        set_field(data, path, value)
    original = deepcopy(data)
    csv_value = value.replace('\r\n', '\n').replace('\r', '\n').replace('\u2028', ' ').replace('\u2029', ' ')
    for control in ('\u0085', '\x0b', '\x0c'):
        csv_value = csv_value.replace(control, ' ')
    csv_value = '\n'.join(line.rstrip() for line in csv_value.split('\n')).strip()
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(('date_label', 'precision', 'uncertainty', 'description', 'source', 'locator',
                     'excerpt', 'conflict_group', 'row_type', 'notice', 'coverage_notice', 'capped',
                     'unit_count', 'total_unit_count', 'coverage_status', 'format'))
    writer.writerow([''] * 8 + ['metadata', csv_value, csv_value, 'False', 1, 1, '', value])
    writer.writerow([''] * 4 + [csv_value + ' · ' + csv_value] + [''] * 3 +
                    ['coverage', '', csv_value, '', '', '', value, value])
    writer.writerow([csv_value, csv_value, csv_value, csv_value, csv_value + ' · ' + csv_value,
                     json.dumps({'label': value}), csv_value, csv_value, 'citation'] + [''] * 6 + [value])
    expected_csv = stream.getvalue().encode('utf-8')
    expected_json = json.dumps(original, ensure_ascii=False, indent=2).encode('utf-8')

    assert export_timeline(data, 'csv').encode('utf-8') == expected_csv
    assert export_timeline(data, 'json').encode('utf-8') == expected_json
    export_timeline(data, 'markdown')
    assert data == original
    assert export_timeline(data, 'csv').encode('utf-8') == expected_csv
    assert export_timeline(data, 'json').encode('utf-8') == expected_json
