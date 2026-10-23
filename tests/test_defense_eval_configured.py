"""Configured scoring uses sourced text, not product presentation notices."""
from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from case_intelligence import defense_eval as evaluation
from case_intelligence import generation
from case_intelligence.generation import GroundedGenerationService, GenerationUnavailable


class ScriptedClient:
    available = True

    def __init__(self, payload):
        self.payload = payload

    def generate(self, **kwargs):
        return self.payload


def one_question(kind="document"):
    text = "Mira counted twelve teal lantern cases beside the platform."
    passage = evaluation.Passage("p", "matter", "p", "0" * 64, kind, "1", text)
    question = evaluation.Question(
        "q", "matter", "fact_lookup", "How many cases did Mira count?", False,
        (evaluation.Fact("count", "twelve", (evaluation.Support("p", text),)),), (),
    )
    return question, passage


def test_configured_transcript_caution_is_not_scored_as_source_text():
    question, passage = one_question("transcript")
    prefix = "The machine transcript appears to say that "
    client = ScriptedClient({
        "answerable": True, "claims": [{"text": prefix + passage.text, "evidence_ids": ["S1"]}],
        "limitation": None, "missing_information": "",
    })
    answer = evaluation.ConfiguredAnswerer(GroundedGenerationService(client))(question, (passage,))
    assert not answer.error
    assert prefix in answer.text
    row = evaluation.score_question(question, answer, (passage,), {"p": passage}, 0)
    assert row["answer_correct"] is True
    assert row["citation_support"] is True
    assert answer.citations == (evaluation.Citation("p", passage.text),)


def test_configured_source_limitation_excludes_verification_notice():
    question, passage = one_question()
    claim = {"text": passage.text, "evidence_ids": ["S1"]}
    client = ScriptedClient({
        "answerable": True, "claims": [claim, {"text": "Unrelated helicopters flew overseas.",
                                                "evidence_ids": ["S1"]}],
        "limitation": claim, "missing_information": "",
    })
    service = GroundedGenerationService(client)
    answer = evaluation.ConfiguredAnswerer(service)(question, (passage,))
    assert not answer.error
    assert "Limitation:" in answer.text
    row = evaluation.score_question(question, answer, (passage,), {"p": passage}, 0)
    assert row["citation_support"] is True
    assert answer.citations == (evaluation.Citation("p", passage.text),)


@pytest.fixture(params=[generation.OpenAICompatibleGenerator, generation.OllamaGenerator])
def adapter(request, monkeypatch):
    class Opener:
        response = b""

        def open(self, request, **kwargs):
            if request.get_method() == "GET":
                return io.BytesIO(b'{"data":[{"id":"synthetic"}],"models":[{"name":"synthetic"}]}')
            if isinstance(self.response, Exception):
                raise self.response
            return io.BytesIO(self.response)

    opener = Opener()
    client = request.param("http://127.0.0.1:9", "synthetic", opener=opener)
    monkeypatch.setattr(evaluation, "generator_from_environment", lambda: client)
    suite = evaluation.load_suite()
    monkeypatch.setattr(evaluation, "load_suite", lambda *_: replace(suite, questions=suite.questions[:1]))
    return client, opener


@pytest.mark.parametrize("content", ["{}", "[]", "I cannot answer that."])
def test_rejected_model_output_is_a_score_not_a_harness_error(content, adapter, tmp_path):
    client, opener = adapter
    message = {"content": content}
    response = ({"choices": [{"message": message}]} if isinstance(client, generation.OpenAICompatibleGenerator)
                else {"message": message})
    opener.response = json.dumps(response).encode()
    assert evaluation.main(["--mode", "configured", "--output-dir", str(tmp_path)]) == 0
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert card["aggregates"]["errors"] == 0
    assert card["aggregates"]["rejections"] == len(card["questions"]) == 1
    assert card["aggregates"]["answer_correct"] == 0
    rejection = "GenerationRejected" if content == "{}" else "GenerationInvalidContent"
    assert all(row["rejection"] == rejection for row in card["questions"])
    if content != "{}":
        with pytest.raises(GenerationUnavailable) as caught:
            client.generate(question="How many cases?", evidence=())
        assert not isinstance(caught.value, generation.GenerationRejected)


@pytest.mark.parametrize("response", [
    OSError("synthetic connection failure"), TimeoutError("synthetic timeout"),
    b"not JSON", b"[]", b"{}", b'{"choices":[{"message":{}}],"message":{}}',
    b" " * (generation.MAX_RESPONSE_BYTES + 1),
], ids=["connection", "timeout", "unreadable", "non-object", "envelope", "missing-content", "oversized"])
def test_adapter_runtime_failures_remain_harness_errors(response, adapter, tmp_path):
    _client, opener = adapter
    opener.response = response
    assert evaluation.main(["--mode", "configured", "--output-dir", str(tmp_path)]) == 2
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert card["aggregates"]["errors"] == len(card["questions"]) == 1
    assert card["aggregates"]["rejections"] == 0
    assert card["questions"][0]["error"] == "GenerationUnavailable"


def test_unavailable_model_is_a_harness_error(monkeypatch, tmp_path):
    class UnavailableClient(ScriptedClient):
        def generate(self, **kwargs):
            raise GenerationUnavailable("synthetic unavailable service")

    monkeypatch.setattr(evaluation, "generator_from_environment", lambda: UnavailableClient({}))
    assert evaluation.main(["--mode", "configured", "--output-dir", str(tmp_path)]) == 2
    card = json.loads((tmp_path / "scorecard.json").read_text())
    assert card["aggregates"]["errors"] == len(card["questions"])
