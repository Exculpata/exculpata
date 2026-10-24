"""Synthetic contracts for measurements, never model-quality expectations."""
from copy import deepcopy

import pytest

from case_intelligence import tier_evals as tiers


def card():
    return tiers.new_scorecard("a" * 40, "synthetic command", {"cpu": "synthetic CPU", "gpus": []})


def measured():
    result = card()
    row = result["tiers"][0]
    row.update(status="partial", retrieval={"correct": 2, "total": 34, "rate": 0.0588},
               answer_accuracy=None, reason="generator unavailable", wall_seconds=1.0,
               execution={"embedding": "SentenceTransformer", "reranker": "CrossEncoder",
                          "devices": ["cpu"], "generator": None})
    return result


def test_schema_and_profile_settings():
    result = card()
    tiers.validate_scorecard(result)
    assert result["schema_version"] == 1
    assert result["top_k"] == 8
    assert result["suite_version"] == "2"
    assert [row["tier"] for row in result["tiers"]] == ["potato", "consumer", "ultra"]
    assert result["tiers"][0]["profile_ids"] == ["cpu", "cpu-16"]
    assert result["tiers"][1]["profile_ids"] == ["gpu-16", "gpu-24"]
    assert result["tiers"][2]["profile_ids"] == []
    assert all(row["settings"] for row in result["tiers"])
    tiers.validate_scorecard(measured())


@pytest.mark.parametrize("field", ["retrieval", "answer_accuracy"])
def test_unmeasured_rejects_numbers(field):
    result = card()
    result["tiers"][0][field] = {"correct": 1, "total": 1, "rate": 1.0}
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


@pytest.mark.parametrize("role,standin", [
    ("embedding", "DeterministicEmbeddingAdapter"), ("reranker", "DeterministicReranker"),
    ("generator", "ReferenceStub"), ("embedding", "UnknownModel"),
])
def test_standins_rejected(role, standin):
    result = measured()
    result["tiers"][0]["execution"][role] = standin
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


@pytest.mark.parametrize("field", ["commit", "command", "hardware", "fingerprint", "started_at", "source_sha256"])
def test_provenance_required(field):
    result = measured()
    del result[field]
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


@pytest.mark.parametrize("field", ["models", "wall_seconds", "execution", "reason"])
def test_tier_provenance_required(field):
    result = measured()
    del result["tiers"][0][field]
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


def test_revisions_and_hardware_are_required():
    result = measured()
    del result["tiers"][0]["models"]["embedding"]["revision"]
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)
    result = measured()
    result["tiers"][0]["execution"]["devices"] = ["cuda:0"]
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


@pytest.mark.parametrize("tier,gpus,expected", [
    ("potato", [], True), ("consumer", [], False),
    ("consumer", [{"name": "NVIDIA GeForce RTX 4090", "vram_mib": 24564}], True),
    ("consumer", [{"name": "NVIDIA L40S", "vram_mib": 46080}], False),
    ("ultra", [{"name": "NVIDIA L40S", "vram_mib": 46080}] * 2, True),
])
def test_hardware_matching(tier, gpus, expected):
    assert tiers.hardware_matches(tier, {"cpu": "synthetic CPU", "gpus": gpus}) is expected


def test_output_roundtrip_and_unknown_schema(tmp_path):
    import json
    result = card()
    tiers.write_scorecard(tmp_path, result)
    assert json.loads((tmp_path / "tier-scorecard.json").read_text()) == result
    assert "unmeasured" in (tmp_path / "tier-scorecard.md").read_text()
    broken = deepcopy(result)
    broken["schema_version"] = 99
    with pytest.raises(ValueError):
        tiers.validate_scorecard(broken)


@pytest.mark.parametrize("field,value", [
    ("retrieval", {"correct": 4, "total": 3, "rate": 1.3333}),
    ("retrieval", {"correct": 1, "total": 3, "rate": 1.0}),
    ("wall_seconds", float("nan")), ("status", "measured"),
    ("answer_accuracy", {"correct": 1, "total": 1, "rate": 1.0}),
])
def test_inconsistent_or_nonfinite_measurements_rejected(field, value):
    result = measured()
    result["tiers"][0][field] = value
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


def test_startup_failure_retains_reason_without_scores(tmp_path, monkeypatch):
    def unavailable(row):
        raise ModuleNotFoundError("synthetic missing dependency")
    monkeypatch.setattr(tiers, "_worker_models", unavailable)
    result = card()
    row = result["tiers"][0]
    tiers.evaluate_tier(row, result["hardware"], tmp_path, False)
    assert row["status"] == "unmeasured"
    assert row["retrieval"] is row["answer_accuracy"] is None
    assert "ModuleNotFoundError" in row["reason"]
    assert row["wall_seconds"] >= 0
    tiers.validate_scorecard(result)
