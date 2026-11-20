"""Synthetic evidence invariants for the optional speech contracts."""

import pytest

from case_intelligence import media_enrichment as enrichment


def source_segments():
    return [
        {"id": "segment-1", "start": 0, "end": 2, "text": "Hola mundo."},
        {"id": "segment-2", "start": 1.8, "end": 4, "text": "Otra prueba."},
    ]


def alignment():
    return {"segments": [
        {"id": "segment-1", "words": [
            {"word": "Hola", "start": 0.1, "end": 0.5}, {"word": " mundo.", "start": 0.5, "end": 1.5}]},
        {"id": "segment-2", "words": [{"word": "Otra prueba.", "start": 1.9, "end": 3.8}]},
    ]}


def test_aligned_boundaries_preserve_source_text_and_identity():
    source = source_segments()
    adjusted = enrichment.apply_contract("alignment", source, alignment())
    assert adjusted == [source[0] | {"start": 0.1, "end": 1.5}, source[1] | {"start": 1.9, "end": 3.8}]
    assert source == source_segments()


@pytest.mark.parametrize("field,value", [
    ("word", "adios"), ("start", -1), ("start", True), ("end", 7), ("end", float("nan")),
    ("end", 0.7), ("end", 0.10001), ("start", 10**500),
])
def test_alignment_rejects_wrong_words_or_times_without_partial_edits(field, value):
    source = source_segments()
    bad = alignment()
    bad["segments"][0]["words"][0][field] = value
    with pytest.raises(enrichment.MediaContractError, match="response"):
        enrichment.apply_contract("alignment", source, bad)
    assert source == source_segments()


@pytest.mark.parametrize("capability,value", [
    ("translation", {"text": "Hello world."}),
    ("speaker_labeling", {"speaker": "SPEAKER_01"}),
    ("alignment", {"words": [{"word": "Hola mundo.", "start": 0, "end": 1}]}),
])
def test_ids_and_completeness_bind_all_answers(capability, value):
    source = source_segments()
    for ids in ([], ["segment-1"], ["segment-1", "segment-1"], ["segment-2", "segment-1"]):
        response = {"segments": [{"id": identity, **value} for identity in ids]}
        with pytest.raises(enrichment.MediaContractError):
            enrichment.apply_contract(capability, source, response)
    assert source == source_segments()


@pytest.mark.parametrize("capability,field,invalid", [
    ("translation", "text", " "), ("translation", "text", "two\x00lines"),
    ("translation", "text", ["x"]), ("translation", "text", "x" * 20001),
    ("speaker_labeling", "speaker", "Synthetic person's name"), ("speaker_labeling", "speaker", None),
])
def test_invalid_translation_and_speaker_are_rejected(capability, field, invalid):
    with pytest.raises(enrichment.MediaContractError):
        enrichment.apply_contract(capability, source_segments()[:1], {"segments": [{"id": "segment-1", field: invalid}]})


def test_results_apply_only_the_requested_annotation():
    source = source_segments()[:1]
    response = {"segments": [{"id": "segment-1", "speaker": "SPEAKER_01", "text": "Hello world.", "start": 455}]}
    assert enrichment.apply_contract("translation", source, response) == [source[0] | {"translated_text": "Hello world."}]
    assert enrichment.apply_contract("speaker_labeling", source, response) == [
        source[0] | {"speaker": {"cluster_id": "SPEAKER_01"}}]
