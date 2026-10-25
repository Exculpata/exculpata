"""Historical regression evidence must be behavioral, complete and reproducible."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/corpus-history-check.py"
spec = importlib.util.spec_from_file_location("corpus_history_check", SCRIPT)
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)

EXPECTED_NODE_IDS = {
    "transcript-edit-restart": (
        "test_history_probe::test_crash_after_edit_before_projection_is_repaired_from_durable_revision",
    ),
    "missing-text-chunk": (
        "test_history_probe::test_missing_middle_production_text_chunk_invalidates_exact_total[cancelled]",
        "test_history_probe::test_missing_middle_production_text_chunk_invalidates_exact_total[neutral NOT cancelled]",
    ),
    "ask-routing": (
        "test_history_probe::test_router_benchmark_accuracy[exact]",
        "test_history_probe::test_router_benchmark_accuracy[question]",
        "test_history_probe::test_router_benchmark_accuracy[every_source]",
        "test_history_probe::test_router_benchmark_accuracy[overall]",
    ),
}


def report(tmp_path, case, *, failure=None, error=None, skipped=False, count=None, name=None):
    suite = ET.Element("testsuite")
    expected = EXPECTED_NODE_IDS[case["id"]]
    for index in range(case["expected_tests"] if count is None else count):
        classname, test_name = expected[index % len(expected)].split("::", 1)
        test = ET.SubElement(suite, "testcase", classname=classname, name=name or test_name)
        if failure and index >= case["expected_tests"] - case["historical_failures"]:
            message = failure
            if failure == "AssertionError: " + case["failure_marker"]:
                message = "AssertionError: " + case.get("failure_marker_by_test", {}).get(test_name, case["failure_marker"])
            ET.SubElement(test, "failure", message=message).text = message
        if error and index == 0:
            ET.SubElement(test, "error", message=error)
        if skipped and index == 0:
            ET.SubElement(test, "skipped")
    path = tmp_path / "report.xml"
    ET.ElementTree(suite).write(path)
    return path


@pytest.mark.parametrize("case", history.CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("historical", [False, True], ids=["current", "historical"])
@pytest.mark.parametrize("mutation", ["replacement-parameter", "replacement-module"])
def test_exact_node_population_rejects_same_count_replacements(tmp_path, case, historical, mutation):
    path = report(tmp_path, case, failure="AssertionError: " + case["failure_marker"] if historical else None)
    tree = ET.parse(path)
    test = list(tree.getroot().iter("testcase"))[-1]
    if mutation == "replacement-parameter":
        test.set("name", case["test"] + "[synthetic-replacement]")
    else:
        test.set("classname", "tests.test_unrelated_probe")
    tree.write(path)
    outcome = history.evaluate_report(path, case, int(historical), historical)
    assert outcome["tests"] == case["expected_tests"]
    assert outcome["failures"] == (case["historical_failures"] if historical else 0)
    assert not outcome["accepted"]


@pytest.mark.parametrize("case", [case for case in history.CASES if case["expected_tests"] > 1],
                         ids=lambda case: case["id"])
@pytest.mark.parametrize("historical", [False, True], ids=["current", "historical"])
def test_exact_node_population_rejects_duplicate_replacing_required_parameter(tmp_path, case, historical):
    path = report(tmp_path, case, failure="AssertionError: " + case["failure_marker"] if historical else None)
    tree = ET.parse(path)
    tests = list(tree.getroot().iter("testcase"))
    tests[-1].set("name", tests[0].get("name"))
    tree.write(path)
    outcome = history.evaluate_report(path, case, int(historical), historical)
    assert outcome["tests"] == case["expected_tests"]
    assert outcome["failures"] == (case["historical_failures"] if historical else 0)
    assert not outcome["accepted"]


@pytest.mark.parametrize("case", history.CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("historical", [False, True], ids=["current", "historical"])
def test_exact_node_population_accepts_reordered_complete_report(tmp_path, case, historical):
    path = report(tmp_path, case, failure="AssertionError: " + case["failure_marker"] if historical else None)
    tree = ET.parse(path)
    suite = tree.getroot()
    suite[:] = list(reversed(list(suite)))
    tree.write(path)
    outcome = history.evaluate_report(path, case, int(historical), historical)
    assert outcome["accepted"]


@pytest.mark.parametrize("case", history.CASES, ids=lambda case: case["id"])
def test_pinned_node_population_is_exact_and_unique(case):
    assert case["expected_node_ids"] == EXPECTED_NODE_IDS[case["id"]]
    assert len(set(case["expected_node_ids"])) == case["expected_tests"]


@pytest.mark.parametrize("case", history.CASES, ids=lambda case: case["id"])
def test_current_evidence_requires_complete_passing_population(tmp_path, case):
    path = report(tmp_path, case)
    assert history.evaluate_report(path, case, 0, False)["accepted"]
    assert not history.evaluate_report(path, case, 1, False)["accepted"]
    path = report(tmp_path, case, count=0)
    assert not history.evaluate_report(path, case, 0, False)["accepted"]


@pytest.mark.parametrize("case", history.CASES, ids=lambda case: case["id"])
def test_historical_evidence_requires_specific_behavioral_failure(tmp_path, case):
    failure = "AssertionError: " + case["failure_marker"]
    path = report(tmp_path, case, failure=failure)
    assert history.evaluate_report(path, case, 1, True)["accepted"]
    # Neither a green baseline nor an interrupted process demonstrates a defect.
    assert not history.evaluate_report(path, case, 0, True)["accepted"]
    assert not history.evaluate_report(path, case, 2, True)["accepted"]
    path = report(tmp_path, case)
    assert not history.evaluate_report(path, case, 0, True)["accepted"]


@pytest.mark.parametrize("case, intended_position", [
    pytest.param(case, position, id=f"{case['id']}-intended-{position}")
    for case in history.CASES if case["historical_failures"] > 1
    for position in range(case["historical_failures"])
])
def test_every_historical_failure_needs_its_own_behavioral_marker(tmp_path, case, intended_position):
    path = report(tmp_path, case, failure="AssertionError: unrelated mismatch")
    tree = ET.parse(path)
    intended = list(tree.getroot().iter("failure"))[intended_position]
    message = "AssertionError: " + case["failure_marker"]
    intended.set("message", message)
    intended.text = message
    tree.write(path)
    outcome = history.evaluate_report(path, case, 1, True)
    assert outcome["failures"] == case["historical_failures"]
    assert not outcome["accepted"]


@pytest.mark.parametrize("wrong_parameter", [None, "question", "every_source", "overall"])
def test_routing_failure_markers_are_bound_to_their_population(tmp_path, wrong_parameter):
    case = history.CASES[2]
    path = report(tmp_path, case)
    tree = ET.parse(path)
    for test, parameter in zip(tree.getroot().iter("testcase"), ("exact", "question", "every_source", "overall")):
        name = f'{case["test"]}[{parameter}]'
        test.set("name", name)
        if parameter == "exact":
            continue
        marker = case.get("failure_marker_by_test", {}).get(name, case["failure_marker"])
        if parameter == wrong_parameter:
            marker = (case["failure_marker"] if parameter == "every_source"
                      else "expected every_source, got question")
        message = "AssertionError: " + marker
        ET.SubElement(test, "failure", message=message).text = message
    tree.write(path)
    outcome = history.evaluate_report(path, case, 1, True)
    assert outcome["failures"] == 3
    assert outcome["accepted"] is (wrong_parameter is None)


@pytest.mark.parametrize("mutation", [
    {"error": "ImportError: synthetic dependency missing"},
    {"skipped": True},
    {"failure": "AttributeError: orange unicycle"},
    {"failure": "AssertionError: unrelated mismatch"},
    {"failure": "AssertionError: orange unicycle", "name": "test_unrelated"},
    {"failure": "AssertionError: orange unicycle", "count": 2},
])
def test_setup_skips_unrelated_or_incomplete_failures_cannot_count_as_proof(tmp_path, mutation):
    case = history.CASES[0]
    path = report(tmp_path, case, **mutation)
    assert not history.evaluate_report(path, case, 1, True)["accepted"]


def test_missing_report_fails_closed(tmp_path):
    assert not history.evaluate_report(tmp_path / "absent.xml", history.CASES[0], 1, True)["accepted"]


@pytest.mark.parametrize("case", history.CASES, ids=lambda case: case["id"])
def test_probes_reuse_current_synthetic_tests_without_unrelated_collection(tmp_path, case):
    probe = history.write_probe(history.ROOT, tmp_path, case)
    contents = probe.read_text()
    assert "def " + case["test"] + "(" in contents
    compile(contents, str(probe), "exec")
    assert len(case["fix"]) == 40
    if case["id"] == "ask-routing":
        assert (tmp_path / "benchmarks/ask-router-v1.json").read_bytes() == (
            history.ROOT / "benchmarks/ask-router-v1.json").read_bytes()


def test_missing_fixture_definition_is_an_error(tmp_path):
    source = tmp_path / "fixture.py"
    source.write_text("def present():\n    pass\n")
    with pytest.raises(ValueError, match="Missing regression definitions"):
        history.definitions(source, {"absent"})


@pytest.mark.parametrize("timeout", ["0", "301"])
def test_timeout_cannot_disable_execution_bound(timeout):
    with pytest.raises(SystemExit) as exc:
        history.main(["--timeout", timeout])
    assert exc.value.code == 2


def test_transcript_probe_carries_the_exact_current_fixture_boundary(tmp_path):
    history.write_probe(history.ROOT, tmp_path, history.CASES[0])
    assert (tmp_path / 'tests/fixture_write.py').read_bytes() == (
        history.ROOT / 'tests/fixture_write.py').read_bytes()


def test_transcript_probe_executes_current_fixture_and_assertion():
    result = history.run_probe(history.ROOT, history.CASES[0], historical=False, timeout=90)
    assert result['accepted'], result.get('diagnostic', result)
    assert result['tests'] == 1
    assert result['failures'] == result['errors'] == result['skipped'] == 0


def test_probe_identity_is_independent_of_parent_pytest_root(tmp_path, monkeypatch):
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    original_temporary = history.tempfile.TemporaryDirectory

    def temporary(*, prefix):
        return original_temporary(prefix=prefix, dir=tmp_path)

    monkeypatch.setattr(history.tempfile, 'TemporaryDirectory', temporary)
    result = history.run_probe(history.ROOT, history.CASES[2], historical=False, timeout=90)
    assert result['accepted'], result.get('diagnostic', result)
    assert result['node_ids'] == list(EXPECTED_NODE_IDS['ask-routing'])
