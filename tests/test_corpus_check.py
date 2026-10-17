"""Gate falsification tests: tolerances and citations must fail closed."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('corpus_check', ROOT/'scripts/corpus_check.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_error_rate_counts_deletion_insertion_and_substitution():
    assert gate.error_rate('abc', 'abc') == 0
    assert gate.error_rate('abc', 'ac') == pytest.approx(1/3)
    assert gate.error_rate('abc', 'axc') == pytest.approx(1/3)
    assert gate.error_rate(['blue'], ['very','blue']) == 1
    assert gate.error_rate('a', 'abcd') == 3


def test_normalization_keeps_fact_changes():
    assert gate.normalize('  BLUE, bicycle! ') == 'blue bicycle'
    assert gate.normalize('09:15') != gate.normalize('09:45')


def sample():
    manifest = {'documents':[{'id':'pdf','ocr':False}], 'audio':[{'id':'wav'}], 'search':[{'id':'search','top_k':1}], 'answers':[{'id':'answer'}]}
    citation = {'document_id':'pdf','excerpt':'The parcel is blue.', 'source_valid':True}
    expected = {'documents':{'pdf':{'text':'The parcel is blue.','cer':0.03}}, 'audio':{'wav':{'text':'The bicycle is green.','wer':0.15}}, 'search':{'search':{'document_ids':['pdf']}}, 'answers':{'answer':{'facts':['parcel is blue'],'citations':[{'document_id':'pdf','span':'parcel is blue'}]}}}
    actual = {'pdf':{'text':'The parcel is blue.'},'wav':{'text':'Speaker 1: The bicycle is green.', 'asr_text':'The bicycle is green.'},'search':{'document_ids':['pdf'],'citations':[dict(citation)]},'answer':{'text':'The parcel is blue.','route':'question','citations':[citation]}}
    return manifest, expected, actual


@pytest.mark.parametrize('mutation', ['missing','fact','wrong-source','missing-citation','extra-citation','invalid-span','route','rank','search-support','search-invalid','text','transcript','projection'])
def test_regressions_fail_with_case_diff(mutation):
    manifest, expected, actual = sample()
    if mutation == 'missing': del actual['answer']
    elif mutation == 'fact': actual['answer']['text'] = 'The parcel is red.'
    elif mutation == 'wrong-source': actual['answer']['citations'][0]['document_id'] = 'other'
    elif mutation == 'missing-citation': actual['answer']['citations'] = []
    elif mutation == 'extra-citation': actual['answer']['citations'].append({'document_id':'other','excerpt':'parcel is blue','source_valid':True})
    elif mutation == 'invalid-span': actual['answer']['citations'][0]['source_valid'] = False
    elif mutation == 'route': actual['answer']['route'] = 'exact'
    elif mutation == 'rank': actual['search']['document_ids'] = ['other','pdf']
    elif mutation == 'search-support': actual['search']['citations'] = []
    elif mutation == 'search-invalid': actual['search']['citations'][0]['source_valid'] = False
    elif mutation == 'text': actual['pdf']['text'] = ''
    elif mutation == 'projection': actual['wav']['text'] = 'Speaker 1: The bicycle.'
    else: actual['wav']['asr_text'] = 'Another entirely wrong statement.'
    failures = [r for r in gate.compare(manifest, expected, actual, full=True) if not r['passed']]
    assert failures and all(r['diff'] for r in failures)


def test_fast_does_not_claim_recognition():
    manifest, expected, actual = sample()
    del actual['wav']
    assert all(r['passed'] for r in gate.compare(manifest, expected, actual))
    assert not all(r['passed'] for r in gate.compare(manifest, expected, actual, full=True))


def test_full_requires_ocr_not_native_text():
    manifest, expected, actual = sample()
    manifest['documents'][0]['ocr'] = True
    assert any('OCR' in str(r['diff']) for r in gate.compare(manifest, expected, actual, full=True))


def test_committed_fixture_inventory_and_pins():
    manifest, expected = gate.load_corpus(gate.CORPUS)
    assert len(manifest['documents']) >= 3
    from pypdf import PdfReader
    for doc in manifest['documents']:
        text = ''.join(page.extract_text() for page in PdfReader(gate.CORPUS/doc['path']).pages)
        assert bool(text.strip()) is not doc['ocr']
    import wave
    for clip in manifest['audio']:
        with wave.open(str(gate.CORPUS/clip['path'])) as wav:
            assert 1 < wav.getnframes()/wav.getframerate() < 30
            assert wav.getnchannels() == 1
            assert any(wav.readframes(wav.getnframes()))


@pytest.mark.parametrize('mutation',['digest','truncated','oversized','duplicate','missing-gold','empty-facts','blank-facts','blank-span','empty-hits','unknown-hit','zero-k','nan','traversal'])
def test_manifest_or_gold_corruption_is_rejected(tmp_path, mutation):
    root = tmp_path/'corpus'; shutil.copytree(gate.CORPUS,root)
    path=root/'manifest.json'; manifest=json.loads(path.read_text())
    gold=root/'expected.json'; expected=json.loads(gold.read_text())
    if mutation=='digest': manifest['documents'][0]['sha256']='0'*64
    elif mutation=='truncated': (root/manifest['audio'][0]['path']).write_bytes(b'bad')
    elif mutation=='oversized': manifest['max_file_bytes']=10000001
    elif mutation=='duplicate': manifest['audio'][0]['id']=manifest['documents'][0]['id']
    elif mutation=='missing-gold': expected['search'].clear()
    elif mutation=='empty-facts': next(iter(expected['answers'].values()))['facts']=[]
    elif mutation=='blank-facts': next(iter(expected['answers'].values()))['facts']=['   ']
    elif mutation=='blank-span': next(iter(expected['answers'].values()))['citations'][0]['span']='   '
    elif mutation=='empty-hits': next(iter(expected['search'].values()))['document_ids']=[]
    elif mutation=='unknown-hit': next(iter(expected['search'].values()))['document_ids']=['unknown']
    elif mutation=='zero-k': manifest['search'][0]['top_k']=0
    elif mutation=='nan': next(iter(expected['documents'].values()))['cer']=float('nan')
    else: manifest['documents'][0]['path']='../outside.pdf'
    path.write_text(json.dumps(manifest)); gold.write_text(json.dumps(expected))
    with pytest.raises(ValueError): gate.load_corpus(root)


def test_report_cannot_overwrite_gold():
    before = (gate.CORPUS/'expected.json').read_bytes()
    assert gate.main(['--report',str(gate.CORPUS/'expected.json')]) == 1
    assert (gate.CORPUS/'expected.json').read_bytes() == before


def test_update_requires_explicit_flag_and_separate_proposal():
    with pytest.raises(SystemExit): gate.main(['--update-expected'])
    assert gate.main(['--update-expected','--proposal',str(gate.CORPUS/'expected.json')]) == 1


def test_asr_staging_and_offline_lookup_require_identical_files(monkeypatch):
    import sys
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('corpus_recognition', ROOT/'scripts/corpus_recognition.py')
    recognition = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recognition)
    calls = []
    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(snapshot_download=lambda *a, **kw: calls.append((a, kw))))
    pin = {'model_id':'synthetic/model', 'revision':'a'*40}
    recognition.stage({'models':{'asr':pin}})
    recognition.snapshot(pin, offline=True)
    assert calls[0][0] == calls[1][0] == ('synthetic/model',)
    assert calls[0][1]['allow_patterns'] == calls[1][1]['allow_patterns']
    assert calls[1][1]['revision'] == 'a'*40
    assert calls[0][1]['local_files_only'] is False
    assert calls[1][1]['local_files_only'] is True
