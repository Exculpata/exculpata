from case_intelligence.production_bates import parse_bates, bates_gaps
import pytest


@pytest.mark.parametrize('label,prefix,number,width', [('ABC-000123','ABC',123,6), ('ABC000123','ABC',123,6), ('XY_Z-09','XY_Z',9,2), ('a1','A',1,1)])
def test_parse(label, prefix, number, width):
    parsed = parse_bates(label)
    assert (parsed.prefix, parsed.number, parsed.width) == (prefix, number, width)


@pytest.mark.parametrize('label', ['', '123', 'ABC', 'ABC 123', 'ABC-12x', 'ABC-1.2', 'ABC--0001', 'ABC1-2', 'ABC' + '1'*13, None])
def test_malformed(label):
    assert parse_bates(label) is None


def test_ranges_duplicates_padding_and_mixed_prefixes():
    result = bates_gaps([
        dict(bates_start='ABC-0001', bates_end='ABC0003', link='/a'),
        dict(bates_start='ABC0003', link='/b'),
        dict(bates_start='ABC0006', link='/c'),
        dict(bates_start='XYZ99', link='/d'),
        dict(bates_start='ABC7', bates_end='XYZ8', link='/e'),
        dict(bates_start='ABC9', bates_end='ABC8', link='/f')])
    assert result['bates_missing'] == [dict(prefix='ABC', start='0004', end='0005', links=['/a','/b','/c'])]
    assert result['bates_duplicates'] == [dict(prefix='ABC', start='0003', end='0003', occurrences=2, links=['/a','/b'])]
    assert len(result['bates_invalid']) == 2


def test_large_ranges_do_not_expand_each_page():
    result = bates_gaps([dict(bates_start='A1', bates_end='A999999999999', link='/a'), dict(bates_start='A5', link='/b')])
    assert not result['bates_missing']
    assert result['bates_duplicates'][0]['start'] == '000000000005'


@pytest.mark.parametrize('count', [100, 500, 10_000])
def test_nested_ranges_bound_output_during_sweep(count):
    caps = {}
    result = bates_gaps((dict(bates_start=f'SYN{i+1:05}', bates_end=f'SYN{2*count-i:05}',
        link=f'/source/{i}') for i in range(count)), caps=caps)
    overlaps = result['bates_duplicates']
    assert len(overlaps) <= 100
    assert sum(len(row['links']) for row in overlaps) <= 2_000
    assert caps['bates_duplicates']['total'] == 2 * count - 3
    assert caps['bates_duplicates']['omitted'] == 2 * count - 3 - len(overlaps)
    assert caps['bates_support_references']['omitted'] > 0
    assert all(len(row['links']) <= 20 for row in overlaps)


def test_identity_deduplicates_normalized_ranges_but_preserves_other_sources():
    rows = [dict(bates_start='ABC-0001', link='/one', identity=('one', 1)),
            dict(bates_start='abc0001', link='/one', identity=('one', 1))]
    assert not bates_gaps(rows)['bates_duplicates']
    rows.append(dict(bates_start='ABC0001', link='/two', identity=('two', 1)))
    assert bates_gaps(rows)['bates_duplicates'][0]['occurrences'] == 2


def test_nested_range_sweep_has_linear_retained_memory():
    import tracemalloc
    tracemalloc.start()
    try:
        count = 10_000
        result = bates_gaps((dict(bates_start=f'SYN{i+1}', bates_end=f'SYN{2*count-i}',
            link=f'/source/{i}') for i in range(count)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 32 * 1024 * 1024
    assert len(result['bates_duplicates']) == 100
