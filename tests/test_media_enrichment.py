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


@pytest.fixture
def configured_translation(monkeypatch, tmp_path):
    from case_intelligence.external_model_grants import configured_model_destination
    from case_intelligence.workspace_store import WorkspaceStore

    monkeypatch.setenv("CASE_INTELLIGENCE_TRANSLATION_URL", "https://translate.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_TRANSLATION_MODEL", "synthetic-translation")
    workspace = WorkspaceStore(tmp_path / "workspace.sqlite")
    owner = "principal-owner"
    workspace.upsert_principal("test", "owner", "Owner", "owner", preferred_principal_id=owner)
    matter = workspace.create_matter("Synthetic one", "", owner).matter_id
    other = workspace.create_matter("Synthetic two", "", owner).matter_id
    destination = configured_model_destination("translation")
    workspace.set_model_grant(matter, destination, owner, approved=True)
    try:
        yield workspace, owner, matter, other, destination
    finally:
        workspace.close()


def test_translation_checks_current_permission_and_never_sends_internal_fields(monkeypatch, configured_translation):
    from io import BytesIO
    import json
    from types import SimpleNamespace

    workspace, owner, matter, other, dest = configured_translation
    original = {"segment_id": "private-original-id", "model_text": "Prueba.", "speaker": {"display_name": "Private label"},
                "start": 0.0, "end": 1.7}
    payload = {"segments": [original], "quality": {"detected_language": "es"}, "job_id": "private-job"}
    calls = []

    def respond(request, **kwargs):
        calls.append((request.full_url, json.loads(request.data)))
        return BytesIO(b'{"segments":[{"id":"segment-1","text":"A test."}]}')

    monkeypatch.setattr(enrichment, "open_generator_request", respond)

    def run(matter_id, check_access=lambda: None):
        job = SimpleNamespace(matter_id=matter_id, duration_ms=2000)
        return enrichment.enrich_transcript(workspace, job, None, payload, check_access=check_access)

    assert run(other)["quality"]["translation_status"] == "denied"
    translated = run(matter)
    assert calls == [("https://translate.example.test/v1/translations", {
        "model": "synthetic-translation", "source_language": "es", "target_language": "en",
        "segments": [{"id": "segment-1", "text": "Prueba."}],
    })]
    assert translated["segments"] == [original | {"translated_text": "A test."}]
    assert translated["provenance"]["translation"] == {"endpoint": dest.endpoint, "model": dest.model}
    assert not translated["degraded"]
    checks = []

    def revoke_at_dispatch():
        checks.append(True)
        if len(checks) == 2:
            workspace.set_model_grant(matter, dest, owner, approved=False)

    denied = run(matter, revoke_at_dispatch)
    assert len(checks) == 2 and len(calls) == 1
    assert denied["degraded"] and denied["quality"]["translation_status"] == "denied"
    assert denied["segments"] == [original]
    assert payload["segments"] == [original]


@pytest.mark.parametrize("metadata", [{"language": "English"}, {"quality": {"detected_language": "en-US"}},
                                      {"review_state": "Human reviewed"}])
def test_english_and_reviewed_sources_skip_translation(monkeypatch, configured_translation, metadata):
    from types import SimpleNamespace

    workspace, _, matter, _, _ = configured_translation
    monkeypatch.setattr(enrichment, "open_generator_request", lambda *a, **kw: pytest.fail("unexpected dispatch"))
    payload = {"segments": source_segments(), **metadata}
    result = enrichment.enrich_transcript(workspace, SimpleNamespace(matter_id=matter), None, payload,
                                          check_access=lambda: None)
    assert result["segments"] == payload["segments"]
