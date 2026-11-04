"""Offline contracts for request thinking and state-specific schema checks."""

import json

import pytest

from case_intelligence import generation, generator_completion, generator_schema
from case_intelligence.review_budget import DEFAULT_REVIEW_BUDGET


ANSWER = {
    "answerable": False,
    "claims": [],
    "limitation": None,
    "missing_information": "The synthetic record does not answer the question.",
}
REVIEW = {
    "decision": "not_identified",
    "rationale": "No express match appears in the synthetic record.",
    "evidence_ids": [],
}


def _reply(server, content):
    message = {
        "content": json.dumps(content),
        "reasoning_content": '{"private_reasoning": "synthetic hidden text"}',
        "thinking": '{"private_reasoning": "synthetic hidden text"}',
    }
    return {"message": message} if server == "ollama" else {"choices": [{"message": message}]}


def _thinking(server, body):
    if server == "ollama":
        return body["think"]
    return body["chat_template_kwargs"]["enable_thinking"]


@pytest.fixture(params=["openai", "ollama"])
def backend(request):
    return request.param


@pytest.fixture
def wire(monkeypatch, backend):
    bodies = []

    def request(_url, body, **_kwargs):
        bodies.append(body)
        schema = body.get("format") or body["response_format"]["json_schema"]["schema"]
        content = REVIEW if "decision" in schema["properties"] else ANSWER
        return _reply(backend, content)

    def probe_transport(_timeout):
        def send(_url, body, _headers):
            bodies.append(body)
            return _reply(backend, {"marker": generator_schema.PROBE_MARKER})
        return send

    monkeypatch.setattr(generation, "_bounded_json_request", request)
    monkeypatch.setattr(generator_schema, "_urllib_transport", probe_transport)
    return bodies


@pytest.fixture
def client(monkeypatch, backend, wire):
    monkeypatch.setattr(generation, "validate_generator_environment", lambda _environment: None)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", backend)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SERVER", "ollama" if backend == "ollama" else "vllm")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "synthetic-model")
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "0")
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_API_KEY", raising=False)
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE", raising=False)
    return generation.generator_from_environment()


def _classify(client, **kwargs):
    return client.classify_source(
        criterion="Identify an express reference.", include_guidance="Use the record.",
        exclude_guidance="Do not infer a reference.", evidence=(), **kwargs,
    )


@pytest.mark.parametrize("disabled", ["0", "1"])
def test_answer_and_extraction_override_environment(monkeypatch, client, backend, wire, disabled):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING", disabled)
    client = generation.generator_from_environment()
    assert client.generate(question="What is missing?", evidence=(), thinking=True) == ANSWER
    assert _classify(client, thinking=False) == REVIEW
    assert [_thinking(backend, body) for body in wire] == [True, False]


@pytest.mark.parametrize("disabled", ["0", "1"])
def test_environment_is_only_the_extraction_default(monkeypatch, client, backend, wire, disabled):
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING", disabled)
    client = generation.generator_from_environment()
    client.generate(question="What is missing?", evidence=(), thinking=None)
    _classify(client, thinking=None)
    assert [_thinking(backend, body) for body in wire] == [True, disabled == "0"]


def test_only_message_content_is_returned(client):
    assert client.generate(question="What is missing?", evidence=()) == ANSWER
    assert _classify(client) == REVIEW


def test_reasoning_budget_is_separate_from_visible_output():
    assert DEFAULT_REVIEW_BUDGET.output_tokens == 1_200
    assert type(DEFAULT_REVIEW_BUDGET.reasoning_tokens) is int
    assert DEFAULT_REVIEW_BUDGET.reasoning_tokens > 0


def test_probe_reasoning_allowance_covers_the_review_budget():
    assert generator_schema.PROBE_REASONING_TOKENS >= DEFAULT_REVIEW_BUDGET.reasoning_tokens


def test_probe_carries_state_and_increases_reasoning_allowance(client, backend, wire):
    for thinking in (False, True):
        result = generator_schema.probe_schema_enforcement(
            client.endpoint, client.schema_server, client.model, thinking=thinking,
        )
        assert result.status == "verified"
        assert _thinking(backend, wire[-1]) is thinking
    limits = [body["options"]["num_predict"] if backend == "ollama" else body["max_tokens"]
              for body in wire]
    assert len(limits) == 2
    assert limits[1] > limits[0]


@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("operation", ["generate", "classify_source", "schema_probe"])
def test_every_request_body_refuses_native_tools(client, backend, wire, thinking, operation):
    if operation == "generate":
        client.generate(question="What is missing?", evidence=(), thinking=thinking)
    elif operation == "classify_source":
        _classify(client, thinking=thinking)
    else:
        _, body = generator_schema.schema_chat_request(
            client.schema_server, model=client.model, schema=generator_schema.PROBE_SCHEMA,
            prompt="Return the synthetic marker.", name="synthetic_probe", thinking=thinking,
        )
        wire.append(body)
        generator_schema.probe_schema_enforcement(
            client.endpoint, client.schema_server, client.model, thinking=thinking,
        )
    assert wire
    for body in wire:
        assert _thinking(backend, body) is thinking
        generator_schema.refuse_tool_request(body)
        assert not {"tools", "tool_choice", "functions", "function_call", "parallel_tool_calls"}.intersection(body)
        for message in body["messages"]:
            assert message["role"].strip().casefold() not in {"tool", "function"}
            assert not {"tool_calls", "function_call"}.intersection(message)


def _synchronous_refresh(monkeypatch, client):
    class InlineThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(generator_completion.threading, "Thread", InlineThread)
    monkeypatch.setattr(generator_completion.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "1")
    client.schema_mode = "verified"
    client._schema_checked_at = 0.0


def test_schema_probe_repairs_only_thinking_state_and_caches_per_model(
    monkeypatch, client, backend, wire,
):
    _synchronous_refresh(monkeypatch, client)
    results = []
    real_probe = generator_schema.probe_schema_enforcement

    def transport(_url, body, _headers):
        wire.append(body)
        thinking = _thinking(backend, body)
        conflicting = generator_schema.PROBE_CONFLICT in body["messages"][0]["content"]
        marker = generator_schema.PROBE_CONFLICT if thinking and conflicting else generator_schema.PROBE_MARKER
        return _reply(backend, {"marker": marker})

    def probe(*args, **kwargs):
        result = real_probe(*args, transport=transport, **kwargs)
        results.append(result.status)
        return result

    monkeypatch.setattr(generator_completion, "probe_schema_enforcement", probe)
    for thinking in (False, True, False, True):
        generator_completion.refresh_generator_schema(client, thinking=thinking)
        generator_completion.schema_refused(client, thinking=thinking)
    assert results == ["verified", "validate_and_retry"]
    assert [_thinking(backend, body) for body in wire] == [False, True, True]
    client.model = "synthetic-model-two"
    generator_completion.refresh_generator_schema(client, thinking=True)
    assert results == ["verified", "validate_and_retry", "validate_and_retry"]
    assert [body["model"] for body in wire] == ["synthetic-model"] * 3 + ["synthetic-model-two"] * 2
    client.model = "synthetic-model"
    generator_completion.refresh_generator_schema(client, thinking=True)
    assert len(results) == 3


@pytest.mark.parametrize("states", [(False, True), (True, False)])
def test_schema_refusal_does_not_disable_other_thinking_state(monkeypatch, client, backend, states):
    _synchronous_refresh(monkeypatch, client)

    def transport(_url, body, _headers):
        marker = generator_schema.PROBE_CONFLICT if _thinking(backend, body) else generator_schema.PROBE_MARKER
        return _reply(backend, {"marker": marker})

    def probe(*args, **kwargs):
        return generator_schema.probe_schema_enforcement(*args, transport=transport, **kwargs)

    monkeypatch.setattr(generator_completion, "probe_schema_enforcement", probe)
    for thinking in states:
        generator_completion.refresh_generator_schema(client, thinking=thinking)
    generator_completion.schema_refused(client, thinking=False)
    with pytest.raises(generation.GenerationUnavailable, match="schema"):
        generator_completion.schema_refused(client, thinking=True)
    assert _classify(client, thinking=False) == REVIEW


def test_unset_environment_uses_code_defaults(monkeypatch, client, backend, wire):
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING", raising=False)
    client = generation.generator_from_environment()
    client.generate(question="What is missing?", evidence=())
    _classify(client)
    assert [_thinking(backend, body) for body in wire] == [True, False]


@pytest.mark.parametrize("disabled", [False, True])
def test_openai_compatibility_attribute_retains_extraction_default(disabled):
    client = generation.OpenAICompatibleGenerator(
        "http://127.0.0.1:8000", "synthetic-model", disable_thinking=disabled,
    )
    assert client.disable_thinking is disabled
    assert client.disable_thinking is not client.extraction_thinking


@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("profile_thinking", [False, True])
def test_explicit_question_profile_precedes_compatibility_default(monkeypatch, disabled, profile_thinking):
    bodies = []

    def request(_url, body, **_kwargs):
        bodies.append(body)
        return _reply("openai", {"questions": []})

    monkeypatch.setattr(generation, "_bounded_json_request", request)
    client = generation.OpenAICompatibleGenerator(
        "http://127.0.0.1:8000", "synthetic-model", disable_thinking=disabled,
        question_profile=generation.QuestionGenerationProfile(thinking=profile_thinking, top_up_calls=0),
    )
    assert client.disable_thinking is not profile_thinking
    assert client.extraction_thinking is not disabled
    client.draft_questions(purpose="witness", topic="Synthetic record", evidence=())
    assert _thinking("openai", bodies[0]) is profile_thinking
    for body in bodies:
        generator_schema.refuse_tool_request(body)


@pytest.mark.parametrize("thinking", [False, True])
def test_reasoning_allowance_is_added_only_when_requested(client, backend, wire, thinking):
    client.generate(question="What is missing?", evidence=(), thinking=thinking)
    _classify(client, thinking=thinking, output_tokens=321)
    allowance = DEFAULT_REVIEW_BUDGET.reasoning_tokens if thinking else 0
    limits = [body["options"]["num_predict"] if backend == "ollama" else body["max_tokens"]
              for body in wire]
    assert limits == [1_200 + allowance, 321 + allowance]


def test_reasoning_cannot_substitute_for_missing_content(monkeypatch, client, backend):
    def request(_url, body, **_kwargs):
        schema = body.get("format") or body["response_format"]["json_schema"]["schema"]
        result = REVIEW if "decision" in schema["properties"] else ANSWER
        message = {"reasoning_content": json.dumps(result), "thinking": json.dumps(result)}
        return {"message": message} if backend == "ollama" else {"choices": [{"message": message}]}

    monkeypatch.setattr(generation, "_bounded_json_request", request)
    with pytest.raises(generation.GenerationUnavailable):
        client.generate(question="What is missing?", evidence=())
    with pytest.raises(generation.GenerationUnavailable):
        _classify(client)
    result = generator_schema.probe_schema_enforcement(
        client.endpoint, client.schema_server, client.model, thinking=True,
        transport=lambda *_args: _reply(backend, None),
    )
    assert result.status == "refused"


@pytest.mark.parametrize("refused_state", [False, True])
def test_cached_refusal_is_applied_to_each_completion(
    monkeypatch, client, backend, wire, refused_state,
):
    for thinking in (False, True):
        status = "refused" if thinking == refused_state else "verified"
        generator_completion.remember_schema(
            client, generator_schema.SchemaProbe(status, 1, "Synthetic check", ""), thinking=thinking,
        )
    monkeypatch.setattr(generation, "_bounded_json_get", lambda *_args, **_kwargs: {
        "models": [{"name": client.model}], "data": [{"id": client.model}],
    })
    assert client.available is (not refused_state)
    for operation in (client.generate, lambda **kwargs: _classify(client, **kwargs)):
        args = {"question": "What is missing?", "evidence": ()} if operation == client.generate else {}
        before = len(wire)
        with pytest.raises(generation.GenerationUnavailable, match="schema"):
            operation(**args, thinking=refused_state)
        assert len(wire) == before
        operation(**args, thinking=not refused_state)
        assert _thinking(backend, wire[-1]) is not refused_state


def test_startup_and_first_extraction_probe_use_their_request_states(monkeypatch, client, backend, wire):
    _synchronous_refresh(monkeypatch, client)
    monkeypatch.delenv("CASE_INTELLIGENCE_GENERATOR_DISABLE_THINKING", raising=False)
    client = generation.generator_from_environment()
    assert [_thinking(backend, body) for body in wire] == [True]
    _classify(client)
    client.generate(question="What is missing?", evidence=())
    _classify(client)
    assert [_thinking(backend, body) for body in wire] == [True, False, False, True, False]


def test_refresh_keeps_inflight_models_and_states_separate(monkeypatch, client, backend, wire):
    pending = []

    class QueuedThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            pending.append(self.target)

    _synchronous_refresh(monkeypatch, client)
    monkeypatch.setattr(generator_completion.threading, "Thread", QueuedThread)
    for thinking in (False, True, False, True):
        generator_completion.refresh_generator_schema(client, thinking=thinking)
    assert len(pending) == 2
    first_model = client.model
    client.model = "synthetic-model-two"
    generator_completion.refresh_generator_schema(client, thinking=False)
    assert len(pending) == 3
    for run in pending:
        run()
    assert [(body["model"], _thinking(backend, body)) for body in wire] == [
        (first_model, False), (first_model, True), (client.model, False),
    ]
    assert not client._schema_refreshing
    for model in (first_model, "synthetic-model-two"):
        client.model = model
        generator_completion.refresh_generator_schema(client, thinking=False, now=1_059)
    assert len(pending) == 3
    generator_completion.refresh_generator_schema(client, thinking=False, now=1_060)
    assert len(pending) == 4


def test_cached_answer_status_follows_the_current_model(monkeypatch, client):
    _synchronous_refresh(monkeypatch, client)
    first_model = client.model
    generator_completion.remember_schema(
        client, generator_schema.SchemaProbe("refused", 3, "Synthetic refusal", ""),
    )
    client.model = "synthetic-model-two"
    generator_completion.remember_schema(
        client, generator_schema.SchemaProbe("verified", 1, "Synthetic success", ""),
    )
    client.model = first_model
    generator_completion.refresh_generator_schema(client, thinking=True)
    assert client.schema_mode == "refused"
    assert client.schema_detail == "Synthetic refusal"
