"""Resealed synthetic captures still require consistent declared provenance."""
import copy
import socket

import pytest

from case_intelligence import claim_evaluation as evaluation
from case_intelligence import generation


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Provenance regressions must never contact a model or network.")
    monkeypatch.setattr(generation.urllib.request, "urlopen", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


class SyntheticClient(generation.OpenAICompatibleGenerator):
    def __init__(self):
        super().__init__("http://127.0.0.1:11435", "synthetic-provenance-only")

    def generate(self, *, question, evidence):
        return {"answerable": True, "claims": [
            {"text": evidence[0].excerpt, "evidence_ids": [evidence[0].evidence_id]},
        ], "limitation": None, "missing_information": ""}


def make_capture(profile="portable", offline="not_exercised"):
    pinned = evaluation.model_records(profile, generator_exercised=False)["components"][0]
    runtime = {
        "runtime_name": "synthetic", "runtime_version": "test-only",
        "accelerator": "none", "accelerator_memory_gib": 0, "driver": "none",
        "model_artifact_sha256": "a" * 64, "offline_readiness": offline,
        "offline_evidence_sha256": "b" * 64 if offline == "operator_attested" else None,
        "upstream_model_id": pinned["model_id"], "upstream_revision": pinned["revision"],
        "license": pinned["license"],
    }
    client = SyntheticClient()
    def check_identity():
        return {"method": "api_model_id_snapshots",
                "model": client.model, "artifact_sha256": None}

    return evaluation.capture(client, profile=profile, runtime=runtime, repetitions=1,
        identity_check=check_identity)


def completed_grades(receipt):
    grades = evaluation.grade_template(receipt)
    grades["grader_id"] = "synthetic-provenance-grader"
    for sample in grades["samples"]:
        sample.update(answer_correct=False, rationale="Synthetic grading, not model evidence.")
        for claim in sample["claims"]:
            claim.update(semantic_valid=False, citations_valid=True,
                         rationale="Conservative synthetic verdict independent of text checks.")
    return grades


def check_grading(receipt, grades, operation):
    if operation == "grade_template":
        return evaluation.grade_template(receipt)
    # Rebind grades deliberately: this regression concerns semantic validation
    # after a new checksum, not the already-covered stale checksum rejection.
    grades["capture_sha256"] = receipt["receipt_sha256"]
    return evaluation.score_capture(receipt, grades)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("field,value", [
    ("upstream_model_id", "synthetic/other-model"),
    ("upstream_revision", "c" * 40),
    ("license", "MIT"),
    ("runtime_name", ""),
    ("accelerator_memory_gib", -1),
    ("model_artifact_sha256", "not-a-digest"),
    ("offline_readiness", "independently_verified"),
    ("offline_evidence_sha256", "b" * 64),
    ("basis", "independently_verified"),
    ("unexpected", "synthetic-extra-field"),
])
def test_resealed_runtime_conflicts_fail_both_grading_paths(operation, field, value):
    receipt = make_capture()
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    receipt["runtime"][field] = value
    with pytest.raises(ValueError):
        check_grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("field", ["basis", "runtime_version", "upstream_revision", "license"])
def test_resealed_missing_runtime_fields_fail_both_grading_paths(operation, field):
    receipt = make_capture()
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    del receipt["runtime"][field]
    with pytest.raises(ValueError):
        check_grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("runtime_field,model_field,value", [
    ("upstream_model_id", "model_id", "synthetic/other-model"),
    ("upstream_revision", "revision", "c" * 40),
    ("license", "license", "MIT"),
])
def test_resealing_runtime_and_model_record_cannot_replace_manifest_pin(
    operation, runtime_field, model_field, value,
):
    receipt = make_capture()
    grades = completed_grades(receipt)
    receipt.pop("receipt_sha256")
    receipt["runtime"][runtime_field] = value
    receipt["models"]["components"][0][model_field] = value
    with pytest.raises(ValueError, match="provenance"):
        check_grading(evaluation.seal(receipt), grades, operation)


@pytest.mark.parametrize("selected", [None, {}, {"profile": "unknown"}, {"profile": []}])
def test_malformed_selected_generator_is_a_controlled_validation_error(selected):
    receipt = make_capture()
    receipt.pop("receipt_sha256")
    receipt["models"]["components"][0] = selected
    with pytest.raises(ValueError, match="model profile"):
        evaluation.grade_template(evaluation.seal(receipt))


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("profile", ["portable", "quality"])
@pytest.mark.parametrize("offline", ["not_exercised", "operator_attested"])
def test_valid_declared_provenance_remains_gradable_without_claiming_attestation(
    operation, profile, offline,
):
    receipt = make_capture(profile, offline)
    original = copy.deepcopy(receipt)
    result = check_grading(receipt, completed_grades(receipt), operation)
    assert receipt == original
    assert receipt["generation"]["attempts"] == receipt["generation"]["generated"] == 11
    assert receipt["runtime"]["basis"] == "operator_declared_not_independently_verified"
    assert receipt["runtime_identity"]["artifact_sha256"] is None
    assert "do not attest loaded weights" in receipt["runtime_identity"]["limitation"]
    if operation == "grade_template":
        assert len(result["samples"]) == 11
    else:
        assert result["generation"]["failed_attempts"] == evaluation.fraction(0, 11)
