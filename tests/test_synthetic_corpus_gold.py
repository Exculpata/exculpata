"""Synthetic generator acceptance, including real artifact and eval boundaries."""
import csv
import gc
import hashlib
import json
import os
import subprocess
import sys
import threading
import tracemalloc
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from pypdf import PdfReader

from case_intelligence import fraud_case_gold as fraud
from case_intelligence.synthetic_corpus_gold import SCALES, generate
from case_intelligence import defense_eval as ev


def rows(root, name):
    return [json.loads(line) for line in (root / name).read_text().splitlines()]


def digests(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_determinism_and_scale_invariance(tmp_path):
    for name, seed, count in [('a', 7, 0), ('b', 7, 0), ('c', 8, 0), ('d', 7, 20)]:
        generate(tmp_path / name, seed=seed, filler_chunks=count)
    assert digests(tmp_path / 'a') == digests(tmp_path / 'b')
    assert digests(tmp_path / 'a') != digests(tmp_path / 'c')
    a, d = rows(tmp_path / 'a', 'gold.jsonl'), rows(tmp_path / 'd', 'gold.jsonl')
    assert a == d[:len(a)]
    assert SCALES == dict(ci=0, small=2000, medium=50000, large=250000, xl=1000000)
    with pytest.raises(FileExistsError):
        generate(tmp_path / 'a')
    with pytest.raises(ValueError):
        generate(tmp_path / 'invalid', filler_chunks=-1)
    assert not (tmp_path / 'invalid').exists()


def test_gold_resolves_and_names_are_fictional(tmp_path):
    generate(tmp_path / 'case', filler_chunks=13)
    root = tmp_path / 'case'
    sources = {s['source_id']: s for s in rows(root, 'sources.jsonl')}
    chunks = {c['chunk_id']: c for c in rows(root, 'chunks.jsonl')}
    gold = rows(root, 'gold.jsonl')
    for source in sources.values():
        data = (root / source['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest() == source['sha256']
        assert len(data) == source['bytes']
        assert '/' in source['media_type']
    for chunk in chunks.values():
        source = sources[chunk['source_id']]
        if source['type'] == 'pdf':
            assert chunk['text'] in PdfReader(root / source['path']).pages[0].extract_text()
        else:
            data = (root / source['path']).read_bytes()
            assert data[chunk['start']:chunk['end']].decode() == chunk['text']
    for record in gold:
        row, kind = record['value'], record['kind']
        assert record['answer_key_version'] == '1.0.0'
        if kind == 'transactions':
            source = sources[row['locator']['source_id']]
            ledger = list(csv.DictReader((root / source['path']).read_text().splitlines()))
            assert ledger[row['locator']['row'] - 2] == {k: v for k, v in row.items() if k != 'locator'}
            assert Decimal(row['amount']) > 0
        if kind == 'questions':
            for passage in row['decisive_passages']:
                chunk = chunks[passage['chunk_id']]
                assert passage['source_id'] == chunk['source_id']
                assert passage['support_text'] in chunk['text']
        if kind == 'entities':
            allowed = ({f'{a} {b}' for a in fraud.FIRST for b in fraud.LAST}
                       if row['entity_type'] == 'person' else set(fraud.COMPANIES))
            assert row['canonical_name'] in allowed
        if kind == 'accounts':
            assert row['bank_id'] in fraud.BANKS
    assert {s['type'] for s in sources.values()} == {'csv', 'pdf', 'eml', 'mbox', 'txt'}
    mail = next(s for s in sources.values() if s['type'] == 'eml')
    assert BytesParser(policy=policy.default).parsebytes((root / mail['path']).read_bytes()).is_multipart()
    manifest = json.loads((root / 'manifest.json').read_text())
    assert manifest['transactions'] == sum(r['kind'] == 'transactions' for r in gold)
    assert manifest['files'] == len(digests(root))
    assert any(place in chunks['identity-v1-p1']['text'] for place in fraud.PLACES)
    message = BytesParser(policy=policy.default).parsebytes((root / mail['path']).read_bytes())
    assert any(part.get_payload(decode=True) == (root / 'artifacts/attachment.txt').read_bytes()
               for part in message.walk() if part.get_filename() == 'attachment.txt')


def test_patterns_are_real_transactions(tmp_path):
    generate(tmp_path / 'case')
    gold = rows(tmp_path / 'case', 'gold.jsonl')
    tx = {r['value']['transaction_id']: r['value'] for r in gold if r['kind'] == 'transactions'}
    paths = [r['value'] for r in gold if r['kind'] == 'money_paths']
    assert {p['pattern_type'] for p in paths} == set(fraud.PATTERNS)
    for path in paths:
        hops = [tx[t] for t in path['hop_transaction_ids']]
        kind = path['pattern_type']
        if kind in {'layering', 'round_trip', 'rapid_in_out'}:
            assert all(a['to_account_id'] == b['from_account_id'] for a, b in zip(hops, hops[1:]))
        if kind == 'layering':
            assert 3 <= len(hops) <= 6
        if kind == 'round_trip':
            assert hops[0]['from_account_id'] == hops[-1]['to_account_id']
        if kind == 'structuring':
            assert len(hops) >= 3 and all(Decimal(t['amount']) < 10000 for t in hops)
        if kind == 'rapid_in_out':
            from datetime import datetime
            assert (datetime.fromisoformat(hops[-1]['posted_at']) -
                    datetime.fromisoformat(hops[0]['posted_at'])).total_seconds() <= 120
        if kind == 'no_business_purpose':
            assert hops[0]['to_account_id'] == 'acct-040'
            assert 'No business purpose' in hops[0]['memo']
        if kind == 'dormant_then_active':
            assert not any(t['from_account_id'] == 'acct-009' or t['to_account_id'] == 'acct-009'
                           for t in tx.values() if t['posted_at'] < hops[0]['posted_at'])
        if kind in {'fan_in', 'fan_out'}:
            field = 'to_account_id' if kind == 'fan_in' else 'from_account_id'
            assert len(hops) >= 3 and len({t[field] for t in hops}) == 1


def _streaming_peak(tmp_path, count, generator):
    generator(tmp_path / f'warm-{count}', filler_chunks=20)
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        generator(tmp_path / f'measured-{count}', filler_chunks=count)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _streaming_peaks(tmp_path, generator):
    child = '''
import json
import sys
from pathlib import Path
from tests import test_synthetic_corpus_gold as tests

peak = tests._streaming_peak(
    Path(sys.argv[1]), int(sys.argv[2]), getattr(tests, sys.argv[3]),
)
print(json.dumps(peak))
'''
    peaks = []
    for count in (1000, 4000):
        result = subprocess.run(
            [sys.executable, '-c', child, str(tmp_path), str(count), generator.__name__],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, check=True, timeout=60,
        )
        peaks.append(json.loads(result.stdout))
    return peaks


def _assert_flat_peaks(peaks):
    # 4x records must stay flat: allow 25% or 256 KB of allocator noise,
    # well below the growth from buffering 1000 versus 4000 chunk records.
    assert peaks[1] <= max(peaks[0] * 1.25, peaks[0] + 256_000), peaks


def _generate_buffered(out, *, filler_chunks):
    generate(out, filler_chunks=filler_chunks)
    content = (out / 'chunks.jsonl').read_text(encoding='utf-8')
    # fixture-write: synthetic corpus copy under the per-call temporary directory.
    (out / 'buffered.jsonl').write_text(content, encoding='utf-8')


def _record_streaming_call(out, *, filler_chunks):
    call = [out.name, filler_chunks, tracemalloc.is_tracing(), os.getpid()]
    # fixture-write: child call observations in the synthetic temporary directory.
    with (out.parent / 'calls.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(call) + '\n')


def test_streaming_memory_warms_before_tracing(tmp_path):
    _streaming_peaks(tmp_path, _record_streaming_call)
    calls = rows(tmp_path, 'calls.jsonl')
    assert [call[:3] for call in calls] == [
        ['warm-1000', 20, False], ['measured-1000', 1000, True],
        ['warm-4000', 20, False], ['measured-4000', 4000, True],
    ]
    assert calls[0][3] == calls[1][3]
    assert calls[2][3] == calls[3][3]
    assert len({os.getpid(), calls[0][3], calls[2][3]}) == 3
    assert not tracemalloc.is_tracing()


def test_streaming_memory(tmp_path):
    peaks = _streaming_peaks(tmp_path, generate)
    print(f'streaming peaks (bytes): {peaks}')
    _assert_flat_peaks(peaks)


def test_streaming_memory_ignores_parent_allocations(tmp_path):
    stop = threading.Event()
    allocated = threading.Event()

    def hold_parent_allocation():
        while not stop.wait(0.001):
            if (tmp_path / 'measured-4000').exists():
                pressure = bytearray(4_000_000)
                allocated.set()
                stop.wait()
                assert len(pressure) == 4_000_000
                return

    neighbor = threading.Thread(target=hold_parent_allocation)
    neighbor.start()
    try:
        test_streaming_memory(tmp_path)
        assert allocated.is_set(), 'parent pressure must overlap measurement'
    finally:
        stop.set()
        neighbor.join()


def test_streaming_memory_rejects_buffering(tmp_path):
    peaks = _streaming_peaks(tmp_path, _generate_buffered)
    print(f'buffered peaks (bytes): {peaks}')
    with pytest.raises(AssertionError):
        _assert_flat_peaks(peaks)


def test_eval_suite_and_lane_diagnostics(tmp_path, monkeypatch):
    from case_intelligence import review_bench_v2 as product
    from case_intelligence.defense_eval_lanes import RecordingBackend, RecordingReranker
    generate(tmp_path / 'case')
    suite = ev.load_suite(tmp_path / 'case' / 'suite.json', root=tmp_path / 'case')
    class Backend:
        def lexical(self, matter_id, query, limit):
            return tuple(product.Candidate(p.matter_id, p.passage_id, p.passage_id,
                         p.path, 1, p.text, 1.0) for p in ev.rank_passages(suite.passages, query, min(limit, ev.MAX_TOP_K)))
        def dense(self, matter_id, vector, query, limit):
            return self.lexical(matter_id, query, limit)
    adapter = object.__new__(ev.PostgresHybridRetriever)
    adapter.suite, adapter._trace, adapter._last_search = suite, {}, None
    adapter._database_ids = {'fraud': 'fraud'}
    adapter._retriever = product.HybridRetriever(RecordingBackend(Backend(), adapter._trace),
        product.DeterministicEmbeddingAdapter(),
        RecordingReranker(product.DeterministicReranker(), adapter._trace))
    monkeypatch.setattr(ev.time, 'perf_counter', lambda: 0)
    card = ev.run_eval(suite, ev.ReferenceStub(), retriever=adapter)
    assert card['aggregates']['questions'] == 6
    assert card['aggregates']['retrieval_hit'] == 5
    assert 'lane_diagnostics' in card
    assert all('lane_diagnostics' in q for q in card['questions'])
    original = ev.run_eval(ev.load_suite(), ev.ReferenceStub())
    again = ev.run_eval(ev.load_suite(), ev.ReferenceStub())
    assert original == again
    assert original['aggregates']['retrieval_hit'] == 34
