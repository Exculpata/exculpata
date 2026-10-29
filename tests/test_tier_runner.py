"""Synthetic runner contracts, never model-quality or hardware measurements."""
import json
import subprocess
import sys

import pytest

from case_intelligence import tier_evals as tiers


def card():
    return tiers.new_scorecard(
        "a" * 40, "synthetic command", {"cpu": "synthetic CPU", "gpus": []}
    )


@pytest.mark.parametrize("tier_index", [1, 2], ids=["consumer", "ultra"])
@pytest.mark.parametrize("with_generator", [False, True], ids=["retrieval-only", "with-generator"])
def test_evaluate_tier_rejects_hardware_mismatch(tmp_path, monkeypatch, tier_index, with_generator):
    result = card()
    row = result["tiers"][tier_index]
    calls = []

    def unexpected_worker_start(row):
        calls.append(row["tier"])
        raise AssertionError("mismatched hardware must not start models")

    monkeypatch.setattr(tiers, "_worker_models", unexpected_worker_start)
    tiers.evaluate_tier(row, result["hardware"], tmp_path, with_generator)
    assert calls == []
    assert row["reason"] == "no matching GPU hardware available in this run"
    assert row["status"] == "unmeasured"
    assert row["retrieval"] is row["answer_accuracy"] is row["execution"] is None
    assert row["wall_seconds"] == 0.0
    assert list(tmp_path.iterdir()) == []
    tiers.validate_scorecard(result)


def test_output_roundtrip_and_unknown_schema(tmp_path):
    result = card()
    directory = tmp_path / "scorecards"
    tiers.write_scorecard(directory, result)
    restored = json.loads((directory / "tier-scorecard.json").read_text())
    assert restored == result
    tiers.validate_scorecard(restored)
    assert "unmeasured" in (directory / "tier-scorecard.md").read_text()
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


def test_refuses_existing_scorecard_directory(tmp_path):
    directory = tmp_path / "existing-scorecards"
    directory.mkdir()
    # fixture-write: synthetic existing scorecard bytes in pytest-owned scratch.
    (directory / "tier-scorecard.json").write_text('{"synthetic": "retained"}\n')
    # fixture-write: synthetic existing Markdown card in pytest-owned scratch.
    (directory / "tier-scorecard.md").write_text("# Synthetic retained scorecard\n")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(FileExistsError):
        tiers.write_scorecard(directory, card())
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_network_contract_passes():
    completed = subprocess.run(
        [sys.executable, "scripts/network-contract.py"],
        cwd=tiers.evaluation.ROOT, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
