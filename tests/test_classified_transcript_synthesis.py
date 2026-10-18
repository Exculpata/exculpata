"""Synthetic classification -> saved finding -> synthesis admission contracts."""
from copy import deepcopy

import pytest

from case_intelligence.full_text_synthesis import validate_prepared
from case_intelligence.generation import EvidenceItem, GroundedGenerationService, verify_original_claim
from case_intelligence.hierarchical_synthesis import synthesis_answer, validate_state
from tests.test_full_text_synthesis_adapter import ledger


STATEMENT = "Synthetic dispatch speaker reported that the blue crate was delivered."
CAUTION = "The machine transcript appears to say that "


class ExactClassifier:
    available = True

    def classify_source(self, **kwargs):
        return {"decision": "include", "rationale": STATEMENT, "evidence_ids": ["S1"]}

    def generate(self, **kwargs):
        return {"answerable": True, "claims": [
            {"text": (CAUTION if source.evidence_kind == "transcript" else "") + source.excerpt,
             "evidence_ids": [source.evidence_id]} for source in kwargs["evidence"]],
            "limitation": None, "missing_information": ""}


@pytest.mark.parametrize("kind", ["document", "transcript"])
def test_exact_classification_survives_saved_admission_and_both_synthesis_levels(ledger, kind):
    generator = GroundedGenerationService(ExactClassifier())
    classified = generator.classify_source(criterion="Identify synthetic delivery accounts.",
        include_guidance="", exclude_guidance="",
        evidence=[EvidenceItem("S1", "Synthetic dispatch", "unit 1", STATEMENT, kind)])
    assert classified.rationale == STATEMENT
    ledger.add(1, STATEMENT, rationale=classified.rationale)
    ledger.originals[1]["evidence_kind"] = kind
    prepared = ledger.prepare()
    receipt = validate_prepared(prepared)
    assert receipt["outcomes"] == [[1, "admitted"]]
    assert not receipt["partial"]
    expected = (CAUTION if kind == "transcript" else "") + STATEMENT
    assert prepared["passes"][0]["text"] == expected
    assert prepared["evidence"][0] == ledger.originals[1]
    assert ledger.connection.execute("SELECT rationale FROM workbench_text_review_chunk").fetchone()[0] == STATEMENT
    ledger.service.generator = generator
    saved = []
    result = ledger.service.run(receipt["question"], prepared, lambda state: saved.append(deepcopy(state)),
        lambda: ledger.check(receipt), now=lambda: 1000)
    validate_state(result, prepared["passes"], prepared["evidence"], final=True)
    assert not result["partial"]
    answer = synthesis_answer(result, prepared["evidence"])
    assert [claim.text for claim in answer.claims] == [expected]
    assert answer.used_evidence_ids == ("S1",)
    assert bool(answer.evidence_notice) == (kind == "transcript")


@pytest.mark.parametrize("rationale", [STATEMENT, CAUTION + STATEMENT])
def test_legacy_raw_and_already_cautioned_transcript_findings_admit_without_rewrite(ledger, rationale):
    ledger.add(1, STATEMENT, rationale=rationale)
    ledger.originals[1]["evidence_kind"] = "transcript"
    prepared = ledger.prepare()
    assert prepared["full_text_synthesis_input"]["outcomes"] == [[1, "admitted"]]
    assert prepared["passes"][0]["text"] == CAUTION + STATEMENT
    assert ledger.connection.execute("SELECT rationale FROM workbench_text_review_chunk").fetchone()[0] == rationale


@pytest.mark.parametrize("rationale", [
    "Synthetic dispatch speaker reported that the red crate was delivered.",
    "Synthetic dispatch speaker reported that the blue crate was not delivered.",
    "Synthetic dispatch speaker reported 999 crates were delivered.",
    "Synthetic dispatch speaker reported crate was delivered.",
    "[S1] " + STATEMENT,
    "",
])
def test_admission_does_not_caution_unsupported_or_rewritten_transcript_findings(ledger, rationale):
    ledger.add(1, STATEMENT, rationale=rationale)
    ledger.originals[1]["evidence_kind"] = "transcript"
    prepared = ledger.prepare()
    assert prepared["full_text_synthesis_input"]["outcomes"] == [[1, "unsupported_finding"]]
    assert prepared["full_text_synthesis_input"]["partial"]
    assert prepared["passes"] == prepared["evidence"] == []


def test_caution_does_not_relax_answer_verifier_or_admit_document_as_transcript(ledger):
    evidence = {"S1": EvidenceItem("S1", "Synthetic transcript", "unit 1", STATEMENT, "transcript")}
    assert verify_original_claim(STATEMENT, ["S1"], evidence) is None
    ledger.add(1, STATEMENT, rationale=CAUTION + STATEMENT)
    assert ledger.prepare()["full_text_synthesis_input"]["outcomes"] == [[1, "unsupported_finding"]]


def test_caution_preserves_existing_claim_length_limit_without_truncation(ledger):
    statement = "Synthetic dispatch " + "blue crate " * 68 + "delivered."
    assert len(statement) <= 800 < len(CAUTION + statement)
    ledger.add(1, statement)
    ledger.originals[1]["evidence_kind"] = "transcript"
    assert ledger.prepare()["full_text_synthesis_input"]["outcomes"] == [[1, "unsupported_finding"]]
