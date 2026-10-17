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
