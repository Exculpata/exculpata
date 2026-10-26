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


def copy_corpus(destination):
    """Keep mutation fixtures writable when CI protects the reviewed originals."""
    for source in gate.CORPUS.rglob('*'):
        if source.is_file():
            target = destination / source.relative_to(gate.CORPUS)
            target.parent.mkdir(parents=True, exist_ok=True)
            # fixture-write: Copy synthetic corpus bytes without read-only modes.
            shutil.copyfile(source, target)


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
    citation = {'document_id':'pdf','excerpt':'The parcel is blue.', 'source_valid':True, 'evidence_kind':'document'}
    expected = {'documents':{'pdf':{'text':'The parcel is blue.','cer':0.03}}, 'audio':{'wav':{'text':'The bicycle is green.','wer':0.15}}, 'search':{'search':{'document_ids':['pdf']}}, 'answers':{'answer':{'facts':['parcel is blue'],'citations':[{'document_id':'pdf','span':'parcel is blue'}]}}}
    actual = {'pdf':{'text':'The parcel is blue.'},'wav':{'text':'Speaker 1: The bicycle is green.', 'asr_text':'The bicycle is green.'},'search':{'document_ids':['pdf'],'citations':[dict(citation)]},'answer':{'text':'The parcel is blue.','route':'question','dispatched':True,'answerable':True,'citations':[citation], 'claims':[{'text':'The parcel is blue.', 'citations':[citation]}]}}
    seed_document_support(actual, citation)
    actual['answer']['kind'] = 'generated'
    seed_document_support(actual, actual['search']['citations'][0])
    expected['documents']['pdf']['pages'] = actual['pdf']['pages'] = 1
    expected['search']['search']['citations'] = [{'document_id':'pdf', 'span':'parcel is blue'}]
    seed_audio_support(expected, actual, {'document_id':'wav', 'excerpt':actual['wav']['text']})
    return manifest, expected, actual


def seed_document_support(actual, citation):
    citation.update(unit=1, location='Page 1', start=0, end=len(citation['excerpt']))
    actual[citation['document_id']] = {'pages':1, 'text':citation['excerpt'], 'state':'ready'}


def seed_audio_support(expected, actual, citation):
    source = citation['document_id']
    text = citation['excerpt']
    reference = gate.re.sub(r'^Speaker [0-9]+:\s*', '', text)
    citation.update(unit=1, start_ms=0, end_ms=2000, location='00:00–00:02', start=0, end=len(text))
    expected['audio'][source] = {'text':reference, 'wer':0.15,
        'locator':{'start_ms':0, 'end_ms':2000, 'tolerance_ms':500},
        'layouts':[[{'text':reference, 'start_ms':0, 'end_ms':2000}]]}
    actual[source] = {'text':text, 'asr_text':reference, 'state':'ready',
        'units':[{'unit':1, 'text':text, 'start_ms':0, 'end_ms':2000}]}


@pytest.mark.parametrize('fact,wrong', [('09:15', '109:150'), ('09:15', '09:150'),
                                     ('blue', 'blueberry'), ('locker seven', 'locker seventeen')])
def test_answer_fact_requires_whole_normalized_tokens(fact, wrong):
    manifest, expected, actual = sample()
    expected['answers']['answer'] = {'facts':[fact], 'citations':[{'document_id':'pdf', 'span':fact}]}
    citation = {'document_id':'pdf', 'excerpt':fact, 'source_valid':True, 'evidence_kind':'document'}
    actual['answer'].update(text=wrong, citations=[citation], claims=[{'text':wrong, 'citations':[citation]}])
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is False
    assert any('fact' in difference for difference in result['diff'])


@pytest.mark.parametrize('mutation', ['missing', 'empty', 'swapped', 'unattached', 'duplicate-unsupported'])
def test_answer_facts_require_their_own_claim_citations(mutation):
    manifest, expected, actual = sample()
    required = [{'document_id':'pdf', 'span':'blue bicycle'}, {'document_id':'wav', 'span':'orange parcel'}]
    expected['answers']['answer'] = {'facts':['blue bicycle', 'orange parcel'], 'citations':required}
    citations = [{'document_id':item['document_id'], 'excerpt':item['span'], 'source_valid':True,
                  'evidence_kind':'transcript' if item['document_id'] == 'wav' else 'document'}
                 for item in required]
    seed_document_support(actual, citations[0])
    seed_audio_support(expected, actual, citations[1])
    claims = [{'text':(gate.TRANSCRIPT_QUALIFIER if item['evidence_kind'] == 'transcript' else '') + item['excerpt'],
               'citations':[item]} for item in citations]
    actual['answer'].update(text=' '.join(claim['text'] for claim in claims), citations=citations, claims=claims)
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
    citation = {'document_id':'pdf', 'excerpt':text, 'source_valid':True, 'evidence_kind':'document'}
    seed_document_support(actual, citation)
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
    if kind == 'transcript':
        citation['document_id'] = expected['answers']['answer']['citations'][0]['document_id'] = 'wav'
        seed_audio_support(expected, actual, citation)
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
    seed_document_support(actual, claim['citations'][0])
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is passes
    if not passes:
        assert any('complete claim' in difference for difference in result['diff'])


@pytest.mark.parametrize('source', ['pdf', 'wav'])
@pytest.mark.parametrize('observed_kind', ['document', 'transcript', None], ids=['document', 'transcript', 'missing'])
@pytest.mark.parametrize('qualified', [False, True], ids=['plain', 'qualified'])
def test_claim_kind_and_attribution_are_bound_to_manifest_source(source, observed_kind, qualified):
    manifest, expected, actual = sample()
    expected['answers']['answer']['citations'][0]['document_id'] = source
    citation = actual['answer']['citations'][0]
    citation['document_id'] = source
    if source == 'wav':
        seed_audio_support(expected, actual, citation)
    if observed_kind is None:
        citation.pop('evidence_kind', None)
    else:
        citation['evidence_kind'] = observed_kind
    text = (gate.TRANSCRIPT_QUALIFIER if qualified else '') + 'The parcel is blue.'
    actual['answer']['text'] = actual['answer']['claims'][0]['text'] = text
    expected_kind = 'transcript' if source == 'wav' else 'document'
    passes = observed_kind == expected_kind and qualified == (source == 'wav')
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is passes


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
    del actual['wav']['asr_text']
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
    root = tmp_path/'corpus'; copy_corpus(root)
    path=root/'manifest.json'; manifest=json.loads(path.read_text())
    gold=root/'expected.json'; expected=json.loads(gold.read_text())
    if mutation=='digest': manifest['documents'][0]['sha256']='0'*64
    elif mutation=='truncated':
        # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
        (root/manifest['audio'][0]['path']).write_bytes(b'bad')
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
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(manifest))
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    gold.write_text(json.dumps(expected))
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
    actual['answer']['citations'] = [{'document_id':'pdf', 'excerpt':excerpt, 'source_valid':valid,
                                     'evidence_kind':'document'}]
    seed_document_support(actual, actual['answer']['citations'][0])
    actual['answer']['claims'] = [{'text':excerpt, 'citations':actual['answer']['citations']}]
    result = next(r for r in gate.compare(manifest, expected, actual) if r['id'] == 'answer')
    assert result['passed'] is passes


@pytest.mark.parametrize('alternatives', [[' '], [None], 'locker 7'])
def test_malformed_span_alternatives_are_rejected(tmp_path, alternatives):
    root = tmp_path/'corpus'; copy_corpus(root)
    path = root/'expected.json'; expected = json.loads(path.read_text())
    expected['answers']['answer-parcel']['citations'][1]['alternative_spans'] = alternatives
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(expected))
    with pytest.raises(ValueError): gate.load_corpus(root)


@pytest.mark.parametrize('missing_source', ['scan-en', 'scan-es'])
def test_full_gate_rejects_ocr_catalog_loss_even_with_accurate_recognition(missing_source):
    manifest, expected = gate.load_corpus(gate.CORPUS)
    actual = {}
    for source, want in expected['documents'].items():
        actual[source] = {'text':want['text'], 'ocr_pages':1, 'pages':want['pages'], 'state':'ready'}
    for source, want in expected['audio'].items():
        actual[source] = {'text':want['text'], 'asr_text':want['text'], 'state':'ready',
            'units':[dict(want['layouts'][0][0], unit=1)]}
    for key, want in expected['search'].items():
        actual[key] = {'document_ids':list(want['document_ids']), 'citations':[
            corpus_citation(source, expected, actual) for source in want['document_ids']]}
    for key, want in expected['answers'].items():
        citations = [corpus_citation(c['document_id'], expected, actual) for c in want['citations']]
        claims = [{'text':(gate.TRANSCRIPT_QUALIFIER if c['evidence_kind'] == 'transcript' else '') + c['excerpt'],
                   'citations':[c]} for c in citations]
        actual[key] = {'text':' '.join(claim['text'] for claim in claims), 'route':'question',
            'kind':'generated', 'dispatched':True, 'answerable':True, 'citations':citations, 'claims':claims}
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual, full=True))
    for key in expected['search']:
        actual[key]['document_ids'] = [s for s in actual[key]['document_ids'] if s != missing_source]
        actual[key]['citations'] = [c for c in actual[key]['citations'] if c['document_id'] != missing_source]
    result = gate.compare(manifest, expected, actual, full=True)
    assert any(not row['passed'] and row['id'].startswith('search-') for row in result), (
        f'Accurate OCR must not conceal missing searchable source {missing_source}', result)


@pytest.mark.parametrize('source', ['scan-en', 'scan-es'])
def test_manifest_cannot_remove_scanned_source_discovery_coverage(tmp_path, source):
    root = tmp_path/'corpus'; copy_corpus(root)
    path = root/'manifest.json'; manifest = json.loads(path.read_text())
    gold = root/'expected.json'; expected = json.loads(gold.read_text())
    key = 'search-' + source
    expected['search'][key]['document_ids'] = ['bicycle']
    expected['search'][key]['citations'] = [
        {'document_id':'bicycle', 'span':expected['documents']['bicycle']['text']}]
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(manifest))
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    gold.write_text(json.dumps(expected))
    with pytest.raises(ValueError, match='Every OCR source'):
        gate.load_corpus(root)


@pytest.mark.parametrize('full_only', [False, 1, 'true'])
def test_scanned_search_requires_boolean_full_lane(tmp_path, full_only):
    root = tmp_path/'corpus'; copy_corpus(root)
    path = root/'manifest.json'; manifest = json.loads(path.read_text())
    next(case for case in manifest['search'] if case['id'] == 'search-scan-en')['full_only'] = full_only
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
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


def corpus_citation(source, expected, actual):
    audio = source in expected['audio']
    citation = {'document_id':source, 'excerpt':actual[source]['text'], 'source_valid':True,
                'evidence_kind':'transcript' if audio else 'document'}
    if audio:
        citation.update({key:actual[source]['units'][0][key] for key in ('unit', 'start_ms', 'end_ms')})
        citation.update(location=gate.transcript_location(citation['start_ms'], citation['end_ms']),
                        start=0, end=len(citation['excerpt']))
    else:
        citation.update(unit=1, location='Page 1', start=0, end=len(citation['excerpt']))
    return citation


@pytest.mark.parametrize('pages', [None, True, False, 0, 2, 1.0, '1'])
def test_pdf_page_count_requires_exact_integer(pages):
    manifest, expected, actual = sample()
    actual['pdf']['pages'] = pages
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'pdf')
    assert result['passed'] is False
    assert any('PDF pages' in line for line in result['diff'])


@pytest.mark.parametrize('start,end,passes', [
    (0, 2000, True), (500, 1500, True), (0, 2500, True),
    (501, 2000, False), (0, 2501, False), (1000, 1100, False),
    (-1, 2000, False), (2000, 1000, False), (0, 0, False),
    (None, 2000, False), (False, 2000, False), (0, True, False),
    (0.0, 2000, False), (0, 2000.0, False), ('0', 2000, False),
])
def test_audio_unit_locators_are_bounded_and_typed_in_both_lanes(start, end, passes):
    manifest, expected, actual = sample()
    actual['wav']['units'][0].update(start_ms=start, end_ms=end)
    for full in (False, True):
        result = next(row for row in gate.compare(manifest, expected, actual, full=full) if row['id'] == 'wav')
        assert result['passed'] is passes


@pytest.mark.parametrize('mutation', ['missing', 'empty', 'wrong-text', 'duplicate', 'missing-start'])
def test_audio_unit_layout_must_be_present_and_match_projected_text(mutation):
    manifest, expected, actual = sample()
    audio = actual['wav']
    if mutation == 'missing': del audio['units']
    elif mutation == 'empty': audio['units'] = []
    elif mutation == 'wrong-text': audio['units'][0]['text'] = 'Different synthetic passage.'
    elif mutation == 'duplicate': audio['units'].append(dict(audio['units'][0]))
    else: del audio['units'][0]['start_ms']
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'wav')
    assert result['passed'] is False


@pytest.mark.parametrize('mutation', ['pages', 'boolean-pages', 'missing-search-spans', 'blank-search-span',
    'wrong-search-source', 'missing-locator', 'wide-tolerance', 'wrong-duration', 'empty-layouts',
    'layout-missing-time', 'layout-boolean-time', 'layout-out-of-bounds', 'layout-empty-text'])
def test_reviewed_page_search_and_timing_metadata_is_required(tmp_path, mutation):
    root = tmp_path/'corpus'; copy_corpus(root)
    path = root/'expected.json'; expected = json.loads(path.read_text())
    audio = expected['audio']['audio-bicycle']
    search = expected['search']['search-gate']
    if mutation == 'pages': del expected['documents']['bicycle']['pages']
    elif mutation == 'boolean-pages': expected['documents']['bicycle']['pages'] = True
    elif mutation == 'missing-search-spans': del search['citations']
    elif mutation == 'blank-search-span': search['citations'][0]['span'] = ' '
    elif mutation == 'wrong-search-source': search['citations'][0]['document_id'] = 'parcel'
    elif mutation == 'missing-locator': del audio['locator']
    elif mutation == 'wide-tolerance': audio['locator']['tolerance_ms'] = 501
    elif mutation == 'wrong-duration': audio['locator']['end_ms'] += 1
    elif mutation == 'empty-layouts': audio['layouts'] = []
    elif mutation == 'layout-missing-time': del audio['layouts'][0][0]['start_ms']
    elif mutation == 'layout-boolean-time': audio['layouts'][0][0]['start_ms'] = False
    elif mutation == 'layout-out-of-bounds': audio['layouts'][0][0]['end_ms'] = audio['locator']['end_ms'] + 1
    else: audio['layouts'][0][0]['text'] = ' '
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(expected))
    with pytest.raises(ValueError): gate.load_corpus(root)


@pytest.mark.parametrize('unit_id', [0, -1, True, 1.0, '1', None])
def test_audio_unit_ids_require_positive_integers(unit_id):
    manifest, expected, actual = sample()
    actual['wav']['units'][0]['unit'] = unit_id
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'wav')
    assert result['passed'] is False


def test_reviewed_split_cannot_overlap_even_within_endpoint_tolerance():
    manifest, expected = gate.load_corpus(gate.CORPUS)
    layout = expected['audio']['audio-parcel']['layouts'][1]
    units = [dict(unit, unit=index) for index, unit in enumerate(layout, 1)]
    observation = {'units':units, 'text':'\n'.join(unit['text'] for unit in units)}
    assert gate.matches_audio_layout(observation, expected['audio']['audio-parcel'])
    units[1]['start_ms'] -= 1
    assert not gate.matches_audio_layout(observation, expected['audio']['audio-parcel'])


@pytest.mark.parametrize('source,signal_end_ms', [('audio-bicycle', 5400), ('audio-parcel', 4650)])
def test_baseline_audio_layout_ends_at_pinned_source_signal_envelope(source, signal_end_ms):
    from array import array
    import math
    import sys
    import wave

    manifest, expected = gate.load_corpus(gate.CORPUS)
    clip = next(item for item in manifest['audio'] if item['id'] == source)
    with wave.open(str(gate.CORPUS/clip['path'])) as recording:
        assert recording.getnchannels() == 1 and recording.getsampwidth() == 2
        rate = recording.getframerate()
        assert rate % 100 == 0
        samples = array('h', recording.readframes(recording.getnframes()))
        if sys.byteorder != 'little':
            samples.byteswap()
    block_size = rate // 100
    last_signal_end = 0
    for start in range(0, len(samples), block_size):
        block = samples[start:start + block_size]
        rms = math.sqrt(sum(value * value for value in block) / len(block))
        if rms >= 32:
            last_signal_end = round((start + len(block)) * 1000 / rate)
    assert last_signal_end == signal_end_ms
    assert expected['audio'][source]['layouts'][0][0]['end_ms'] == last_signal_end
    assert expected['audio'][source]['locator']['end_ms'] == round(len(samples) * 1000 / rate)
    assert expected['audio'][source]['locator']['tolerance_ms'] == 500


@pytest.mark.parametrize('field,value', [('model_id', 'synthetic/unreviewed'), ('license', 'unreviewed'),
    ('model_id', None), ('license', None), ('revision', 'a'*40), ('languages', ['eng']), ('sha256', {})])
def test_ocr_identity_requires_complete_reviewed_record(tmp_path, field, value):
    root = tmp_path/'corpus'; copy_corpus(root)
    path = root/'manifest.json'; manifest = json.loads(path.read_text())
    if value is None:
        del manifest['models']['ocr'][field]
    else:
        manifest['models']['ocr'][field] = value
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='OCR'):
        gate.load_corpus(root)


@pytest.mark.parametrize('field,value', [
    ('unit', 99), ('unit', 0), ('unit', -1), ('unit', True), ('unit', 1.0), ('unit', '1'),
    ('location', 'Page 99'), ('location', 'Page 01'), ('location', ''),
    ('start', -1), ('start', 1), ('start', False), ('start', 0.0),
    ('end', 0), ('end', 99999), ('end', True), ('end', 19.0),
    ('start_ms', 0), ('end_ms', 1000), ('excerpt', 'parcel is blue'),
])
def test_document_citation_locator_is_bound_to_observed_page(field, value):
    manifest, expected, actual = sample()
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual))
    for citation in [actual['answer']['citations'][0], actual['search']['citations'][0]]:
        citation[field] = value
    results = {row['id']:row for row in gate.compare(manifest, expected, actual)}
    assert not results['answer']['passed'] and not results['search']['passed']


@pytest.mark.parametrize('field', ['unit', 'location', 'start', 'end'])
def test_document_citation_locator_fields_are_required(field):
    manifest, expected, actual = sample()
    for citation in [actual['answer']['citations'][0], actual['search']['citations'][0]]:
        del citation[field]
    results = {row['id']:row for row in gate.compare(manifest, expected, actual)}
    assert not results['answer']['passed'] and not results['search']['passed']


@pytest.mark.parametrize('observation', [None, {}, {'pages':True, 'text':'The parcel is blue.'},
    {'pages':2, 'text':'The parcel is blue.'}, {'pages':1, 'text':'Different synthetic page.'}])
def test_document_citation_needs_matching_observed_source(observation):
    manifest, expected, actual = sample()
    actual['pdf'] = observation
    results = {row['id']:row for row in gate.compare(manifest, expected, actual)}
    assert not results['answer']['passed'] and not results['search']['passed']


def test_distinct_claims_can_share_a_valid_source_passage():
    manifest, expected, actual = sample()
    answer = actual['answer']
    citation = answer['citations'][0]
    answer['claims'].append({'text':'parcel is blue', 'citations':[copy.deepcopy(citation)]})
    answer['text'] = ' '.join(claim['text'] for claim in answer['claims'])
    answer['citations'] = [c for claim in answer['claims'] for c in claim['citations']]
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual))


@pytest.mark.parametrize('duplicate', ['citation', 'claim-reordered', 'claim-whitespace', 'aggregate-only', 'none'])
def test_answer_uniqueness_matches_product_text_and_evidence_set(duplicate):
    manifest, expected, actual = sample()
    manifest['documents'].append({'id':'pdf-other', 'ocr':False})
    expected['documents']['pdf-other'] = copy.deepcopy(expected['documents']['pdf'])
    other = copy.deepcopy(actual['answer']['citations'][0]); other['document_id'] = 'pdf-other'
    seed_document_support(actual, other)
    expected['answers']['answer']['citations'].append({'document_id':'pdf-other', 'span':'parcel is blue'})
    answer = actual['answer']
    claim = answer['claims'][0]
    claim['citations'].append(other)
    answer['citations'] = list(claim['citations'])
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual))
    if duplicate == 'citation':
        extra = copy.deepcopy(claim['citations'][0]); extra['irrelevant_annotation'] = 'synthetic'
        claim['citations'].append(extra)
    elif duplicate.startswith('claim-'):
        extra = copy.deepcopy(claim)
        if duplicate == 'claim-reordered': extra['citations'].reverse()
        else: extra['text'] = '  The  parcel is blue.  '
        answer['claims'].append(extra)
    elif duplicate == 'none':
        # The product permits identical wording supported by a distinct passage set.
        answer['claims'] = [{'text':claim['text'], 'citations':[citation]} for citation in claim['citations']]
    answer['text'] = ' '.join(c['text'] for c in answer['claims'])
    answer['citations'] = [c for item in answer['claims'] for c in item['citations']]
    if duplicate == 'aggregate-only': answer['citations'].append(copy.deepcopy(other))
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is (duplicate == 'none')
    if duplicate in {'citation', 'claim-reordered', 'claim-whitespace'}:
        assert any('duplicate' in line for line in result['diff'])


@pytest.mark.parametrize('start,end,label', [
    (0, 5615, '00:00–00:05'), (2040, 4480, '00:02–00:04'),
    (999, 1001, '00:00–00:01'), (59999, 60001, '00:59–01:00'),
    (3599999, 3600123, '59:59–01:00:00'),
])
def test_transcript_display_label_uses_product_floor_and_hour_contract(start, end, label):
    assert gate.transcript_location(start, end) == label


@pytest.mark.parametrize('field,value', [
    ('location', None), ('location', False), ('location', []), ('location', {}),
    ('location', '00:00–00:03'), ('location', '00:00—00:02'), ('location', '00:00-00:02'),
    ('location', '0–2000'), ('location', '00:00:00,000–00:00:02,000'), ('location', '00:00–00:02 '),
    ('start', None), ('start', False), ('start', 0.0), ('start', '0'), ('start', -1), ('start', 1),
    ('end', None), ('end', True), ('end', 19.0), ('end', '19'), ('end', 0), ('end', 99999),
])
def test_transcript_display_and_excerpt_range_require_exact_validated_values(field, value):
    manifest, expected, actual = sample()
    citation = actual['answer']['citations'][0]
    citation.update(document_id='wav', evidence_kind='transcript')
    seed_audio_support(expected, actual, citation)
    expected['answers']['answer']['citations'][0]['document_id'] = 'wav'
    actual['answer']['text'] = actual['answer']['claims'][0]['text'] = gate.TRANSCRIPT_QUALIFIER + citation['excerpt']
    expected['search']['search']['document_ids'] = ['wav']
    expected['search']['search']['citations'][0]['document_id'] = 'wav'
    actual['search'] = {'document_ids':['wav'], 'citations':[copy.deepcopy(citation)]}
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual))
    for observed in [citation, actual['search']['citations'][0]]:
        if value is None: del observed[field]
        else: observed[field] = value
    results = {row['id']:row for row in gate.compare(manifest, expected, actual)}
    assert not results['answer']['passed'] and not results['search']['passed']


@pytest.mark.parametrize('source', ['pdf', 'wav'])
@pytest.mark.parametrize('state', ['ready', None, 'processing', 'needs_ocr', 'failed', '', True, 'READY'])
def test_every_graded_source_and_its_citations_require_ready_state(source, state):
    manifest, expected, actual = sample()
    if source == 'wav':
        citation = actual['answer']['citations'][0]
        citation.update(document_id='wav', evidence_kind='transcript')
        seed_audio_support(expected, actual, citation)
        expected['answers']['answer']['citations'][0]['document_id'] = 'wav'
        actual['answer']['text'] = actual['answer']['claims'][0]['text'] = gate.TRANSCRIPT_QUALIFIER + citation['excerpt']
        expected['search']['search']['document_ids'] = ['wav']
        expected['search']['search']['citations'][0]['document_id'] = 'wav'
        actual['search'] = {'document_ids':['wav'], 'citations':[copy.deepcopy(citation)]}
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual, full=True))
    if state is None: del actual[source]['state']
    else: actual[source]['state'] = state
    for full in (False, True):
        results = {row['id']:row for row in gate.compare(manifest, expected, actual, full=full)}
        for key in (source, 'search', 'answer'):
            assert results[key]['passed'] is (state == 'ready'), results[key]


@pytest.mark.parametrize('state', [None, 'processing', 'needs_ocr', 'failed'])
def test_full_ocr_text_and_page_success_cannot_hide_nonready_source(state):
    manifest, expected, actual = sample()
    manifest['documents'][0]['ocr'] = True
    actual['pdf']['ocr_pages'] = 1
    if state is None: del actual['pdf']['state']
    else: actual['pdf']['state'] = state
    results = {row['id']:row for row in gate.compare(manifest, expected, actual, full=True)}
    assert not results['pdf']['passed']
    assert any('expected ready source' in line for line in results['pdf']['diff'])


@pytest.mark.parametrize('adapter', ['answer', 'retrieval'])
@pytest.mark.parametrize('mutation', ['provider', 'model', 'missing-provider', 'missing-model',
    'missing-record', 'null-record', 'nonmapping-record', 'revision', 'null-revision', 'extra-field'])
def test_fixed_adapter_manifest_contract_rejects_unexecuted_identity(tmp_path, adapter, mutation):
    root = tmp_path/'corpus'; copy_corpus(root)
    path = root/'manifest.json'; manifest = json.loads(path.read_text())
    record = manifest['models'][adapter]
    if mutation == 'provider': record['provider'] = 'Synthetic unexecuted provider'
    elif mutation == 'model': record['model'] = 'synthetic/unexecuted-model'
    elif mutation == 'missing-provider': del record['provider']
    elif mutation == 'missing-model': del record['model']
    elif mutation == 'missing-record': del manifest['models'][adapter]
    elif mutation == 'null-record': manifest['models'][adapter] = None
    elif mutation == 'nonmapping-record': manifest['models'][adapter] = []
    elif mutation == 'revision': record['revision'] = 'a'*40
    elif mutation == 'null-revision': record['revision'] = None
    else: record['unexecuted_setting'] = True
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=adapter + '.*fixed adapter contract'):
        gate.load_corpus(root)


@pytest.mark.parametrize('adapter,field,value', [
    ('answer', 'provider', 'Synthetic unexecuted generator'),
    ('retrieval', 'model', 'synthetic/unexecuted-embedding'),
])
def test_fixed_adapter_receipt_cannot_attest_to_unexecuted_metadata(tmp_path, monkeypatch, capsys, adapter, field, value):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    root = tmp_path/'corpus'; copy_corpus(root)
    path = root/'manifest.json'; manifest = json.loads(path.read_text())
    manifest['models'][adapter][field] = value
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(manifest))
    report = tmp_path/'receipt.json'
    assert gate.main(['--corpus', str(root), '--report', str(report)]) == 1
    assert not report.exists()
    assert adapter + ' identity must match the fixed adapter contract' in capsys.readouterr().err


def test_fixed_adapter_receipt_preserves_actual_fast_contract(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    manifest, _ = gate.load_corpus(gate.CORPUS)
    fixed = {
        'answer':{'provider':'CorpusExtractiveGenerator + ExtractiveAnswerProvider', 'model':None},
        'retrieval':{'provider':'product lexical', 'model':None},
    }
    for adapter, record in fixed.items():
        assert manifest['models'][adapter] == record
    report = tmp_path/'receipt.json'
    assert gate.main(['--report', str(report)]) == 0
    receipt = json.loads(report.read_text())
    assert receipt['passed'] is True and receipt['mode'] == 'fast-contract'
    for adapter, record in fixed.items():
        assert receipt['models'][adapter] == record


@pytest.mark.parametrize('record', [{'provider': 'not-run', 'model': 'mutable'}, None, []])
def test_receipt_rejects_extra_top_level_model_records(tmp_path, monkeypatch, capsys, record):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    root = tmp_path / 'corpus'
    copy_corpus(root)
    path = root / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['models']['unexecuted'] = record
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(manifest))
    report = tmp_path / 'receipt.json'
    assert gate.main(['--corpus', str(root), '--report', str(report)]) == 1
    assert not report.exists()
    assert 'exact reviewed model inventory' in capsys.readouterr().err


@pytest.mark.parametrize('group,identity', [
    ('documents', 'scan-en'), ('documents', 'scan-es'),
    ('search', 'search-bicycle'), ('search', 'search-gate'),
    ('search', 'search-scan-en'), ('search', 'search-scan-es'),
])
def test_versioned_inventory_rejects_paired_manifest_and_gold_removal(tmp_path, group, identity):
    root = tmp_path / 'corpus'
    copy_corpus(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    expected = json.loads((root / 'expected.json').read_text())
    manifest[group] = [row for row in manifest[group] if row['id'] != identity]
    del expected[group][identity]
    if group == 'documents':
        search_id = 'search-' + identity
        manifest['search'] = [row for row in manifest['search'] if row['id'] != search_id]
        del expected['search'][search_id]
    elif identity.startswith('search-scan-'):
        # Keep discovery coverage valid so the old minimum-count check accepts it.
        source = identity.removeprefix('search-')
        expected['search']['search-locker']['document_ids'].append(source)
        expected['search']['search-locker']['citations'].append({
            'document_id': source, 'span': expected['documents'][source]['text']})
        next(row for row in manifest['search'] if row['id'] == 'search-locker')['full_only'] = True
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    (root / 'manifest.json').write_text(json.dumps(manifest))
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    (root / 'expected.json').write_text(json.dumps(expected))
    with pytest.raises(ValueError, match='exact reviewed v1 case inventory'):
        gate.load_corpus(root)


@pytest.mark.parametrize('group', ['documents', 'audio', 'search', 'answers'])
def test_versioned_inventory_rejects_paired_case_rename(tmp_path, group):
    root = tmp_path / 'corpus'
    copy_corpus(root)
    manifest = json.loads((root / 'manifest.json').read_text())
    expected = json.loads((root / 'expected.json').read_text())
    old = manifest[group][0]['id']
    new = old + '-replacement'
    manifest[group][0]['id'] = new
    expected[group][new] = expected[group].pop(old)
    for section in ('search', 'answers'):
        for case in expected[section].values():
            if 'document_ids' in case:
                case['document_ids'] = [new if key == old else key for key in case['document_ids']]
            for citation in case['citations']:
                if citation['document_id'] == old:
                    citation['document_id'] = new
    if old in manifest['fast_transcripts']:
        manifest['fast_transcripts'][new] = manifest['fast_transcripts'].pop(old)
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    (root / 'manifest.json').write_text(json.dumps(manifest))
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    (root / 'expected.json').write_text(json.dumps(expected))
    with pytest.raises(ValueError, match='exact reviewed v1 case inventory'):
        gate.load_corpus(root)


@pytest.mark.parametrize('kind', [None, 'not-supported', '', True, 1, [], {}, 'Generated', 'generated'])
def test_answer_kind_matches_rendered_generated_branch(kind):
    manifest, expected, actual = sample()
    if kind is not None:
        actual['answer']['kind'] = kind
    else:
        actual['answer'].pop('kind', None)
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is (kind == 'generated')
    if kind != 'generated':
        assert any('expected generated answer' in line for line in result['diff'])


@pytest.mark.parametrize('field,value', [
    ('unexecuted_setting', True), ('runtime', 'synthetic-runtime==1'), ('compute_type', 'float32'),
    ('sha256', {'model.bin': '0' * 64}),
])
def test_asr_receipt_identity_rejects_unexecuted_settings(tmp_path, field, value):
    root = tmp_path / 'corpus'
    copy_corpus(root)
    path = root / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['models']['asr'][field] = value
    # fixture-write: Deliberately mutate only the temporary synthetic corpus copy.
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='complete reviewed corpus ASR record'):
        gate.load_corpus(root)


@pytest.mark.parametrize('excess', ['citations', 'sources', 'both'])
def test_search_rejects_unsliced_results_exceeding_top_k(excess):
    manifest, expected, actual = sample()
    search = actual['search']
    if excess in {'citations', 'both'}:
        search['citations'].append(copy.deepcopy(search['citations'][0]))
    if excess in {'sources', 'both'}:
        search['document_ids'].append('wav')
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'search')
    assert result['passed'] is False
    assert any('top-k' in line for line in result['diff'])
