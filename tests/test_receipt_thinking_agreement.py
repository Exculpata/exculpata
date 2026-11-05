"""Captured request settings must agree exactly with the adapter configuration."""

import pytest

from case_intelligence import claim_evaluation as evaluation
from tests.test_claim_evaluation_protocol import capture, no_network  # noqa: F401


@pytest.mark.parametrize("disable_thinking", [True, False])
def test_capture_records_effective_thinking(capture, disable_thinking):
    receipt, requests = capture(disable_thinking=disable_thinking)
    thinking = not disable_thinking
    settings = {"thinking": thinking, "output_tokens": 1200,
                "reasoning_tokens": 4096 if thinking else 0}
    evaluation.validate_receipt(receipt)
    assert receipt["configuration"]["thinking"] is thinking
    assert {key: receipt["configuration"][key] for key in settings} == settings
    assert len(requests) == len(receipt["results"]) == 11
    for request, row in zip(requests, receipt["results"]):
        assert row["request_settings"] == settings
        assert request["chat_template_kwargs"] == {"enable_thinking": thinking}
        assert request["max_tokens"] == 1200 + settings["reasoning_tokens"]


def test_ollama_capture_keeps_thinking_on(capture):
    receipt, requests = capture("ollama")
    evaluation.validate_receipt(receipt)
    assert receipt["configuration"]["thinking"] is True
    assert receipt["configuration"]["disable_thinking"] is None
    for request, row in zip(requests, receipt["results"]):
        assert request["think"] is True
        assert row["request_settings"] == {
            "thinking": True, "output_tokens": 1200, "reasoning_tokens": 4096,
        }


def test_unconfigured_adapter_records_answer_default(capture):
    receipt, _ = capture(disable_thinking=None)
    evaluation.validate_receipt(receipt)
    assert receipt["configuration"]["disable_thinking"] is False
    assert receipt["configuration"]["thinking"] is True


@pytest.mark.parametrize("disable_thinking,field,value", [
    (True, "thinking", True), (False, "thinking", False),
    (False, "output_tokens", 1201), (False, "reasoning_tokens", 4095),
    (False, "thinking", 1),
])
def test_resealed_row_settings_must_match_configuration(capture, disable_thinking, field, value):
    receipt, _ = capture(disable_thinking=disable_thinking)
    receipt.pop("receipt_sha256")
    receipt["results"][0]["request_settings"][field] = value
    with pytest.raises(ValueError, match="request settings"):
        evaluation.validate_receipt(evaluation.seal(receipt))


@pytest.mark.parametrize("backend,disable_thinking", [
    ("openai", True), ("openai", False), ("ollama", None),
])
def test_configuration_must_match_adapter_even_when_rows_agree(capture, backend, disable_thinking):
    receipt, _ = capture(backend, disable_thinking=disable_thinking)
    receipt.pop("receipt_sha256")
    thinking = not receipt["configuration"]["thinking"]
    settings = {"thinking": thinking, "output_tokens": 1200,
                "reasoning_tokens": 4096 if thinking else 0}
    receipt["configuration"].update(settings)
    for row in receipt["results"]:
        row["request_settings"] = dict(settings)
    with pytest.raises(ValueError, match="configuration|thinking"):
        evaluation.validate_receipt(evaluation.seal(receipt))
