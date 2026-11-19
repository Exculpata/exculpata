"""Exercise grant checks at the external case-data generation boundary."""

import io
import json
from types import SimpleNamespace

import pytest

from case_intelligence import generation, generator_transport
from case_intelligence.generation_errors import GenerationUnavailable


ORIGIN = "https://answers.example.test"
MODEL = "synthetic-answer"


def test_external_case_request_without_matter_stops_before_transport(monkeypatch):
    requests = []

    def receive(request, **_):
        requests.append(request)
        return io.BytesIO(b'{}')

    monkeypatch.setattr(generator_transport, "open_generator_request", receive)
    with pytest.raises(GenerationUnavailable, match="matter approval"):
        generation._bounded_json_request(ORIGIN + "/v1/chat/completions",
                                         {"model": MODEL, "messages": []}, timeout=1)
    assert not requests


def test_workbench_answers_use_the_live_grant_for_each_matter_and_queued_job(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from case_intelligence.answer_jobs import AnswerJobFailure
    from case_intelligence.external_model_grants import model_destination
    from case_intelligence.managed_storage import StoragePolicy
    from case_intelligence.workbench import create_workbench_app

    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", ORIGIN)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", MODEL)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "0")
    statement = "Aquamarine rests in the cedar drawer."
    question = "Where is aquamarine?"
    submissions = []

    def receive(request, **_):
        if request.get_method() == "GET":
            assert request.data is None
            response = {"data": [{"id": MODEL}]}
        else:
            submissions.append(json.loads(request.data))
            answer = {"answerable": True, "claims": [{"text": statement, "evidence_ids": ["S1"]}],
                      "limitation": None, "missing_information": ""}
            response = {"choices": [{"message": {"content": json.dumps(answer)}}]}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(generator_transport, "open_generator_request", receive)
    actor = "development-taylor-morgan"
    app = create_workbench_app(tmp_path / "runtime", auth_mode="test",
                               storage_policy=StoragePolicy(reserve_bytes=0))
    with TestClient(app):
        bench = app.state.workbench
        bench.answers.close()
        bench.answers = None
        approved = bench.create_matter("Synthetic approved matter", "", actor)
        denied = bench.create_matter("Synthetic unapproved matter", "", actor)
        for matter in (approved, denied):
            doc, _ = bench.source_store(matter).store_stream(
                "Synthetic drawer note.txt", "text/plain", io.BytesIO(statement.encode()))
            bench._sync_source_catalog(matter, [doc])
        grant = model_destination("generation", ORIGIN, MODEL)
        bench.workspace.set_model_grant(approved.matter_id, grant, actor, approved=True)
        conversation = bench.workspace.get_conversation(approved.matter_id)
        bench.ask(approved, conversation, question)
        assert len(submissions) == 1
        assert statement in submissions[0]["messages"][-1]["content"]
        with pytest.raises(GenerationUnavailable, match="matter approval"):
            bench.ask(denied, bench.workspace.get_conversation(denied.matter_id), question)

        job, _ = bench.queue_answer(approved, conversation, question,
                                    "answer-request-" + "a" * 32, actor)
        claimed = bench.workspace.claim_answer_job("synthetic-answer-worker")
        assert claimed.job_id == job.job_id
        bench.workspace.set_model_grant(approved.matter_id, grant, actor, approved=False)
        with pytest.raises(AnswerJobFailure, match="matter approval"):
            bench._process_answer_job(claimed, lambda *_: None, lambda: False)
        assert len(submissions) == 1


@pytest.mark.parametrize("revoke", [False, True])
def test_recorded_tokenization_and_completion_each_require_the_prefixed_grant(monkeypatch, revoke):
    from case_intelligence.external_model_grants import model_destination
    from case_intelligence.generation_matter import generation_matter_scope
    from case_intelligence.recorded_generation import RecordedDispatch

    endpoint = ORIGIN + "/tenant-a"
    active = {("matter-a", model_destination("generation", endpoint, MODEL))}
    workspace = SimpleNamespace(model_grant_allows=lambda matter, target: (matter, target) in active)
    saved = SimpleNamespace(prepare=lambda *_: 1, transition=lambda *_: None)
    client = SimpleNamespace(endpoint=endpoint, model=MODEL, timeout=1, _headers={})
    paths = []

    def receive(request, **_):
        paths.append(request.full_url)
        if request.full_url == endpoint + "/tokenize":
            if revoke:
                active.clear()
            return io.BytesIO(b'{"count":1,"tokens":[1],"max_model_len":4096}')
        return io.BytesIO(b'{"usage":{"prompt_tokens":1}}')

    monkeypatch.setattr(generator_transport, "open_generator_request", receive)
    dispatch = RecordedDispatch(saved, object(), lambda: None, {})
    packet = {"model": MODEL, "messages": [{"role": "user", "content": "Synthetic saved context"}],
              "max_tokens": 32}
    with generation_matter_scope(workspace, "matter-a"):
        if revoke:
            with pytest.raises(GenerationUnavailable, match="matter approval"):
                dispatch.send(client, packet, False)
            assert paths == [endpoint + "/tokenize"]
        else:
            dispatch.send(client, packet, False)
            assert paths == [endpoint + "/tokenize", endpoint + "/v1/chat/completions"]


@pytest.mark.parametrize(("url", "model"), [
    (ORIGIN + "/tenant-b/v1/chat/completions", MODEL),
    ("https://alternate.example.test/tenant-a/v1/chat/completions", MODEL),
    (ORIGIN + "/tenant-a/v1/chat/completions", "other-model"),
    (ORIGIN + "/tenant-a/other-route", MODEL),
])
def test_dispatch_must_match_approved_provider_and_supported_route(monkeypatch, url, model):
    from case_intelligence.external_model_grants import model_destination
    from case_intelligence.generation_matter import generation_matter_scope

    allowed = model_destination("generation", ORIGIN + "/tenant-a", MODEL)
    workspace = SimpleNamespace(model_grant_allows=lambda matter, target: matter == "matter-a" and target == allowed)
    monkeypatch.setattr(generator_transport, "open_generator_request", lambda *_a, **_k: pytest.fail("external send"))
    with generation_matter_scope(workspace, "matter-a"), pytest.raises(GenerationUnavailable, match="matter approval"):
        generation._bounded_json_request(url, {"model": model, "messages": []}, timeout=1)


def test_classification_revalidates_grant_for_schema_repair(monkeypatch):
    from case_intelligence.external_model_grants import model_destination
    from case_intelligence.generation_matter import generation_matter_scope

    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test")
    endpoint = ORIGIN + "/tenant-a"
    grants = {("matter-a", model_destination("generation", endpoint, MODEL))}
    workspace = SimpleNamespace(model_grant_allows=lambda matter, target: (matter, target) in grants)
    client = generation.OllamaGenerator(endpoint, MODEL)
    urls = []

    def invalid_reply(request, **_):
        urls.append(request.full_url)
        grants.clear()
        return io.BytesIO(b'{"message":{"content":"{}"}}')

    monkeypatch.setattr(generator_transport, "open_generator_request", invalid_reply)
    with generation_matter_scope(workspace, "matter-a"), pytest.raises(GenerationUnavailable, match="matter approval"):
        client.classify_source(criterion="Find the synthetic note", include_guidance="", exclude_guidance="",
                               evidence=[generation.EvidenceItem("S1", "Synthetic note", "Page 1", "A cedar drawer.")])
    assert urls == [endpoint + "/api/chat"]


def test_synthetic_doctor_succeeds_without_any_matter_grant(monkeypatch):
    from case_intelligence.generator_doctor import probe_configured_generator
    from case_intelligence.generator_schema import PROBE_MARKER

    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", "answers.example.test")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "openai")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", ORIGIN)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", MODEL)
    requests = []

    def receive(request, **_):
        requests.append(json.loads(request.data))
        marker = json.dumps({"marker": PROBE_MARKER})
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": marker}}]}).encode())

    monkeypatch.setattr(generator_transport, "open_generator_request", receive)
    assert probe_configured_generator().status == "verified"
    assert len(requests) == 1
    assert requests[0]["model"] == MODEL
