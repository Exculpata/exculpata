"""Synthetic contracts for measurements, never model-quality expectations."""
import ast
from copy import deepcopy
import inspect
import re

import pytest

from case_intelligence import tier_evals as tiers


def card(status="unmeasured"):
    result = tiers.new_scorecard("a" * 40, "synthetic command", {"cpu": "synthetic CPU", "gpus": []})
    if status != "unmeasured":
        suite = tiers.evaluation.load_suite()
        consumer = status == "consumer"
        if consumer:
            result["hardware"]["gpus"] = [dict(name="synthetic GeForce", vram_mib=16384)]
        result["tiers"][int(consumer)].update(
            status="partial" if consumer else status, reason="synthetic retrieval only", wall_seconds=1.0,
            retrieval=dict(correct=0, total=sum(bool(q.required_facts) for q in suite.questions), rate=0.0),
            answer_accuracy=dict(correct=0, total=len(suite.questions), rate=0.0) if status == "measured" else None,
            execution=dict(embedding="SentenceTransformer", reranker="CrossEncoder", devices=["cuda:0"] if consumer else ["cpu"],
                           generator="vllm" if status == "measured" else None))
    return result


# Each change preserves schema validity and violates exactly one semantic guard.
GUARDS = [
    ("inventory", "unmeasured", ("hardware", "cpu"), "", "hardware inventory is required"),
    ("rows", "unmeasured", ("tiers",), [], "one row per tier is required"),
    ("suite", "unmeasured", ("fingerprint",), "0" * 64, "scorecard must identify the executed versioned suite"),
    ("pins", "unmeasured", ("tiers", 0, "models"), {}, "tier settings and pinned model provenance are required"),
    ("metric", "partial", ("tiers", 0, "retrieval", "rate"), 1.0, "metric counts and rate disagree"),
    ("unmeasured", "unmeasured", ("tiers", 0, "reason"), "", "unmeasured tiers require a reason and reject scores"),
    ("hardware", "consumer", ("hardware", "gpus"), [], "measurements require matching hardware and completed retrieval"),
    ("coverage", "partial", ("tiers", 0, "retrieval", "total"), 1, "measurements must cover the complete question set"),
    ("execution", "partial", ("tiers", 0, "execution", "embedding"), "StandIn", "stand-in models or mismatched execution devices cannot be measurements"),
    ("partial", "partial", ("tiers", 0, "execution", "generator"), "vllm", "partial means retrieval only, with a reason"),
    ("measured", "measured", ("tiers", 0, "execution", "generator"), None, "measured requires real generation and answer scores"),
]


def damaged_card(status, path, value):
    result = card(status)
    tiers.validate_scorecard(result)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = deepcopy(value)
    return result


@pytest.mark.parametrize("name,status,path,value,message", GUARDS, ids=[g[0] for g in GUARDS])
def test_validator_guard(name, status, path, value, message):
    result = damaged_card(status, path, value)
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        tiers.validate_scorecard(result)


@pytest.mark.parametrize("name,status,path,value,message", GUARDS, ids=[g[0] for g in GUARDS])
def test_guard_mutation_is_killed(monkeypatch, name, status, path, value, message):
    result = damaged_card(status, path, value)
    tree = ast.parse(inspect.getsource(tiers.validate_scorecard))
    messages = {node.exc.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Raise)}
    assert messages == {guard[4] for guard in GUARDS}
    guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
              and any(isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call)
                      and child.exc.args and isinstance(child.exc.args[0], ast.Constant)
                      and child.exc.args[0].value == message for child in node.body)]
    assert len(guards) == 1
    guards[0].test = ast.Constant(False)
    namespace = vars(tiers).copy()
    exec(compile(ast.fix_missing_locations(tree), "<synthetic guard mutation>", "exec"), namespace)
    disabled = namespace["validate_scorecard"]
    disabled(result)  # Acceptance proves no other guard rejects this fixture.
    monkeypatch.setattr(tiers, "validate_scorecard", disabled)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_validator_guard(name, status, path, value, message)


def test_schema_and_profile_settings():
    result = card()
    tiers.validate_scorecard(result)
    assert (result["schema_version"], result["top_k"], result["suite_version"]) == (1, 8, "2")
    assert {row["tier"]: row["profile_ids"] for row in result["tiers"]} == {
        "potato": ["potato", "cpu"], "consumer": ["gpu-16", "gpu-24"], "ultra": ["ultra"]}
    assert all(row["settings"] for row in result["tiers"])
    for status in ("partial", "measured"):
        tiers.validate_scorecard(card(status))


@pytest.mark.parametrize("status,path,value", [
    ("unmeasured", ("tiers", 0, "retrieval"), dict(correct=1, total=1, rate=1.0)),
    ("unmeasured", ("tiers", 0, "answer_accuracy"), dict(correct=1, total=1, rate=1.0)),
    ("partial", ("tiers", 0, "execution", "reranker"), "DeterministicReranker"),
    ("partial", ("tiers", 0, "execution", "generator"), "ReferenceStub"),
    ("partial", ("tiers", 0, "execution", "devices"), ["cuda:0"]),
    ("partial", ("tiers", 0, "models", "embedding"), dict(model_id="synthetic", license="synthetic")),
    ("partial", ("tiers", 0, "retrieval"), dict(correct=4, total=3, rate=1.3333)),
    ("partial", ("tiers", 0, "retrieval"), dict(correct=4, total=3, rate=1.0)),
    ("partial", ("tiers", 0, "wall_seconds"), float("nan")),
    ("partial", ("tiers", 0, "status"), "measured"),
    ("measured", ("tiers", 0, "answer_accuracy"), dict(correct=0, total=1, rate=0.0)),
])
def test_schema_and_guard_variants(status, path, value):
    result = damaged_card(status, path, value)
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


@pytest.mark.parametrize("field", ["commit", "command", "hardware", "fingerprint", "started_at", "source_sha256"])
def test_provenance_required(field):
    result = card("partial")
    del result[field]
    with pytest.raises(ValueError):
        tiers.validate_scorecard(result)


@pytest.mark.parametrize("field", ["models", "wall_seconds", "execution", "reason"])
def test_tier_provenance_required(field):
    result = card("partial")
    del result["tiers"][0][field]
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
    result["schema_version"] = 99
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
