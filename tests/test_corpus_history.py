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


def report(tmp_path, case, *, failure=None, error=None, skipped=False, count=None, name=None):
    suite = ET.Element("testsuite")
    for index in range(case["expected_tests"] if count is None else count):
        test = ET.SubElement(suite, "testcase", name=name or f'{case["test"]}[{index}]')
        if failure and index < case["historical_failures"]:
            ET.SubElement(test, "failure", message=failure).text = failure
        if error and index == 0:
            ET.SubElement(test, "error", message=error)
        if skipped and index == 0:
            ET.SubElement(test, "skipped")
    path = tmp_path / "report.xml"
    ET.ElementTree(suite).write(path)
    return path


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
