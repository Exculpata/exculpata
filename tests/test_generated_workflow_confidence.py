"""Synthetic workflow copy must describe attempts and retrieval without truth guarantees."""
import io

import pytest

from case_intelligence.generation import GenerationUnavailable
from case_intelligence.report_compilation import compile_report
from tests.test_report_compilation import material
from tests.test_report_review_basis import workspace  # noqa: F401 - pytest fixture registration

READY_HINT = "Generated answers use selected passages; check claims against the original sources."
ABSTENTION_HINT = "These retrieved passages need direct review; no generated answer is shown."


def test_ready_assistant_requests_source_review(workspace):
    client, bench, matter = workspace
    document, _ = bench.source_store(matter).store_stream(
        "synthetic-readiness.txt", "text/plain", io.BytesIO(b"The synthetic note records a blue bicycle."))
    bench._sync_source_catalog(matter, [document])
    page = client.get(f"/matters/{matter.slug}/assistant")
    assert page.status_code == 200
    assert READY_HINT in page.text
    assert "Answers stay grounded" not in page.text


def test_served_readiness_script_uses_the_same_review_caution(workspace):
    client, _, _ = workspace
    script = client.get("/static/case-intelligence.js")
    assert script.status_code == 200
    assert READY_HINT in script.text
    assert "Answers stay grounded" not in script.text


def test_abstention_explains_retrieved_passages_without_a_verified_answer_claim(workspace):
    client, bench, matter = workspace
    document, _ = bench.source_store(matter).store_stream(
        "synthetic-related-passage.txt", "text/plain", io.BytesIO(b"The synthetic note records a blue bicycle."))
    bench._sync_source_catalog(matter, [document])
    citation = bench._citation(matter, bench._candidate(matter, document, document.parsed_units()[0], 1))
    conversation = bench.workspace.get_conversation(matter.matter_id)
    payload = {"kind": "not-supported", "missing_information": "The synthetic request needs direct review.",
        "source_matches": [citation.staff_payload()]}
    saved = bench.workspace.append_message(matter.matter_id, conversation.conversation_id,
        "assistant", payload["missing_information"], payload)
    page = client.get(f"/matters/{matter.slug}")
    assert page.status_code == 200
    assert ABSTENTION_HINT in page.text
    assert "verified generated answer" not in page.text
    assert citation.source_name in page.text and citation.support_token in page.text
    assert bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1] == saved


@pytest.mark.parametrize("stage", ["source", "classification"])
def test_unavailable_compilation_calls_are_labelled_attempted(stage):
    class FailedService:
        available = True

        def answer(self, *args, **kwargs):
            raise GenerationUnavailable("Synthetic unavailable service")

    item = material(origin="human", citations=()) if stage == "classification" else material()
    draft = compile_report("topic", "delivery", (item,), FailedService())
    assert draft.coverage["model_calls"] == draft.coverage["unavailable_model_calls"] == 1
    assert draft.coverage["rejected_model_calls"] == 0
    # Preserve the established machine-readable identifier and outcome counters.
    assert draft.coverage["model_call_unit"] == "verified_answer_service_call"
    assert draft.coverage["mode"] == "unfiltered_saved_material_arrangement"
    assert draft.coverage["stop_reason"] == "analysis_incomplete"
    ledger = draft.sections[-1]["body"]
    assert "Answer-service calls attempted: 1" in ledger
    assert "Verified answer-service calls" not in ledger
    assert "unavailable model calls: 1" in ledger
    assert draft.sections[0]["body"].startswith(item.text)
