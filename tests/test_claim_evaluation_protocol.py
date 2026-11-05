"""Synthetic production-adapter captures must retain their frozen protocol."""
import copy
import json
import socket

import pytest

from case_intelligence import claim_evaluation as evaluation
from case_intelligence import generation


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Protocol regressions must never contact a model or network.")
    monkeypatch.setattr(generation.urllib.request, "urlopen", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


@pytest.fixture
def capture(monkeypatch):
    def run(backend="openai", *, disable_thinking=False, repetitions=1, failure=False):
        requests = []
        def response(url, payload, **kwargs):
            requests.append(copy.deepcopy(payload))
            if failure:
                raise generation.GenerationUnavailable("Synthetic unavailable response")
            content = json.dumps({"answerable": False, "claims": [], "limitation": None,
                                  "missing_information": "Synthetic abstention."})
            return {"message": {"content": content}, "choices": [{"message": {"content": content}}]}
        monkeypatch.setattr(generation, "_bounded_json_request", response)
        pinned = evaluation.model_records("portable", generator_exercised=False)["components"][0]
        runtime = {
            "runtime_name": "synthetic", "runtime_version": "test-only",
            "accelerator": "none", "accelerator_memory_gib": 0, "driver": "none",
            "model_artifact_sha256": "a" * 64, "offline_readiness": "not_exercised",
            "offline_evidence_sha256": None, "upstream_model_id": pinned["model_id"],
            "upstream_revision": pinned["revision"], "license": pinned["license"],
        }
        client = (generation.OllamaGenerator("http://127.0.0.1:11435", "synthetic-protocol")
                  if backend == "ollama" else generation.OpenAICompatibleGenerator(
                      "http://127.0.0.1:11435", "synthetic-protocol", disable_thinking=disable_thinking))
        snapshot = {"method": "ollama_tag_digest_snapshots" if backend == "ollama" else "api_model_id_snapshots",
                    "model": client.model, "artifact_sha256": "a" * 64 if backend == "ollama" else None}
        receipt = evaluation.capture(client, profile="portable", runtime=runtime, repetitions=repetitions,
                                     identity_check=lambda: copy.deepcopy(snapshot))
        return receipt, requests
    return run


def completed_grades(receipt):
    grades = evaluation.grade_template(receipt)
    grades["grader_id"] = "synthetic-protocol-grader"
    for sample in grades["samples"]:
        sample.update(answer_correct=False, rationale="Synthetic abstention, not a correct answer.")
    return grades


def grading(receipt, grades, operation):
    if operation == "grade_template":
        return evaluation.grade_template(receipt)
    grades["capture_sha256"] = receipt["receipt_sha256"]
    return evaluation.score_capture(receipt, grades)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("backend,field,value", [
    ("openai", "temperature", 0.9),
    ("openai", "thinking", False),
    ("openai", "thinking", 1),
    ("openai", "reasoning_tokens", 0),
    ("openai", "reasoning_tokens", 4096.0),
    ("openai", "output_tokens", 10),
    ("openai", "output_tokens", 1200.0),
    ("openai", "context_tokens", 8192),
    ("ollama", "context_tokens", 16384),
    ("ollama", "context_tokens", 8192.0),
    ("openai", "seed", 42),
    ("openai", "seed_status", "fixed_seed"),
    ("openai", "history", [["Synthetic earlier question", "Synthetic earlier answer"]]),
    ("openai", "working_context", "Synthetic notebook context"),
    ("openai", "grounding_repair", True),
    ("openai", "grounding_repair", 0),
    ("openai", "response_schema", {}),
    ("openai", "unexpected", "synthetic-extra-option"),
    ("openai", "disable_thinking", None),
    ("openai", "disable_thinking", 0),
    ("ollama", "disable_thinking", False),
])
def test_resealed_configuration_cannot_contradict_capture_protocol(capture, operation, backend, field, value):
    receipt, _ = capture(backend)
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    receipt["configuration"][field] = value
    with pytest.raises(ValueError, match="configuration|thinking"):
        grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("field", ["temperature", "history", "response_schema", "disable_thinking", "thinking", "reasoning_tokens"])
def test_resealed_configuration_cannot_drop_protocol_fields(capture, operation, field):
    receipt, _ = capture()
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    del receipt["configuration"][field]
    with pytest.raises(ValueError, match="configuration"):
        grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("synthetic", 1), ("synthetic", False),
    ("suite_id", "synthetic-other-suite"), ("scope", "installed_stack_acceptance"),
    ("retrieval", "embedding_and_reranker_exercised"),
    ("release_acceptance", "accepted"), ("supported_hardware_acceptance", "established"),
])
def test_resealed_receipt_cannot_change_producer_owned_scope(capture, operation, field, value):
    receipt, _ = capture()
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    receipt[field] = value
    with pytest.raises(ValueError, match="protocol|schema"):
        grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("mutation", ["manifest", "embedding_execution", "reranker_execution",
                                      "embedding_revision", "generator_gated", "revision_basis",
                                      "missing_component", "extra_component"])
def test_resealed_complete_model_catalog_must_match_capture_scope(capture, operation, mutation):
    receipt, _ = capture()
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    models = receipt["models"]
    if mutation == "manifest":
        models["manifest_sha256"] = "b" * 64
    elif mutation in {"embedding_execution", "reranker_execution"}:
        models["components"][1 if mutation == "embedding_execution" else 2]["execution"] = "exercised"
    elif mutation == "embedding_revision":
        models["components"][1]["revision"] = "b" * 40
    elif mutation == "generator_gated":
        models["components"][0]["gated"] = 0
    elif mutation == "revision_basis":
        models["components"][0]["revision_basis"] = "runtime_attestation"
    elif mutation == "missing_component":
        models["components"].pop()
    else:
        models["components"].append(copy.deepcopy(models["components"][-1]))
    with pytest.raises(ValueError, match="model catalog"):
        grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
def test_schema_boolean_cannot_be_replaced_by_equal_python_number(capture, operation):
    receipt, _ = capture()
    receipt = copy.deepcopy(receipt)
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    receipt["configuration"]["response_schema"]["additionalProperties"] = 0
    with pytest.raises(ValueError, match="configuration"):
        grading(evaluation.seal(receipt), grades, operation)


def test_capture_schema_is_detached_from_production_schema(capture):
    receipt, _ = capture()
    schema = receipt["configuration"]["response_schema"]
    assert schema is not generation.ANSWER_SCHEMA
    original = json.dumps(generation.ANSWER_SCHEMA, sort_keys=True)
    schema["additionalProperties"] = 0
    assert json.dumps(generation.ANSWER_SCHEMA, sort_keys=True) == original


@pytest.mark.parametrize("invalid", [None, [], "synthetic", True, 1])
def test_non_object_receipt_has_controlled_validation_error(invalid):
    with pytest.raises(ValueError, match="receipt"):
        evaluation.validate_receipt(invalid)


@pytest.mark.parametrize("invalid", [None, [], "synthetic", True, 1])
def test_non_object_configuration_has_controlled_validation_error(capture, invalid):
    receipt, _ = capture()
    receipt.pop("receipt_sha256")
    receipt["configuration"] = invalid
    with pytest.raises(ValueError, match="configuration"):
        evaluation.grade_template(evaluation.seal(receipt))


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("mutation", ["system", "user", "missing", "extra", "other_case"])
def test_every_resealed_sample_prompt_must_match_frozen_case(capture, operation, failure, mutation):
    receipt, _ = capture(failure=failure)
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    row = receipt["results"][0]
    if mutation in {"system", "user"}:
        row["prompt"][mutation] += " Synthetic altered instruction."
    elif mutation == "missing":
        del row["prompt"]["user"]
    elif mutation == "extra":
        row["prompt"]["history"] = []
    else:
        row["prompt"] = copy.deepcopy(receipt["results"][1]["prompt"])
    with pytest.raises(ValueError, match="prompt"):
        grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("backend,thinking", [("ollama", None), ("openai", False), ("openai", True)])
@pytest.mark.parametrize("repetitions", [1, 2])
def test_supported_adapter_options_preserve_actual_payloads_and_grading(capture, backend, thinking, repetitions):
    receipt, requests = capture(backend, disable_thinking=thinking, repetitions=repetitions)
    original = copy.deepcopy(receipt)
    configuration = receipt["configuration"]
    template = evaluation.grade_template(receipt)
    score = evaluation.score_capture(receipt, completed_grades(receipt))
    assert receipt == original
    assert len(template["samples"]) == len(requests) == 11 * repetitions
    assert score["generation"]["failed_attempts"] == evaluation.fraction(0, 11 * repetitions)
    assert configuration["disable_thinking"] is thinking
    expected_thinking = not thinking
    expected_reasoning = 4096 if expected_thinking else 0
    assert configuration["thinking"] is expected_thinking
    assert configuration["output_tokens"] == 1200
    assert configuration["reasoning_tokens"] == expected_reasoning
    assert configuration["seed"] is None
    assert configuration["seed_status"] == "production_adapters_do_not_set_seed"
    for request, row in zip(requests, receipt["results"]):
        assert row["request_settings"] == {
            "thinking": expected_thinking, "output_tokens": 1200, "reasoning_tokens": expected_reasoning,
        }
        assert request["model"] == configuration["model"]
        assert request["messages"] == [{"role": role, "content": row["prompt"][role]}
                                       for role in ("system", "user")]
        if backend == "ollama":
            assert request["options"] == {"temperature": configuration["temperature"],
                "num_ctx": configuration["context_tokens"], "num_predict": configuration["output_tokens"] + configuration["reasoning_tokens"]}
            assert request["think"] is True
            assert request["format"] == configuration["response_schema"]
        else:
            assert request["temperature"] == configuration["temperature"]
            assert request["max_tokens"] == configuration["output_tokens"] + configuration["reasoning_tokens"]
            assert request["response_format"]["json_schema"]["schema"] == configuration["response_schema"]
            assert request["chat_template_kwargs"] == {"enable_thinking": expected_thinking}
            assert configuration["context_tokens"] == "server_configured"


@pytest.mark.parametrize("backend", ["openai", "ollama"])
@pytest.mark.parametrize("failure", [False, True])
def test_capture_budget_evidence_comes_from_dispatched_payload(capture, monkeypatch, backend, failure):
    monkeypatch.setattr(generation, "_completion_token_limit", lambda *_args: 5297)
    with pytest.raises(ValueError, match="request settings"):
        capture(backend, failure=failure)


@pytest.mark.parametrize("field,value", [("thinking", False), ("thinking", 1),
    ("output_tokens", 5296), ("output_tokens", 1200.0),
    ("reasoning_tokens", 0), ("reasoning_tokens", 4096.0)])
def test_resealed_request_settings_cannot_contradict_capture_protocol(capture, field, value):
    receipt, _ = capture()
    receipt.pop("receipt_sha256")
    receipt["results"][0]["request_settings"][field] = value
    with pytest.raises(ValueError, match="request settings"):
        evaluation.grade_template(evaluation.seal(receipt))
