from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from case_intelligence.generation import (
    EvidenceItem,
    GenerationGroundingRejected,
    GenerationRejected,
    GenerationUnavailable,
    GroundedGenerationService,
    OpenAICompatibleGenerator,
    OllamaGenerator,
    UnavailableGenerator,
)


EVIDENCE = (
    EvidenceItem(
        "S1",
        "Incident reports.pdf",
        "Page 12",
        "Officer Daniels reported seeing the canvas bag at 10:07 p.m. before activating emergency lights.",
    ),
    EvidenceItem(
        "S2",
        "Interview notes.docx",
        "Section 4",
        "Officer Lee stated that he first observed the canvas bag at 10:12 p.m., after the vehicle stopped.",
    ),
)


class FakeGenerator:
    available = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def test_generation_calls_real_boundary_and_keeps_only_verified_claims():
    client = FakeGenerator(
        {
            "answerable": True,
            "claims": [
                {
                    "text": "Officer Daniels reported seeing the canvas bag at 10:07 p.m. before activating emergency lights.",
                    "evidence_ids": ["S1"],
                },
                {
                    "text": "Officer Lee reported first seeing the canvas bag at 10:12 p.m. after the vehicle stopped.",
                    "evidence_ids": ["S2"],
                },
                {
                    "text": "A helicopter recorded the event at 11:45 p.m.",
                    "evidence_ids": ["S1"],
                },
            ],
            "limitation": {
                "text": "The sources do not independently resolve which account is more accurate.",
                "evidence_ids": [],
            },
            "missing_information": "",
        }
    )
    service = GroundedGenerationService(client)
    answer = service.answer(
        "What conflicts exist between the accounts?",
        EVIDENCE,
        history=(("user", "Compare the two officers."),),
    )
    assert answer.model_called is True
    assert len(client.calls) == 1
    assert len(answer.claims) == 2
    assert answer.omitted_claims == 2
    assert answer.used_evidence_ids == ("S1", "S2")
    assert answer.limitation is not None
    assert "omitted" in answer.limitation.text
    assert service.requests_started == service.requests_completed == 1


def test_review_classification_is_explicit_and_verifies_only_inclusions():
    class ReviewGenerator(FakeGenerator):
        def classify_source(self, **kwargs):
            self.calls.append(kwargs)
            return self.payload

    included = GroundedGenerationService(
        ReviewGenerator(
            {
                "decision": "include",
                "rationale": "Officer Daniels reported seeing the canvas bag at 10:07 p.m.",
                "evidence_ids": ["S1"],
            }
        )
    ).classify_source(
        criterion="Include references to the canvas bag.",
        include_guidance="The source must expressly mention the bag.",
        exclude_guidance="Do not infer a reference.",
        evidence=EVIDENCE,
    )
    assert included.decision == "include"
    assert included.used_evidence_ids == ("S1",)

    not_identified = GroundedGenerationService(
        ReviewGenerator(
            {
                "decision": "not_identified",
                "rationale": "The supplied passages do not expressly match the saved rule.",
                "evidence_ids": [],
            }
        )
    ).classify_source(
        criterion="Include references to a helicopter.",
        include_guidance="Require an express helicopter reference.",
        exclude_guidance="Do not infer one.",
        evidence=EVIDENCE,
    )
    assert not_identified.decision == "not_identified"
    assert not_identified.used_evidence_ids == ()
    assert "No explicit match" in not_identified.rationale

    with pytest.raises(GenerationGroundingRejected, match="inclusion rationale"):
        GroundedGenerationService(
            ReviewGenerator(
                {
                    "decision": "include",
                    "rationale": "A helicopter arrived with three passengers.",
                    "evidence_ids": ["S1"],
                }
            )
        ).classify_source(
            criterion="Include helicopter evidence.",
            include_guidance="Require an express reference.",
            exclude_guidance="Do not infer one.",
            evidence=EVIDENCE,
        )


def test_review_exact_correction_span_survives_semicolon_negation_boundary():
    class ReviewGenerator(FakeGenerator):
        def classify_source(self, **kwargs):
            self.calls.append(kwargs)
            return self.payload

    correction = (
        EvidenceItem(
            "S1",
            "Generated corrected report.pdf",
            "Page 1",
            "Correction: the vehicle was not red; its final recorded color is dark gray.",
        ),
    )
    result = GroundedGenerationService(
        ReviewGenerator(
            {
                "decision": "include",
                "rationale": "Correction: the vehicle was not red; its final recorded color is dark gray.",
                "evidence_ids": ["S1"],
            }
        )
    ).classify_source(
        criterion="Include a final correction from red to dark gray.",
        include_guidance="Require the correction and final color.",
        exclude_guidance="Exclude uncertainty or an unchanged red description.",
        evidence=correction,
    )
    assert result.decision == "include"
    assert result.used_evidence_ids == ("S1",)


def test_generation_rejects_when_no_material_claim_has_support():
    client = FakeGenerator(
        {
            "answerable": True,
            "claims": [{"text": "A helicopter arrived at 11:45 p.m.", "evidence_ids": ["S1"]}],
            "limitation": None,
            "missing_information": "",
        }
    )
    service = GroundedGenerationService(client)
    with pytest.raises(GenerationGroundingRejected, match="No generated claim"):
        service.answer("What happened?", EVIDENCE)
    assert [call["grounding_repair"] for call in client.calls] == [False, True]
    assert service.requests_started == service.requests_completed == 2


def test_generation_repairs_a_paraphrase_with_source_close_transcript_wording():
    transcript = (
        EvidenceItem(
            "S1",
            "generated-fixture.wav",
            "00:12–00:16",
            "My full name is Eleanor Davis, and I drove to the office.",
            "transcript",
        ),
    )

    class RepairingGenerator:
        available = True

        def __init__(self):
            self.calls = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            text = (
                "The machine transcript appears to say that my full name is Eleanor Davis."
                if kwargs["grounding_repair"]
                else "The speaker identifies herself as Eleanor Davis."
            )
            return {
                "answerable": True,
                "claims": [{"text": text, "evidence_ids": ["S1"]}],
                "limitation": None,
                "missing_information": "",
            }

    client = RepairingGenerator()
    stages = []
    answer = GroundedGenerationService(client).answer(
        "What full name is stated?",
        transcript,
        stage_callback=stages.append,
    )

    assert answer.answerable is True
    assert [claim.text for claim in answer.claims] == [
        "The machine transcript appears to say that my full name is Eleanor Davis."
    ]
    assert answer.used_evidence_ids == ("S1",)
    assert "not establish that an event occurred" in answer.evidence_notice
    assert answer.limitation is None
    assert [call["grounding_repair"] for call in client.calls] == [False, True]
    assert stages == ["generating", "verifying", "repairing", "verifying"]


def test_partial_transcript_answer_is_repaired_without_the_generic_limitation():
    transcript = (
        EvidenceItem(
            "S1",
            "generated-fixture.wav",
            "00:00–00:04",
            "Jordan handed me the blue folder near the records desk.",
            "transcript",
        ),
        EvidenceItem(
            "S2",
            "generated-fixture.wav",
            "00:04–00:08",
            "I carried the blue folder into the office.",
            "transcript",
        ),
    )

    class PartialRepairGenerator:
        available = True

        def __init__(self):
            self.calls = []

        def generate(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["grounding_repair"]:
                claims = [
                    {
                        "text": "The machine transcript appears to say that Jordan handed me the blue folder near the records desk.",
                        "evidence_ids": ["S1"],
                    },
                    {
                        "text": "The machine transcript appears to say that I carried the blue folder into the office.",
                        "evidence_ids": ["S2"],
                    },
                ]
            else:
                claims = [
                    {
                        "text": "The machine transcript appears to say that Jordan handed me the blue folder near the records desk.",
                        "evidence_ids": ["S1"],
                    },
                    {
                        "text": "The machine transcript appears to say that Jordan secretly destroyed the red vehicle.",
                        "evidence_ids": ["S1"],
                    },
                ]
            return {
                "answerable": True,
                "claims": claims,
                "limitation": None,
                "missing_information": "",
            }

    client = PartialRepairGenerator()
    answer = GroundedGenerationService(client).answer(
        "What does the transcript say about the folder?", transcript
    )

    assert len(answer.claims) == 2
    assert answer.omitted_claims == 0
    assert answer.limitation is None
    assert [call["grounding_repair"] for call in client.calls] == [False, True]
    assert "machine transcript" in answer.evidence_notice.casefold()


def test_transcript_paraphrase_has_a_cautioned_looser_gate_but_document_does_not():
    payload = {
        "answerable": True,
        "claims": [
            {
                "text": "The machine transcript appears to say that a speaker drove the blue vehicle toward the courthouse.",
                "evidence_ids": ["S1"],
            }
        ],
        "limitation": None,
        "missing_information": "",
    }
    media = (
        EvidenceItem(
            "S1",
            "generated-fixture.wav",
            "00:10–00:14",
            "I drove the blue sedan to the courthouse.",
            "transcript",
        ),
    )
    answer = GroundedGenerationService(FakeGenerator(payload)).answer(
        "What does the recording say about travel?", media
    )
    assert answer.answerable is True
    assert answer.evidence_notice

    document = (
        EvidenceItem(
            "S1",
            "Interview notes.docx",
            "Section 2",
            "I drove the blue sedan to the courthouse.",
        ),
    )
    with pytest.raises(GenerationGroundingRejected):
        GroundedGenerationService(FakeGenerator(payload)).answer(
            "What do the notes establish about travel?", document
        )


def test_generation_keeps_structural_rejection_distinct_from_grounding_abstention():
    client = FakeGenerator({"unexpected": True})
    service = GroundedGenerationService(client)
    with pytest.raises(GenerationRejected, match="required structure") as rejected:
        service.answer("What happened?", EVIDENCE)
    assert not isinstance(rejected.value, GenerationGroundingRejected)
    assert [call["grounding_repair"] for call in client.calls] == [False]


def test_generation_omits_negation_reversal_and_low_coverage_claims():
    evidence = EVIDENCE + (
        EvidenceItem(
            "S3",
            "Interview notes.docx",
            "Section 5",
            "Officer Lee did not describe who placed the canvas bag in the vehicle.",
        ),
    )
    service = GroundedGenerationService(
        FakeGenerator(
            {
                "answerable": True,
                "claims": [
                    {
                        "text": "Officer Daniels reported seeing the canvas bag at 10:07 p.m. before activating emergency lights.",
                        "evidence_ids": ["S1"],
                    },
                    {
                        "text": "Officer Lee described who placed the canvas bag in the vehicle.",
                        "evidence_ids": ["S3"],
                    },
                    {
                        "text": "The bag was transferred to a laboratory for testing.",
                        "evidence_ids": ["S1"],
                    },
                ],
                "limitation": None,
                "missing_information": "",
            }
        )
    )
    answer = service.answer("What happened to the bag?", evidence)
    assert [claim.text for claim in answer.claims] == [
        "Officer Daniels reported seeing the canvas bag at 10:07 p.m. before activating emergency lights."
    ]
    assert answer.omitted_claims == 2


def test_empty_evidence_abstains_without_calling_model():
    client = FakeGenerator({})
    answer = GroundedGenerationService(client).answer("Who signed the report?", ())
    assert answer.answerable is False
    assert answer.model_called is False
    assert client.calls == []


def test_unavailable_generator_is_visible_and_never_extractively_substituted():
    service = GroundedGenerationService(UnavailableGenerator())
    with pytest.raises(GenerationUnavailable, match="Search and source review"):
        service.answer("Compare the accounts", EVIDENCE)


def test_ollama_gateway_uses_loopback_structured_chat_contract():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # pragma: no cover - keep test output quiet
            return

        def do_GET(self):
            assert self.path == "/api/tags"
            body = json.dumps({"models": [{"name": "fixture-generator"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/api/chat"
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            content = json.dumps(
                {
                    "answerable": True,
                    "claims": [
                        {
                            "text": "Officer Daniels reported seeing the canvas bag at 10:07 p.m. before activating emergency lights.",
                            "evidence_ids": ["S1"],
                        }
                    ],
                    "limitation": None,
                    "missing_information": "",
                }
            )
            body = json.dumps({"message": {"content": content}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OllamaGenerator(f"http://127.0.0.1:{server.server_port}", "fixture-generator")
        assert client.available is True
        answer = GroundedGenerationService(client).answer("When did Daniels see the bag?", EVIDENCE)
        assert answer.answerable is True and answer.model_called is True
        assert len(requests) == 1
        assert requests[0]["model"] == "fixture-generator"
        assert requests[0]["format"]["required"] == [
            "answerable", "claims", "limitation", "missing_information"
        ]
        assert "Incident reports.pdf" in requests[0]["messages"][1]["content"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_generator_endpoint_must_use_an_explicitly_allowed_host():
    with pytest.raises(ValueError, match="explicit host|exact host allowed"):
        OllamaGenerator("https://example.com", "forbidden")


def test_vllm_qwen_request_disables_thinking_and_keeps_strict_schema(monkeypatch):
    import case_intelligence.generation as module

    captured = {}

    def request(url, payload, **kwargs):
        captured.update({"url": url, "payload": payload, **kwargs})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answerable": False,
                                "claims": [],
                                "limitation": None,
                                "missing_information": "The supplied sources do not answer the question.",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(module, "_bounded_json_request", request)
    client = OpenAICompatibleGenerator(
        "http://127.0.0.1:8790",
        "case-intelligence-qwen35-9b",
        disable_thinking=True,
    )
    result = client.generate(question="What is missing?", evidence=EVIDENCE)
    assert result["answerable"] is False
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["payload"]["response_format"]["json_schema"]["strict"] is True
    assert "claims must be an empty array" in captured["payload"]["messages"][0]["content"]

    client.generate(
        question="What is missing?",
        evidence=EVIDENCE,
        grounding_repair=True,
    )
    messages = captured["payload"]["messages"]
    assert "source-close grounding repair pass" in messages[0]["content"]
    assert "Source-close repair requirement" in messages[1]["content"]

    def review_request(url, payload, **kwargs):
        captured.update({"url": url, "payload": payload, **kwargs})
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "not_identified",
                                "rationale": "No express match was identified.",
                                "evidence_ids": [],
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(module, "_bounded_json_request", review_request)
    result = client.classify_source(
        criterion="Include helicopter references.",
        include_guidance="Require an express reference.",
        exclude_guidance="Do not infer one.",
        evidence=EVIDENCE,
    )
    assert result["decision"] == "not_identified"
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["payload"]["response_format"]["json_schema"]["name"] == "source_review_decision"
    assert "checklist of every required" in captured["payload"]["messages"][0]["content"]
    assert "request or plan is not a completed event" in captured["payload"]["messages"][0]["content"]
