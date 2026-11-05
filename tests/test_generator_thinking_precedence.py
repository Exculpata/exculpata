"""Synthetic request checks for explicit thinking settings and defaults."""

import json

import pytest

from case_intelligence import generation, generator_schema
from case_intelligence.review_budget import DEFAULT_REVIEW_BUDGET


@pytest.fixture(params=[generation.OpenAICompatibleGenerator, generation.OllamaGenerator])
def adapter(request, monkeypatch):
    bodies = []

    def send(_url, body, **_kwargs):
        bodies.append(body)
        schema = body.get("format") or body["response_format"]["json_schema"]["schema"]
        payload = (
            {"decision": "not_identified", "rationale": "No synthetic match.", "evidence_ids": []}
            if "decision" in schema["properties"] else
            {"answerable": False, "claims": [], "limitation": None, "missing_information": "No synthetic answer."}
        )
        message = {"content": json.dumps(payload)}
        return {"message": message} if "think" in body else {"choices": [{"message": message}]}

    monkeypatch.setattr(generation, "_bounded_json_request", send)
    return request.param, bodies


def _requests(client, **kwargs):
    client.generate(question="What is missing?", evidence=(), **kwargs)
    client.classify_source(
        criterion="Identify an express reference.", include_guidance="Use the record.",
        exclude_guidance="Do not infer.", evidence=(), **kwargs,
    )


def _assert_requests(bodies, states):
    assert len(bodies) == len(states)
    for body, thinking, visible in zip(bodies, states, (1_200, 400)):
        generator_schema.refuse_tool_request(body)
        if "think" in body:
            assert body["think"] is thinking
            assert body["format"]["type"] == "object"
            limit = body["options"]["num_predict"]
        else:
            assert body["chat_template_kwargs"] == {"enable_thinking": thinking}
            assert body["response_format"]["json_schema"]["strict"] is True
            limit = body["max_tokens"]
        assert limit == visible + (DEFAULT_REVIEW_BUDGET.reasoning_tokens if thinking else 0)


def test_unconfigured_answer_and_extraction_keep_independent_defaults(adapter):
    client_type, bodies = adapter
    client = client_type("http://127.0.0.1:8000", "synthetic-model")
    _requests(client)
    _assert_requests(bodies, (True, False))


@pytest.mark.parametrize("disabled", [False, True])
def test_explicit_constructor_setting_beats_answer_and_extraction_defaults(adapter, disabled):
    client_type, bodies = adapter
    client = client_type("http://127.0.0.1:8000", "synthetic-model", disable_thinking=disabled)
    _requests(client)
    _assert_requests(bodies, (not disabled, not disabled))


@pytest.mark.parametrize("thinking", [False, True])
def test_explicit_profile_setting_beats_answer_and_extraction_defaults(adapter, thinking):
    client_type, bodies = adapter
    client = client_type(
        "http://127.0.0.1:8000", "synthetic-model", disable_thinking=thinking,
        question_profile=generation.QuestionGenerationProfile(thinking=thinking),
    )
    _requests(client)
    _assert_requests(bodies, (thinking, thinking))


@pytest.mark.parametrize("thinking", [False, True])
def test_call_value_overrides_explicit_constructor_setting(adapter, thinking):
    client_type, bodies = adapter
    client = client_type("http://127.0.0.1:8000", "synthetic-model", disable_thinking=thinking)
    _requests(client, thinking=thinking)
    _assert_requests(bodies, (thinking, thinking))
