"""The shipped reranker is consistent across the catalog and hardware profiles."""
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RERANKER = "ibm-granite/granite-embedding-reranker-english-r2"


def test_default_reranker_is_granite_english_r2():
    catalog = json.loads((ROOT / "config/models.json").read_text())
    rerankers = [model for model in catalog["models"] if model["role"] == "reranker"]
    assert [model["model_id"] for model in rerankers] == [RERANKER]


@pytest.mark.parametrize("profile", json.loads(
    (ROOT / "config/model_profiles.json").read_text()
)["profiles"], ids=lambda profile: profile["id"])
def test_profile_reranker_is_granite_english_r2(profile):
    assert profile.get("reranker_model") == RERANKER


def test_reranker_runtime_and_deployment_pins_match_catalog():
    from case_intelligence import model_portfolio, retrieval_worker, review_bench_v2

    catalog = json.loads((ROOT / "config/models.json").read_text())
    pin = next(model for model in catalog["models"] if model["role"] == "reranker")
    assert review_bench_v2.DEFAULT_RERANKER_MODEL == RERANKER
    assert retrieval_worker.RERANKER_MODEL == RERANKER
    assert retrieval_worker.RERANKER_REVISION == pin["revision"]
    assert model_portfolio.APPROVED_RERANKER_REVISION == pin["revision"]
    for filename in ("compose.yaml", ".env.example"):
        text = (ROOT / filename).read_text()
        assert RERANKER in text
        assert pin["revision"] in text


def test_reranker_lineage_is_not_an_evaluation_pass():
    from case_intelligence.model_qualification import (
        UNVALIDATED_MODEL_LABEL, apply_harness_result, load_qualifications,
        qualification_label, qualification_schema,
    )

    registry = load_qualifications()
    assert registry == load_qualifications(ROOT / "config/model_qualifications.json")
    assert qualification_schema() == qualification_schema(ROOT / "config/model_qualifications.schema.json")
    metadata = registry["model_metadata"]
    rerankers = [model for model in metadata if model["model_id"] == RERANKER]
    assert len(rerankers) == 1
    model = rerankers[0]
    catalog = json.loads((ROOT / "config/models.json").read_text())
    pin = next(model for model in catalog["models"] if model["role"] == "reranker")
    assert (model["revision"], model["license"]) == (pin["revision"], "Apache-2.0")
    assert model["training_data_lineage"]
    assert model["source"] == f"https://huggingface.co/{RERANKER}/blob/{pin['revision']}/README.md"
    assert qualification_label(RERANKER, revision="sha256:" + "0" * 64, registry=registry) == UNVALIDATED_MODEL_LABEL
    updated = apply_harness_result(registry, model_id="synthetic/model", harness="synthetic", passed=False)
    assert updated["model_metadata"] == metadata
