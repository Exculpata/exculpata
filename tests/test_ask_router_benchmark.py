"""Frozen synthetic reviewer language; run with pytest -q -s for accuracy rows."""

import hashlib
import json
from pathlib import Path

import pytest

from case_intelligence.ask_router import classify
from case_intelligence.exact_search import parse_query


BENCHMARK_PATH = Path(__file__).resolve().parents[1] / "benchmarks/ask-router-v1.json"
BENCHMARK_SHA256 = "64fae76af910dee404dbae35a4073e492cfb224e7309671c445075518d21bb43"
KINDS = ("exact", "question", "every_source")
# Initial whole-percent floors at 61c4bbd: exact 100, question 71,
# every_source 77, overall 80. Full measurements are in docs/EXACT_SEARCH.md.
# Ratchet each floor to the repaired accuracy, rounded down to a whole percent.
MINIMUM_ACCURACY_PERCENT = {"exact": 100, "question": 100, "every_source": 100, "overall": 100}


@pytest.fixture(scope="module")
def benchmark():
    raw = BENCHMARK_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == BENCHMARK_SHA256, (
        "The frozen benchmark changed; review the cases and measurements before repinning its SHA-256."
    )
    return json.loads(raw)


def test_benchmark_contains_reviewable_synthetic_cases(benchmark):
    assert benchmark["schema_version"] == 1
    assert benchmark["suite_id"] == "ask-router-v1"
    assert benchmark["synthetic"] is True
    assert benchmark["frozen"] is True
    cases = benchmark["cases"]
    assert len(cases) >= 150
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["text"] for case in cases}) == len(cases)
    assert set(case["kind"] for case in cases) == set(KINDS)
    for case in cases:
        assert isinstance(case["id"], str) and case["id"].strip(), case
        assert isinstance(case["text"], str), case["id"]
        rationale = case["rationale"]
        assert isinstance(rationale, str) and rationale == rationale.strip() and rationale, case["id"]
        assert len(rationale.splitlines()) == 1, case["id"]
        if case["kind"] == "exact":
            # Exact examples must exercise syntax the unchanged search parser supports.
            parse_query(case["text"])


@pytest.fixture(scope="module")
def results(benchmark):
    return [(case, classify(case["text"]).kind) for case in benchmark["cases"]]


@pytest.mark.parametrize("kind", (*KINDS, "overall"))
def test_router_benchmark_accuracy(results, kind):
    selected = [(case, actual) for case, actual in results if kind == "overall" or case["kind"] == kind]
    failures = [(case, actual) for case, actual in selected if actual != case["kind"]]
    total = len(selected)
    correct = total - len(failures)
    floor = MINIMUM_ACCURACY_PERCENT[kind]
    assert total > 0
    print(f"ask-router-v1 {kind}: {correct}/{total} ({100 * correct / total:.2f}%); floor {floor}%")
    details = "\n".join(
        f'{case["id"]}: expected {case["kind"]}, got {actual}: {case["text"]!r}'
        for case, actual in failures
    )
    assert correct * 100 >= total * floor, details


def test_benchmark_accuracy_floors_meet_acceptance_target():
    assert set(MINIMUM_ACCURACY_PERCENT) == {*KINDS, "overall"}
    assert MINIMUM_ACCURACY_PERCENT["overall"] >= 95
    assert MINIMUM_ACCURACY_PERCENT["exact"] == 100
    assert all(0 <= floor <= 100 for floor in MINIMUM_ACCURACY_PERCENT.values())


def test_no_accidental_every_source_review(results):
    unexpected = [case["id"] for case, actual in results if actual == "every_source" and case["kind"] != actual]
    assert not unexpected, f"Expensive every-source review was selected without explicit intent: {unexpected}"
