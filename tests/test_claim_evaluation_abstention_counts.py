"""Count recorded production abstentions without losing raw evaluation samples."""
import socket

import pytest

from case_intelligence import claim_evaluation as evaluation
from case_intelligence import generation


@pytest.fixture
def capture_outputs(monkeypatch):
    def deny_network(*args, **kwargs):
        pytest.fail("Synthetic abstention accounting must not contact a model or network.")

    monkeypatch.setattr(generation.urllib.request, "urlopen", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    model = evaluation.model_records("portable", generator_exercised=False)["components"][0]
    runtime = {"runtime_name": "synthetic", "runtime_version": "test-only",
        "accelerator": "none", "accelerator_memory_gib": 0, "driver": "none",
        "model_artifact_sha256": "a" * 64, "offline_readiness": "not_exercised",
        "offline_evidence_sha256": None, "upstream_model_id": model["model_id"],
        "upstream_revision": model["revision"], "license": model["license"]}

    def run(output):
        class SyntheticClient(generation.OpenAICompatibleGenerator):
            calls = 0

            def generate(self, *, question, evidence):
                self.calls += 1
                return output(self.calls, evidence)

        client = SyntheticClient("http://127.0.0.1:11435", "synthetic-test-only")
        def check_identity():
            return {"method": "api_model_id_snapshots",
                "model": client.model, "artifact_sha256": None}

        return evaluation.capture(client, profile="portable", runtime=runtime, repetitions=1,
            identity_check=check_identity)

    return run


def abstention():
    return {"answerable": False, "claims": [], "limitation": None, "missing_information": ""}


def source_claim(evidence):
    return {"text": evidence[0].excerpt, "evidence_ids": [evidence[0].evidence_id]}


@pytest.mark.parametrize("shape,schema_valid,state,slots", [
    ("contradictory_claim", True, "rejected", ["claim-0"]),
    ("malformed_missing", False, "rejected", []),
    ("ordinary_abstention", True, "abstained", []),
    ("ignored_malformed_limitation", False, "abstained", ["limitation"]),
])
def test_capture_abstention_count_follows_recorded_verifier_not_raw_flag(
    capture_outputs, shape, schema_valid, state, slots,
):
    def output(call, evidence):
        raw = abstention()
        if shape == "contradictory_claim":
            raw["claims"] = [source_claim(evidence)]
        elif shape == "malformed_missing":
            raw["missing_information"] = None
        elif shape == "ignored_malformed_limitation":
            # Preserve production behavior: this field is ignored on abstention,
            # while JSON-schema validity and the raw grading slot stay separate.
            raw["limitation"] = 42
        return raw

    receipt = capture_outputs(output)
    assert len(receipt["results"]) == 11
    for row in receipt["results"]:
        assert row["state"] == "generated"
        assert row["raw"]["answerable"] is False
        assert row["schema_valid"] is schema_valid
        assert [claim["slot"] for claim in row["verification"]["claims"]] == slots
        for path in ("ordinary", "exact_span"):
            assert row["verification"]["response"][path]["state"] == state
            assert all(claim[path + "_disposition"] == "response_" + state
                       for claim in row["verification"]["claims"])
    assert receipt["generation"] == {"status": "requires_independent_human_grading",
        "attempts": 11, "generated": 11, "schema_invalid": 0 if schema_valid else 11,
        "abstained": 11 if state == "abstained" else 0}
    evaluation.validate_receipt(receipt)
    grades = evaluation.grade_template(receipt)
    assert len(grades["samples"]) == 11
    assert all([claim["slot"] for claim in sample["claims"]] == slots
               for sample in grades["samples"])


def test_mixed_capture_keeps_failure_and_raw_occurrence_denominators(capture_outputs):
    def output(call, evidence):
        if call == 4:
            raise generation.GenerationUnavailable("Synthetic unavailable response")
        raw = abstention()
        if call in (1, 3):
            raw["answerable"] = call == 3
            raw["claims"] = [source_claim(evidence), source_claim(evidence)]
        return raw

    receipt = capture_outputs(output)
    assert receipt["results"][0]["verification"]["response"]["ordinary"]["state"] == "rejected"
    assert receipt["generation"] == {"status": "requires_independent_human_grading",
        "attempts": 11, "generated": 10, "schema_invalid": 0, "abstained": 8}
    grades = evaluation.grade_template(receipt)
    grades["grader_id"] = "synthetic-grader"
    assert len(grades["samples"]) == 10
    assert sum(len(sample["claims"]) for sample in grades["samples"]) == 4
    for index, sample in enumerate(grades["samples"]):
        sample.update(answer_correct=index == 2, rationale="Synthetic complete-source grading.")
        for claim in sample["claims"]:
            claim.update(semantic_valid=True, citations_valid=True,
                         rationale="Exact synthetic source passage and citation.")
    score = evaluation.score_capture(receipt, grades)
    assert score["generation"]["failed_attempts"] == evaluation.fraction(1, 11)
    assert score["generation"]["answer_error_among_generated"] == evaluation.fraction(9, 10)
    assert score["generation"]["semantic_claim_errors"] == evaluation.fraction(0, 4)
    for path in ("ordinary", "exact_span"):
        assert score["verifier"][path]["duplicate_omissions"] == evaluation.fraction(1, 4)
        assert score["verifier"][path]["false_rejection"] == evaluation.fraction(3, 4)
