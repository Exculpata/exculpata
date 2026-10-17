"""Synthetic limits checked before retaining, decoding, or traversing more input."""
from dataclasses import replace
import io
import json
from types import SimpleNamespace

import pytest

from case_intelligence.pilot_uploads import PilotDocument, PilotUnit
from case_intelligence.timeline_coverage import CoverageLimits, collect_timeline_units
from case_intelligence.unit_stream import iter_unit_records


def document(rows=(), stream=None):
    source = PilotDocument('a' * 32, 'Synthetic.txt', '', 'text/plain', 0, 'ready', '', list(rows))
    if stream is not None:
        source.units_file = 'a' * 32 + '.json'
        source._units_iterator = lambda name, **kwargs: (
            PilotUnit(**row) for row in iter_unit_records(stream, **kwargs))
    return source


def collect(sources, **kwargs):
    return collect_timeline_units(sources, lambda doc, unit, ordinal: unit, **kwargs)


def test_unit_and_document_limits_do_not_pull_an_unbounded_tail():
    pulled = []
    def endless(**kwargs):
        while True:
            pulled.append(1)
            yield PilotUnit(len(pulled), 'Synthetic')
    units, total, complete = collect([SimpleNamespace(iter_parsed_units=endless)],
                                     limits=replace(CoverageLimits(), units=3))
    assert len(pulled) == 3 and units == []
    assert total is None and not complete
    opened = []
    def sources():
        while True:
            opened.append(1)
            yield document()
    assert collect(sources(), limits=replace(CoverageLimits(), documents=2)) == ([], None, False)
    assert len(opened) == 2


@pytest.mark.parametrize('kind', ['input', 'record', 'deadline'])
def test_limits_stop_serialized_reads_before_large_decode(monkeypatch, kind):
    now = [0]
    class Reads(io.StringIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            value = super().read(size)
            if kind == 'deadline':
                now[0] = 3
            return value
    raw = (' ' * 200000 if kind == 'input' else
           '{"version":1,"units":[{"number":1,"text":"' + 'x' * 200000 + '"}]}')
    stream = Reads(raw)
    original = json.JSONDecoder.raw_decode
    def decode(self, value, *args, **kwargs):
        assert kind == 'record' and len(value) < 1024
        return original(self, value, *args, **kwargs)
    monkeypatch.setattr(json.JSONDecoder, 'raw_decode', decode)
    limits = replace(CoverageLimits(), input_chars=1024 if kind == 'input' else 500000,
                     record_chars=1024)
    assert collect([document(stream=stream)], limits=limits, clock=lambda: now[0]) == ([], None, False)
    assert stream.tell() == 65536


@pytest.mark.parametrize('limits', [replace(CoverageLimits(), text_chars=10),
                                   replace(CoverageLimits(), record_chars=10)])
def test_inline_text_is_bounded_before_candidate_construction(limits):
    source = document([{'number': 1, 'text': 'x' * 11}])
    assert collect_timeline_units([source], lambda *args: pytest.fail('oversized candidate'),
                                  limits=limits) == ([], None, False)


def test_corrupt_container_discards_its_prefix_but_keeps_prior_sources():
    first = document([{'number': 1, 'text': 'Synthetic prior source'}])
    corrupt = document(stream=io.StringIO(
        '{"version":2,"units":[{"number":1,"text":"Synthetic invalid source"}]}'))
    units, total, complete = collect([first, corrupt])
    assert [unit.text for unit in units] == ['Synthetic prior source']
    assert total is None and not complete


def test_exact_count_requires_validated_end_of_all_sources():
    source = document(stream=io.StringIO('{"version":1,"units":[{"number":1,"text":"Synthetic"}]}'))
    units, total, complete = collect([source, document()])
    assert len(units) == total == 1 and complete

@pytest.mark.parametrize('footer', ['"version":2}', 'broken'])
def test_passage_limit_discards_source_before_invalid_footer(footer):
    first = document([{'number': 1, 'text': 'Synthetic validated source'}])
    tail = document(stream=io.StringIO(
        '{"units":[{"number":1,"text":"2034-04-18"},'
        '{"number":2,"text":"2034-04-19"}],' + footer))
    units, total, complete = collect([first, tail], limits=replace(CoverageLimits(), units=3))
    assert [unit.text for unit in units] == ['Synthetic validated source']
    assert total is None and not complete

@pytest.mark.parametrize('kind', ['input', 'deadline'])
def test_read_limits_discard_source_before_unvalidated_footer(kind):
    now = [0]
    reads = []
    class Reads(io.StringIO):
        def read(self, count=-1):
            reads.append(1)
            if kind == 'deadline' and len(reads) == 2:
                now[0] = 3
            return super().read(count)
    first = document([{'number': 1, 'text': 'Synthetic validated source'}])
    tail = document(stream=Reads('{"units":[{"number":1,"text":"2034-04-18"}]'
                                 + ' ' * 100000 + ',"version":2}'))
    limits = replace(CoverageLimits(), input_chars=65536 if kind == 'input' else 200000)
    units, total, complete = collect([first, tail], limits=limits, clock=lambda: now[0])
    assert len(reads) == 2
    assert [unit.text for unit in units] == ['Synthetic validated source']
    assert total is None and not complete
