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
    actual = {'pdf':{'text':'The parcel is blue.'},'wav':{'text':'Speaker 1: The bicycle is green.', 'asr_text':'The bicycle is green.'},'search':{'document_ids':['pdf'],'citations':[dict(citation)]},'answer':{'text':'The parcel is blue.','route':'question','dispatched':True,'answerable':True,'citations':[citation], 'claims':[{'text':'The parcel is blue.', 'citations':[citation]}]}}
    return manifest, expected, actual


@pytest.mark.parametrize('fact,wrong', [('09:15', '109:150'), ('09:15', '09:150'),
                                     ('blue', 'blueberry'), ('locker seven', 'locker seventeen')])
def test_answer_fact_requires_whole_normalized_tokens(fact, wrong):
    manifest, expected, actual = sample()
    expected['answers']['answer'] = {'facts':[fact], 'citations':[{'document_id':'pdf', 'span':fact}]}
    citation = {'document_id':'pdf', 'excerpt':fact, 'source_valid':True}
    actual['answer'].update(text=wrong, citations=[citation], claims=[{'text':wrong, 'citations':[citation]}])
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is False
    assert any('fact' in difference for difference in result['diff'])


@pytest.mark.parametrize('mutation', ['missing', 'empty', 'swapped', 'unattached', 'duplicate-unsupported'])
def test_answer_facts_require_their_own_claim_citations(mutation):
    manifest, expected, actual = sample()
    required = [{'document_id':'pdf', 'span':'blue bicycle'}, {'document_id':'wav', 'span':'orange parcel'}]
    expected['answers']['answer'] = {'facts':['blue bicycle', 'orange parcel'], 'citations':required}
    citations = [{'document_id':item['document_id'], 'excerpt':item['span'], 'source_valid':True}
                 for item in required]
    claims = [{'text':item['excerpt'], 'citations':[item]} for item in citations]
    actual['answer'].update(text='blue bicycle orange parcel', citations=citations, claims=claims)
    assert next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')['passed']
    if mutation == 'missing': del actual['answer']['claims']
    elif mutation == 'empty': actual['answer']['claims'] = []
    elif mutation == 'swapped':
        claims[0]['citations'], claims[1]['citations'] = claims[1]['citations'], claims[0]['citations']
    elif mutation == 'unattached': claims[0]['citations'] = []
    else: claims.append({'text':'blue bicycle', 'citations':[citations[1]]})
    if mutation not in {'missing', 'empty'}:
        actual['answer']['text'] = ' '.join(claim['text'] for claim in claims)
        actual['answer']['citations'] = [citation for claim in claims for citation in claim['citations']]
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is False
    assert any('claim' in difference for difference in result['diff'])
    if mutation in {'swapped', 'unattached', 'duplicate-unsupported'}:
        assert any('lacks support attached to its claim' in difference or 'invalid observed claim' in difference
                   for difference in result['diff'])


@pytest.mark.parametrize('fact,text', [('09:15', 'Arrived at (09:15).'),
    ('blue bicycle', 'The BLUE—BICYCLE arrived.'), ('locker seven', 'Locker SEVEN!')])
def test_answer_fact_normalization_preserves_case_and_punctuation(fact, text):
    manifest, expected, actual = sample()
    expected['answers']['answer'] = {'facts':[fact], 'citations':[{'document_id':'pdf', 'span':fact}]}
    citation = {'document_id':'pdf', 'excerpt':text, 'source_valid':True}
    actual['answer'].update(text=text, citations=[citation], claims=[{'text':text, 'citations':[citation]}])
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is True


@pytest.mark.parametrize('text,kind,passes', [
    ('The parcel is blue.', 'document', True),
    ('A reviewer confirmed that The parcel is blue.', 'document', False),
    ('The parcel is blue. It was hidden.', 'document', False),
    ('The machine transcript appears to say that The parcel is blue.', 'transcript', True),
    ('The machine transcript appears to say that The parcel is blue.', 'document', False),
    ('The machine transcript appears to say that The machine transcript appears to say that The parcel is blue.', 'transcript', False),
    ('The machine transcript appears to say that The parcel is blue. It was hidden.', 'transcript', False),
    ('The parcel is blue.', 'transcript', False),
])
def test_complete_claim_requires_own_passage_with_only_exact_transcript_qualifier(text, kind, passes):
    manifest, expected, actual = sample()
    citation = actual['answer']['citations'][0]
    citation['evidence_kind'] = kind
    actual['answer']['text'] = actual['answer']['claims'][0]['text'] = text
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is passes
    if not passes:
        assert any('complete claim' in difference for difference in result['diff'])


@pytest.mark.parametrize('claimed,observed,passes', [
    ('09:15', '09:45', False), ('locker seven', 'Locker 7', False), ('Locker 7', 'locker 7', True),
])
def test_complete_claim_cannot_change_details_beyond_required_gold_fact(claimed, observed, passes):
    manifest, expected, actual = sample()
    claim = actual['answer']['claims'][0]
    claim['text'] = actual['answer']['text'] = f'The parcel is blue at {claimed}.'
    claim['citations'][0]['excerpt'] = f'The parcel is blue at {observed}.'
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is passes
    if not passes:
        assert any('complete claim' in difference for difference in result['diff'])


@pytest.mark.parametrize('present,answerable', [(False, None), (True, None), (True, False),
    (True, 0), (True, 1), (True, 'true'), (True, 'false'), (True, []), (True, {})],
    ids=['missing', 'null', 'false', 'zero', 'one', 'true-string', 'false-string', 'list', 'dict'])
def test_answer_requires_explicit_true_despite_valid_facts_and_citations(present, answerable):
    manifest, expected, actual = sample()
    if not present:
        del actual['answer']['answerable']
    else:
        actual['answer']['answerable'] = answerable
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is False
    assert any('answerable' in difference for difference in result['diff'])


@pytest.mark.parametrize('mutation', ['missing','fact','wrong-source','missing-citation','extra-citation','invalid-span','route','dispatch','rank','search-support','search-invalid','text','transcript','projection'])
def test_regressions_fail_with_case_diff(mutation):
    manifest, expected, actual = sample()
    if mutation == 'missing': del actual['answer']
    elif mutation == 'fact': actual['answer']['text'] = 'The parcel is red.'
    elif mutation == 'wrong-source': actual['answer']['citations'][0]['document_id'] = 'other'
    elif mutation == 'missing-citation': actual['answer']['citations'] = []
    elif mutation == 'extra-citation': actual['answer']['citations'].append({'document_id':'other','excerpt':'parcel is blue','source_valid':True})
    elif mutation == 'invalid-span': actual['answer']['citations'][0]['source_valid'] = False
    elif mutation == 'route': actual['answer']['route'] = 'exact'
    elif mutation == 'dispatch': actual['answer']['dispatched'] = False
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


def test_receipt_records_measured_recognition_error_rates():
    manifest, expected, actual = sample()
    records = gate.compare(manifest, expected, actual, full=True)
    assert records[0]['metric'] == 'cer'
    assert records[1]['metric'] == 'wer'
    assert records[0]['error_rate'] == records[1]['error_rate'] == 0
    assert records[1]['tolerance'] == .15


@pytest.mark.parametrize('excerpt,valid,passes', [
    ('The parcel is in locker seven.', True, True),
    ('The parcel is in Locker 7.', True, True),
    ('The parcel is in Locker 8.', True, False),
    ('The parcel is in Locker 70.', True, False),
    ('The parcel is in Locker 71.', True, False),
    ('The parcel is in Locker 7.', False, False),
])
def test_reviewed_span_alternative_preserves_number_and_source_checks(excerpt, valid, passes):
    manifest, expected, actual = sample()
    expected['answers']['answer']['facts'] = ['parcel is in']
    expected['answers']['answer']['citations'] = [{
        'document_id': 'pdf', 'span': 'parcel is in locker seven',
        'alternative_spans': ['parcel is in locker 7'],
    }]
    actual['answer']['text'] = excerpt
    actual['answer']['citations'] = [{'document_id':'pdf', 'excerpt':excerpt, 'source_valid':valid}]
    actual['answer']['claims'] = [{'text':excerpt, 'citations':actual['answer']['citations']}]
    result = next(r for r in gate.compare(manifest, expected, actual) if r['id'] == 'answer')
    assert result['passed'] is passes


@pytest.mark.parametrize('alternatives', [[' '], [None], 'locker 7'])
def test_malformed_span_alternatives_are_rejected(tmp_path, alternatives):
    root = tmp_path/'corpus'; shutil.copytree(gate.CORPUS, root)
    path = root/'expected.json'; expected = json.loads(path.read_text())
    expected['answers']['answer-parcel']['citations'][1]['alternative_spans'] = alternatives
    path.write_text(json.dumps(expected))
    with pytest.raises(ValueError): gate.load_corpus(root)


@pytest.mark.parametrize('missing_source', ['scan-en', 'scan-es'])
def test_full_gate_rejects_ocr_catalog_loss_even_with_accurate_recognition(missing_source):
    manifest, expected = gate.load_corpus(gate.CORPUS)
    actual = {}
    for source, want in expected['documents'].items():
        actual[source] = {'text':want['text'], 'ocr_pages':1}
    for source, want in expected['audio'].items():
        actual[source] = {'text':want['text'], 'asr_text':want['text']}
    for key, want in expected['search'].items():
        actual[key] = {'document_ids':list(want['document_ids']), 'citations':[
            {'document_id':source, 'excerpt':'Synthetic search support', 'source_valid':True}
            for source in want['document_ids']]}
    for key, want in expected['answers'].items():
        citations = [{'document_id':c['document_id'], 'excerpt':c['span'], 'source_valid':True}
                     for c in want['citations']]
        claims = [{'text':c['excerpt'], 'citations':[c]} for c in citations]
        actual[key] = {'text':' '.join(claim['text'] for claim in claims), 'route':'question',
            'dispatched':True, 'answerable':True, 'citations':citations, 'claims':claims}
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual, full=True))
    for key in expected['search']:
        actual[key]['document_ids'] = [s for s in actual[key]['document_ids'] if s != missing_source]
        actual[key]['citations'] = [c for c in actual[key]['citations'] if c['document_id'] != missing_source]
    result = gate.compare(manifest, expected, actual, full=True)
    assert any(not row['passed'] and row['id'].startswith('search-') for row in result), (
        f'Accurate OCR must not conceal missing searchable source {missing_source}', result)


@pytest.mark.parametrize('source', ['scan-en', 'scan-es'])
def test_manifest_cannot_remove_scanned_source_discovery_coverage(tmp_path, source):
    root = tmp_path/'corpus'; shutil.copytree(gate.CORPUS, root)
    path = root/'manifest.json'; manifest = json.loads(path.read_text())
    gold = root/'expected.json'; expected = json.loads(gold.read_text())
    key = 'search-' + source
    manifest['search'] = [case for case in manifest['search'] if case['id'] != key]
    del expected['search'][key]
    path.write_text(json.dumps(manifest)); gold.write_text(json.dumps(expected))
    with pytest.raises(ValueError, match='Every OCR source'):
        gate.load_corpus(root)


@pytest.mark.parametrize('full_only', [False, 1, 'true'])
def test_scanned_search_requires_boolean_full_lane(tmp_path, full_only):
    root = tmp_path/'corpus'; shutil.copytree(gate.CORPUS, root)
    path = root/'manifest.json'; manifest = json.loads(path.read_text())
    next(case for case in manifest['search'] if case['id'] == 'search-scan-en')['full_only'] = full_only
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='full-only lane'):
        gate.load_corpus(root)


def test_scanned_search_is_required_only_in_full_comparison():
    manifest, expected = gate.load_corpus(gate.CORPUS)
    fast = gate.compare(manifest, expected, {})
    full = gate.compare(manifest, expected, {}, full=True)
    for key in ['search-scan-en', 'search-scan-es']:
        assert not any(row['id'] == key for row in fast)
        assert any(row['id'] == key and not row['passed'] for row in full)
