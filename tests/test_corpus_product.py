"""Tiny synthetic inputs exercise the corpus adapter without model downloads."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import wave

import pytest
from case_intelligence.generation import EvidenceItem
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


SPEC = importlib.util.spec_from_file_location(
    "corpus_product", Path(__file__).parents[1] / "scripts" / "corpus_product.py")
product = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(product)


def _inputs(root):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })}),
    })
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 16 Tf 45 700 Td (Synthetic blue bicycle arrived at Cedar Gate.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(root / "record.pdf")
    with wave.open(str(root / "record.wav"), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 16000)
    return {
        "synthetic": True,
        "documents": [{"id": "written", "path": "record.pdf", "ocr": False}],
        "audio": [{"id": "spoken", "path": "record.wav"}],
        "fast_transcripts": {"spoken": [{"start": 0, "end": 2,
            "text": "Synthetic orange parcel was stored in locker seven.", "speaker": "SPEAKER_00"}]},
        "search": [{"id": "search", "query": "orange parcel", "top_k": 1}],
        "answers": [{"id": "answer", "question": 'What does the recording say about "orange parcel"?'}],
    }


def test_fast_native_ingestion_and_transcript_citations_use_product_paths(tmp_path):
    manifest = _inputs(tmp_path)
    # Deliberately contradictory expected material must never influence observation.
    (tmp_path / "expected.json").write_text('{"answer":"invented green suitcase"}')
    result = product.observe(tmp_path, manifest)
    assert result["written"]["state"] == "ready"
    assert "blue bicycle" in result["written"]["text"]
    assert result["spoken"]["mode"] == "prepared-transcript-fixture"
    assert result["search"]["document_ids"] == ["spoken"]
    answer = result["answer"]
    assert answer["route"] == "question"
    assert answer["dispatched"] is True
    assert answer["answerable"] is True
    assert "orange parcel" in answer["text"]
    assert "green suitcase" not in answer["text"]
    assert answer["provider"] == "CorpusExtractiveGenerator"
    for citation in answer["citations"] + result["search"]["citations"]:
        assert citation["document_id"] == "spoken"
        assert citation["source_valid"] is True
        assert citation["evidence_kind"] == "transcript"
        assert (citation["start_ms"], citation["end_ms"]) == (0, 2000)
        assert citation["excerpt"][citation["start"]:citation["end"]] == citation["excerpt"]


@pytest.mark.parametrize("question", [
    "Where was the orange parcel stored?",
    'What does the recording say about "orange parcel"?',
])
def test_fixture_generator_does_not_turn_unrelated_retrieval_hits_into_claims(question):
    evidence = (
        EvidenceItem("S1", "Synthetic note", "Page 1", "The blue bicycle arrived at Cedar Gate."),
        EvidenceItem("S2", "Synthetic clip", "00:00–00:02", "The orange parcel was stored in locker seven.", "transcript"),
    )
    raw = product.CorpusExtractiveGenerator().generate(question=question, evidence=evidence)
    assert len(raw["claims"]) == 1
    assert raw["claims"][0]["evidence_ids"] == ["S2"]
    assert raw["claims"][0]["text"].startswith("The machine transcript appears to say that ")


def test_full_mode_never_substitutes_prepared_transcript_expectations(tmp_path):
    with pytest.raises(ValueError, match="independently observed transcripts"):
        product.observe(tmp_path, _inputs(tmp_path), full=True)


def test_adapter_refuses_configured_external_database_before_creating_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_POSTGRES_DSN", "synthetic-connection-sentinel")
    with pytest.raises(ValueError, match="isolated environment"):
        product.observe(tmp_path, {"synthetic": True})


def test_adapter_rejects_non_synthetic_inputs_and_path_escape(tmp_path):
    with pytest.raises(ValueError, match="explicitly synthetic"):
        product.observe(tmp_path, {"synthetic": False})
    outside = tmp_path / "outside.txt"
    outside.write_text("Synthetic path boundary")
    root = tmp_path / "corpus"
    root.mkdir()
    with pytest.raises(ValueError, match="inside the corpus root"):
        product._input_path(root, "../outside.txt")


def test_real_dispatcher_wrong_route_cannot_be_masked_by_direct_answer(tmp_path, monkeypatch):
    from case_intelligence import ask_router
    manifest = _inputs(tmp_path)
    monkeypatch.setattr(ask_router, 'classify', lambda text: ask_router.RouteDecision('exact', 'Synthetic wrong route'))
    result = product.observe(tmp_path, manifest)
    assert result['answer']['answerable'] is True
    assert result['answer']['route'] == 'exact'
    assert result['answer']['dispatched'] is False


def test_corpus_citations_accept_observed_spoken_numeral_without_inference_from_gold():
    import copy
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest, expected = gate.load_corpus(gate.CORPUS)
    transcripts = copy.deepcopy(manifest['fast_transcripts'])
    transcripts['audio-parcel'] = [
        {'start':0, 'end':2.04, 'text':'This is a synthetic recording.', 'speaker':'SPEAKER_00'},
        {'start':2.04, 'end':4.48, 'text':'The orange parcel was stored in Locker 7.', 'speaker':'SPEAKER_00'},
    ]
    actual = product.observe(gate.CORPUS, manifest, observed_transcripts=transcripts)
    assert 'Locker 7.' in actual['audio-parcel']['text']
    assert 'search-scan-en' not in actual and 'search-scan-es' not in actual
    results = gate.compare(manifest, expected, actual)
    assert all(row['passed'] for row in results), results


def test_manual_media_seed_holds_owning_lock_across_all_public_steps(tmp_path, monkeypatch):
    from case_intelligence.workspace_store import WorkspaceStore

    import threading

    seed_thread = threading.current_thread()
    seen = []
    for name in ('queue_media_job', 'claim_media_job', 'import_media_transcript', 'finish_media_job'):
        original = getattr(WorkspaceStore, name)

        def observed(workspace, *args, _original=original, _name=name, **kwargs):
            if threading.current_thread() is seed_thread:
                assert workspace._lock._is_owned(), f'{_name} escaped the media fixture scope'
                seen.append(_name)
            return _original(workspace, *args, **kwargs)

        monkeypatch.setattr(WorkspaceStore, name, observed)
    result = product.observe(tmp_path, _inputs(tmp_path))
    assert seen == ['queue_media_job', 'claim_media_job', 'import_media_transcript', 'finish_media_job']
    assert result['spoken']['mode'] == 'prepared-transcript-fixture'
    assert result['answer']['answerable'] is True
    assert all(citation['source_valid'] for citation in result['answer']['citations'])


def test_observation_preserves_wrong_claim_attachment_for_gate_rejection(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest = _inputs(tmp_path)
    manifest['answers'][0]['question'] = 'What about the blue bicycle and orange parcel?'
    original = product.CaseIntelligenceWorkbench.ask
    observed_claims = []

    def swapped(bench, *args, **kwargs):
        answer = original(bench, *args, **kwargs)
        claims = answer.payload['claims']
        assert len(claims) == 2
        assert claims[0]['citations'][0]['document_id'] != claims[1]['citations'][0]['document_id']
        claims[0]['citations'], claims[1]['citations'] = claims[1]['citations'], claims[0]['citations']
        observed_claims.extend(claims)
        return answer

    monkeypatch.setattr(product.CaseIntelligenceWorkbench, 'ask', swapped)
    actual = product.observe(tmp_path, manifest)
    expected = {'documents':{'written':{'text':actual['written']['text'], 'cer':0, 'pages':1}},
        'audio':{'spoken':{'text':actual['spoken']['text'], 'wer':0.15,
            'locator':{'start_ms':0, 'end_ms':2000, 'tolerance_ms':500},
            'layouts':[[{'text':actual['spoken']['text'], 'start_ms':0, 'end_ms':2000}]]}},
        'search':{'search':{'document_ids':['spoken'],
            'citations':[{'document_id':'spoken', 'span':'orange parcel'}]}},
        'answers':{'answer':{'facts':['blue bicycle', 'orange parcel'], 'citations':[
            {'document_id':'written', 'span':'blue bicycle'},
            {'document_id':'spoken', 'span':'orange parcel'}]}}}
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer')
    assert result['passed'] is False
    assert [claim['text'] for claim in actual['answer']['claims']] == [claim['text'] for claim in observed_claims]
    for claim in actual['answer']['claims']:
        assert all(citation['source_valid'] for citation in claim['citations'])
        if 'blue bicycle' in claim['text']:
            assert [citation['document_id'] for citation in claim['citations']] == ['spoken']
        else:
            assert [citation['document_id'] for citation in claim['citations']] == ['written']


def test_actual_corpus_shared_facts_cannot_hide_swapped_full_claim_support():
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest, expected = gate.load_corpus(gate.CORPUS)
    actual = product.observe(gate.CORPUS, manifest)
    positive = gate.compare(manifest, expected, actual)
    assert len(positive) == 14 and all(row['passed'] for row in positive), positive
    answer = actual['answer-bicycle']
    first, second = answer['claims']
    assert {first['citations'][0]['document_id'], second['citations'][0]['document_id']} == {
        'bicycle', 'audio-bicycle'}
    for claim in (first, second):
        assert all(gate.contains_tokens(claim['text'], fact)
                   for fact in expected['answers']['answer-bicycle']['facts'])
    first['citations'], second['citations'] = second['citations'], first['citations']
    answer['citations'] = [citation for claim in answer['claims'] for citation in claim['citations']]
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer-bicycle')
    assert result['passed'] is False
    assert any('complete claim' in difference for difference in result['diff'])


@pytest.mark.parametrize('kind', ['document', None], ids=['mislabeled', 'missing'])
def test_actual_spoken_source_kind_cannot_remove_transcript_attribution(kind):
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest, expected = gate.load_corpus(gate.CORPUS)
    actual = product.observe(gate.CORPUS, manifest)
    positive = gate.compare(manifest, expected, actual)
    assert len(positive) == 14 and all(row['passed'] for row in positive), positive
    answer = actual['answer-spoken']
    assert len(answer['claims']) == 1
    claim = answer['claims'][0]
    assert claim['text'].startswith(gate.TRANSCRIPT_QUALIFIER)
    assert {citation['document_id'] for citation in claim['citations']} == {'audio-bicycle'}
    claim['text'] = claim['text'][len(gate.TRANSCRIPT_QUALIFIER):]
    for citation in claim['citations']:
        assert citation['evidence_kind'] == 'transcript'
        if kind is None:
            del citation['evidence_kind']
        else:
            citation['evidence_kind'] = kind
    answer['text'] = claim['text']
    answer['citations'] = claim['citations']
    result = next(row for row in gate.compare(manifest, expected, actual) if row['id'] == 'answer-spoken')
    assert result['passed'] is False
    assert any('complete claim' in difference for difference in result['diff'])


@pytest.mark.parametrize('mutation,target', [
    ('pages', 'bicycle'), ('collapsed-time', 'audio-bicycle'), ('wrong-search-passage', 'search-gate'),
])
def test_actual_corpus_rejects_page_timing_and_search_passage_regressions(mutation, target):
    import copy
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest, expected = gate.load_corpus(gate.CORPUS)
    transcripts = copy.deepcopy(manifest['fast_transcripts'])
    if mutation == 'collapsed-time':
        for segment in transcripts['audio-bicycle']:
            segment.update(start=1.0, end=1.1)
    actual = product.observe(gate.CORPUS, manifest, observed_transcripts=transcripts)
    if mutation == 'pages':
        assert actual['bicycle']['pages'] == 1
        actual['bicycle']['pages'] = 2
    elif mutation == 'wrong-search-passage':
        citation = next(c for c in actual['search-gate']['citations'] if c['document_id'] == 'audio-bicycle')
        assert citation['source_valid'] is True
        citation['excerpt'] = 'This is a synthetic recording.'
    results = gate.compare(manifest, expected, actual)
    assert any(row['id'] == target and not row['passed'] for row in results), results


@pytest.mark.parametrize('mutation', ['collapsed', 'shifted', 'missing', 'boolean', 'float', 'boolean-unit', 'different-unit'])
def test_actual_audio_citation_must_match_reviewed_observed_unit(mutation):
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest, expected = gate.load_corpus(gate.CORPUS)
    actual = product.observe(gate.CORPUS, manifest)
    assert all(row['passed'] for row in gate.compare(manifest, expected, actual))
    for key in ('search-gate', 'answer-spoken'):
        citation = next(c for c in actual[key]['citations'] if c['document_id'] == 'audio-bicycle')
        if mutation == 'collapsed': citation.update(start_ms=1000, end_ms=1100)
        elif mutation == 'shifted': citation.update(start_ms=1000, end_ms=6615)
        elif mutation == 'missing': del citation['start_ms']
        elif mutation == 'boolean': citation['start_ms'] = False
        elif mutation == 'float': citation['end_ms'] = 5615.0
        elif mutation == 'boolean-unit': citation['unit'] = True
        else: citation['unit'] += 1
    results = gate.compare(manifest, expected, actual)
    assert all(not row['passed'] for row in results if row['id'] in ('search-gate', 'answer-spoken'))


@pytest.mark.parametrize('mutation', ['collapsed-fact', 'swapped-text', 'shifted-fact'])
def test_actual_split_audio_requires_each_reviewed_passage_interval(mutation):
    import copy
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest, expected = gate.load_corpus(gate.CORPUS)
    transcripts = copy.deepcopy(manifest['fast_transcripts'])
    segments = [
        {'start':0, 'end':2.04, 'text':'This is a synthetic recording.', 'speaker':'SPEAKER_00'},
        {'start':2.04, 'end':4.48, 'text':'The orange parcel was stored in Locker 7.', 'speaker':'SPEAKER_00'},
    ]
    if mutation == 'collapsed-fact': segments[1].update(start=4.4, end=4.48)
    elif mutation == 'swapped-text': segments[0]['text'], segments[1]['text'] = segments[1]['text'], segments[0]['text']
    else: segments[1].update(start=3.04, end=5.48)
    transcripts['audio-parcel'] = segments
    actual = product.observe(gate.CORPUS, manifest, observed_transcripts=transcripts)
    results = gate.compare(manifest, expected, actual)
    assert all(not row['passed'] for row in results if row['id'] in ('audio-parcel', 'answer-parcel', 'search-parcel'))


def test_observed_bicycle_numeral_and_speech_endpoint_fit_source_signal_layout():
    import copy
    spec = importlib.util.spec_from_file_location('corpus_check', Path(__file__).parents[1]/'scripts/corpus_check.py')
    gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
    manifest, expected = gate.load_corpus(gate.CORPUS)
    transcripts = copy.deepcopy(manifest['fast_transcripts'])
    transcripts['audio-bicycle'] = [{'start':0, 'end':5.08, 'speaker':'SPEAKER_00',
        'text':'This is a synthetic recording. The blue bicycle arrived at Cedar Gate at 9.15.'}]
    actual = product.observe(gate.CORPUS, manifest, observed_transcripts=transcripts)
    results = gate.compare(manifest, expected, actual)
    assert all(row['passed'] for row in results), results
    assert actual['audio-bicycle']['units'][0]['end_ms'] == 5080
    assert gate.error_rate(gate.normalize(expected['audio']['audio-bicycle']['text']).split(),
                           gate.normalize(transcripts['audio-bicycle'][0]['text']).split()) == pytest.approx(2/15)
