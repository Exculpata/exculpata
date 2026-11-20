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


def test_completed_private_alignment_uses_existing_segment_boundaries(monkeypatch):
    monkeypatch.setenv("CASE_INTELLIGENCE_ALIGNMENT_URL", "https://alignment.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_ALIGNMENT_MODEL", "synthetic-aligner")
    payload = {"quality": {"source_alignment_status": "succeeded"}, "segments": source_segments()}
    result = enrichment.enrich_transcript(None, None, None, payload, check_access=lambda: pytest.fail("dispatch"))
    assert result["quality"]["alignment_status"] == "completed_locally"
    assert result["segments"] == payload["segments"]


def test_segment_request_limit_counts_encoded_bytes(monkeypatch):
    monkeypatch.setattr(enrichment, "_LIMIT", 8)
    assert enrichment._serialized("á") == '"\\u00e1"'
    with pytest.raises(enrichment.MediaContractError, match="request limit"):
        enrichment._serialized(["á"])


def test_review_shows_machine_translation_beside_the_cited_source_after_an_edit(tmp_path):
    import re
    from fastapi.testclient import TestClient
    from tests.test_matter_media_workflow import ImmediateMediaProcessor, _matter, _upload_and_wait
    from tests.test_media_preflight import app_for

    class TranslatedRecording(ImmediateMediaProcessor):
        def transcript(self, owner, external_job_id):
            payload = super().transcript(owner, external_job_id)
            payload["segments"][0].update(text="La caja roja.", model_text="La caja roja.",
                                           translated_text="The red box.")
            return payload

    with TestClient(app_for(tmp_path / "runtime", TranslatedRecording(), background=True)) as client:
        slug = _matter(client)
        _, token = _upload_and_wait(client, slug)
        path = f"/matters/{slug}/sources/{token}"
        def passage(ordinal):
            page = client.get(path)
            assert page.status_code == 200
            return page.text.split(f'<article id="segment-{ordinal}"', 1)[1].split("</article>", 1)[0]

        first = passage(1)
        assert "English machine translation of the original transcript" in first
        assert '<span lang="en">The red box.</span>' in first
        assert "<p>La caja roja.</p>" in first
        assert "transcript-translation" not in passage(2)
        segment_id = re.search(r'data-segment-id="([^"]+)"', first).group(1)
        timed_anchor = re.search(r'data-start-ms="[^"]+" data-end-ms="[^"]+"', first).group()
        edit = client.post(f"{path}/segments/{segment_id}",
                           data={"expected_revision": 0, "text": "La caja es roja."}, follow_redirects=False)
        assert edit.status_code == 303
        reviewed = passage(1)
        assert f'data-segment-id="{segment_id}"' in reviewed and timed_anchor in reviewed
        assert "<p>La caja es roja.</p>" in reviewed
        assert '<span lang="en">The red box.</span>' in reviewed
