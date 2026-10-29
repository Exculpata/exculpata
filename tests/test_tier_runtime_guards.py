"""Synthetic runtime guard probes; fake backends never establish measurements."""
from contextlib import nullcontext
from types import SimpleNamespace
import sys

import pytest

from case_intelligence import retrieval_worker as worker, tier_evals as tiers
from tests.test_tier_runner import card


@pytest.mark.parametrize("damage", [None, "embedding-pin", "reranker-pin", "embedding-type", "reranker-type"])
def test_worker_guards(monkeypatch, damage):
    class SentenceTransformer:
        device = "cpu"

    class CrossEncoder:
        model = SimpleNamespace(device="cpu")

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(
        SentenceTransformer=SentenceTransformer, CrossEncoder=CrossEncoder))
    def embedding():
        return object() if damage == "embedding-type" else SentenceTransformer()

    def reranker():
        return object() if damage == "reranker-type" else CrossEncoder()

    monkeypatch.setattr(worker, "_embedding_model", embedding)
    monkeypatch.setattr(worker, "_reranker_model", reranker)
    monkeypatch.setattr(worker, "_embedding", None)
    monkeypatch.setattr(worker, "_reranker", None)
    monkeypatch.setenv("CASE_REVIEW_MODEL_DEVICE", "synthetic-original")
    row = card()["tiers"][0]
    if damage and damage.endswith("-pin"):
        row["models"][damage.split("-")[0]]["revision"] = "b" * 40
    if damage:
        with pytest.raises(ValueError, match="tier pins|stand-in worker"):
            tiers._worker_models(row)
    else:
        assert tiers._worker_models(row)[2] == ["cpu"]
    assert tiers.os.environ["CASE_REVIEW_MODEL_DEVICE"] == "synthetic-original"


@pytest.mark.parametrize("damage", [None, "device", "parallel", "type"])
def test_generator_guards(monkeypatch, damage):
    class LLM:
        def __new__(cls, **kwargs):
            instance = object.__new__(StandIn if damage == "type" else cls)
            instance.llm_engine = SimpleNamespace(vllm_config=SimpleNamespace(
                device_config=SimpleNamespace(device="cuda" if damage == "device" else "cpu"),
                parallel_config=SimpleNamespace(tensor_parallel_size=2 if damage == "parallel" else 1)))
            return instance

    class StandIn(LLM):
        pass

    monkeypatch.setitem(sys.modules, "vllm", SimpleNamespace(LLM=LLM, SamplingParams=lambda **kwargs: kwargs))
    row = card()["tiers"][0]
    if damage:
        with pytest.raises(ValueError, match="generator device does not match tier"):
            tiers._Generator(row)
    else:
        assert tiers._Generator(row).params["temperature"] == 0


@pytest.mark.parametrize("errors", [0, 1])
def test_answer_errors_cannot_be_measured(monkeypatch, tmp_path, errors):
    result = card()
    row = result["tiers"][0]
    suite = tiers.evaluation.load_suite()
    totals = dict(retrieval_hit=0, retrieval_applicable=sum(bool(q.required_facts) for q in suite.questions),
                  questions=len(suite.questions), answer_correct=0, errors=errors)
    monkeypatch.setattr(tiers, "_worker_models", lambda row: (
        SimpleNamespace(model=object()), SimpleNamespace(model=object()), ["cpu"]))
    monkeypatch.setattr(tiers.evaluation, "PostgresHybridRetriever", lambda *args, **kwargs: nullcontext(SimpleNamespace(record={})))
    monkeypatch.setattr(tiers.evaluation, "run_eval", lambda *args, **kwargs: dict(aggregates=totals, questions=[]))
    monkeypatch.setattr(tiers.evaluation, "write_scorecard", lambda *args: None)
    monkeypatch.setattr(tiers, "_Generator", lambda row: SimpleNamespace(available=True))
    tiers.evaluate_tier(row, result["hardware"], tmp_path, True)
    assert row["retrieval"] == dict(correct=0, total=totals["retrieval_applicable"], rate=0.0)
    assert row["status"] == ("partial" if errors else "measured")
    assert (row["answer_accuracy"] is None) == bool(errors)
    assert row["execution"]["generator"] == (None if errors else "vllm")
    if errors:
        assert "ValueError" in row["reason"]


@pytest.mark.parametrize("name", ["tier-scorecard.json", "tier-scorecard.md"])
@pytest.mark.parametrize("entrypoint", ["writer", "cli"])
@pytest.mark.parametrize("symlink", [False, True])
def test_existing_card_guard(tmp_path, monkeypatch, name, entrypoint, symlink):
    # fixture-write: synthetic retained card in pytest-owned scratch.
    (tmp_path / name).write_text("synthetic retained bytes")
    if symlink:
        (tmp_path / name).unlink()
        # fixture-write: dangling synthetic output link in pytest-owned scratch.
        (tmp_path / name).symlink_to(tmp_path / "missing")
    calls = []
    monkeypatch.setattr(tiers.subprocess, "check_output", lambda *args, **kwargs: calls.append("commit") or "a" * 40)
    monkeypatch.setattr(tiers, "hardware_inventory", lambda: calls.append("inventory") or card()["hardware"])
    monkeypatch.setattr(tiers, "evaluate_tier", lambda *args: None)
    with pytest.raises(FileExistsError):
        if entrypoint == "cli":
            tiers.main(["--output-dir", str(tmp_path)])
        else:
            tiers.write_scorecard(tmp_path, card())
    assert calls == []
    if symlink:
        assert (tmp_path / name).is_symlink() and not (tmp_path / "missing").exists()
    else:
        assert (tmp_path / name).read_text() == "synthetic retained bytes"
    assert len(list(tmp_path.iterdir())) == 1


def test_writer_validates_before_creating_artifacts(tmp_path):
    result = card()
    result["schema_version"] = 99
    with pytest.raises(ValueError):
        tiers.write_scorecard(tmp_path / "invalid", result)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", [tiers.JSON_CARD, tiers.MARKDOWN_CARD])
def test_writer_exclusive_creation(tmp_path, monkeypatch, name):
    def race(directory):
        # fixture-write: synthetic competing writer after the preflight check.
        (directory / name).write_text("synthetic concurrent card")

    monkeypatch.setattr(tiers, "_check_output", race)
    with pytest.raises(FileExistsError):
        tiers.write_scorecard(tmp_path, card())
    assert (tmp_path / name).read_text() == "synthetic concurrent card"
