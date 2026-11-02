"""Synthetic grading checks execution declarations without attesting a host."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import subprocess

import pytest

from case_intelligence import claim_evaluation as evaluation
from case_intelligence import generation


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Execution regressions must never contact a model or network.")
    monkeypatch.setattr(generation.urllib.request, "urlopen", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


@pytest.fixture
def capture(monkeypatch):
    def response(*args, **kwargs):
        content = json.dumps({"answerable": False, "claims": [], "limitation": None,
                              "missing_information": "Synthetic abstention."})
        return {"choices": [{"message": {"content": content}}]}
    monkeypatch.setattr(generation, "_bounded_json_request", response)
    pinned = evaluation.model_records("portable", generator_exercised=False)["components"][0]
    runtime = {"runtime_name": "synthetic", "runtime_version": "test-only", "accelerator": "none",
        "accelerator_memory_gib": 0, "driver": "none", "model_artifact_sha256": "a" * 64,
        "offline_readiness": "not_exercised", "offline_evidence_sha256": None,
        "upstream_model_id": pinned["model_id"], "upstream_revision": pinned["revision"],
        "license": pinned["license"]}
    client = generation.OpenAICompatibleGenerator("http://127.0.0.1:11435", "synthetic-execution")
    receipt = evaluation.capture(client, profile="portable", runtime=runtime, repetitions=1,
        identity_check=lambda: {"method": "api_model_id_snapshots", "model": client.model,
                                "artifact_sha256": None})
    grades = evaluation.grade_template(receipt)
    grades["grader_id"] = "synthetic-execution-grader"
    for sample in grades["samples"]:
        sample.update(answer_correct=False, rationale="Synthetic abstention.")
    return receipt, grades


def reseal(receipt):
    return evaluation.seal({key: value for key, value in receipt.items() if key != "receipt_sha256"})


def grade(receipt, grades, operation):
    receipt = reseal(receipt)
    if operation == "grade_template":
        return evaluation.grade_template(receipt)
    grades["capture_sha256"] = receipt["receipt_sha256"]
    return evaluation.score_capture(receipt, grades)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("field,value", [
    ("git_commit", "0" * 40), ("git_commit", "a" * 7), ("git_commit", None),
    ("git_commit", "f" * 40), ("working_tree_dirty", "false"), ("working_tree_dirty", 0),
    ("os", None), ("os_release", 42), ("architecture", []), ("python", None),
    ("unexpected", "synthetic-extra-field"),
])
def test_resealed_execution_declarations_fail_closed(capture, operation, field, value):
    receipt, grades = capture
    receipt["execution"][field] = value
    with pytest.raises(ValueError, match="execution|commit"):
        grade(receipt, grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("field", ["os", "git_commit", "working_tree_dirty", "implementation_sha256"])
def test_execution_fields_cannot_be_omitted(capture, operation, field):
    receipt, grades = capture
    del receipt["execution"][field]
    with pytest.raises(ValueError, match="execution"):
        grade(receipt, grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("invalid", [None, [], "synthetic", 1])
def test_execution_must_be_an_object(capture, operation, invalid):
    receipt, grades = capture
    receipt["execution"] = invalid
    with pytest.raises(ValueError, match="execution"):
        grade(receipt, grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("value", [None, 0, "not-a-date", "2026-09-23", "2026-09-23T12:00:00",
                                   "2026-09-23T12:00:00+01:00", "2026-02-30T12:00:00+00:00"])
def test_capture_timestamp_must_be_well_formed_utc(capture, operation, value):
    receipt, grades = capture
    receipt["created_at"] = value
    with pytest.raises(ValueError, match="timestamp"):
        grade(receipt, grades, operation)


def committed_view(monkeypatch, *, mismatch=None, unavailable=False):
    """Synthetic local object database; never create commits or fetch objects."""
    original = subprocess.check_output
    def output(args, **kwargs):
        if args[:3] == ["git", "cat-file", "-t"]:
            if unavailable:
                raise subprocess.CalledProcessError(128, args)
            return "commit\n" if kwargs.get("text") else b"commit\n"
        if args[:3] == ["git", "cat-file", "blob"]:
            path = args[3].split(":", 1)[1]
            value = (evaluation.ROOT / path).read_bytes()
            if path == mismatch:
                if path.endswith(".json"):
                    data = json.loads(value)
                    data["synthetic_changed_committed_content"] = True
                    value = json.dumps(data).encode()
                else:
                    value += b"\n# Synthetic different committed implementation\n"
            return value
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "check_output", output)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("path", ["src/case_intelligence/generation.py",
    "src/case_intelligence/claim_evaluation.py", "scripts/evaluate-claims.py",
    "config/models.json", "benchmarks/r1b-claims-v2.json"])
def test_clean_capture_binds_code_catalog_and_suite_to_recorded_commit(capture, monkeypatch, operation, path):
    receipt, grades = capture
    receipt["execution"]["working_tree_dirty"] = False
    committed_view(monkeypatch, mismatch=path)
    with pytest.raises(ValueError, match="clean|committed"):
        grade(receipt, grades, operation)


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("dirty", [False, True])
def test_available_commit_and_consistent_clean_or_dirty_sources_remain_gradable(capture, monkeypatch, operation, dirty):
    receipt, grades = capture
    receipt["execution"]["working_tree_dirty"] = dirty
    committed_view(monkeypatch, mismatch="src/case_intelligence/claim_evaluation.py" if dirty else None)
    original = copy.deepcopy(receipt)
    result = grade(receipt, grades, operation)
    assert receipt == original
    assert receipt["generation"]["attempts"] == 11
    if operation == "score_capture":
        assert result["generation"]["failed_attempts"] == evaluation.fraction(0, 11)


@pytest.mark.parametrize("dirty", [False, True])
def test_recorded_commit_must_be_available_without_fetching(capture, monkeypatch, dirty):
    receipt, grades = capture
    receipt["execution"]["working_tree_dirty"] = dirty
    committed_view(monkeypatch, unavailable=True)
    with pytest.raises(ValueError, match="commit.*available"):
        grade(receipt, grades, "grade_template")


def test_all_object_reads_override_fetch_and_replace_settings(capture, monkeypatch):
    receipt, grades = capture
    receipt["execution"]["working_tree_dirty"] = False
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https:file")
    monkeypatch.setenv("RECORD_BENCH_SYNTHETIC_ENV", "preserved")
    committed_view(monkeypatch)
    original = subprocess.check_output
    calls = []
    def output(args, **kwargs):
        if args[:2] == ["git", "cat-file"]:
            calls.append((args, kwargs.get("env")))
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "check_output", output)
    grade(receipt, grades, "grade_template")
    assert len(calls) == 6  # object kind, three implementation blobs, catalog and suite
    for _, environment in calls:
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert environment["GIT_ALLOW_PROTOCOL"] == ""
        assert environment["RECORD_BENCH_SYNTHETIC_ENV"] == "preserved"
    assert os.environ["GIT_ALLOW_PROTOCOL"] == "https:file"


def test_missing_promisor_commit_fails_without_remote_transport(capture, monkeypatch, tmp_path):
    receipt, grades = capture
    # Even a regressed lookup can only attempt this nonexistent local path.
    # No network remote, model, commit, or shared-repository mutation is used.
    source = evaluation.ROOT
    repository = tmp_path / "synthetic-partial-repository"
    repository.mkdir()
    for name in (*receipt["execution"]["implementation_sha256"], "config/models.json"):
        destination = repository / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "config", "remote.synthetic.promisor", "true"], cwd=repository, check=True)
    subprocess.run(["git", "config", "remote.synthetic.url", str(tmp_path / "absent-local-origin")],
                   cwd=repository, check=True)
    trace = tmp_path / "synthetic-git-trace.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:https")
    monkeypatch.setattr(evaluation, "ROOT", repository)
    receipt["execution"].update(git_commit="f" * 40, working_tree_dirty=True)
    with pytest.raises(ValueError, match="commit.*available"):
        grade(receipt, grades, "grade_template")
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    # On supported Git, no lazy fetch child is created. On older Git that lacks
    # the flag, the empty transport allow-list still blocks any remote helper.
    assert not any(event.get("event") == "child_start" and
                   any("remote-" in str(arg) for arg in event.get("argv", [])) for event in events)
    assert not any(event.get("event") == "child_start" and
                   event.get("child_class") == "transport/file" for event in events)


def test_grader_environment_and_head_need_not_match_recorded_capture(capture, monkeypatch):
    receipt, grades = capture
    receipt["execution"]["working_tree_dirty"] = True
    receipt["execution"].update(os="SyntheticOtherOS", os_release="synthetic-release",
                                architecture="synthetic-architecture", python="3.14.0rc1")
    original = subprocess.check_output
    def output(args, **kwargs):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return "e" * 40 + "\n"
        if args[:3] == ["git", "status", "--porcelain"]:
            return b""
        assert args[:2] != ["git", "fetch"]
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "check_output", output)
    result = grade(receipt, grades, "score_capture")
    assert result["generation"]["failed_attempts"] == evaluation.fraction(0, 11)


def test_historical_utc_timestamp_does_not_require_current_clock_match(capture):
    receipt, grades = capture
    receipt["created_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert len(grade(receipt, grades, "grade_template")["samples"]) == 11


@pytest.mark.parametrize("operation", ["grade_template", "score_capture"])
@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00", "skewed_clock"])
def test_valid_utc_calendar_values_do_not_attest_capture_clock(capture, operation, timestamp):
    receipt, grades = capture
    receipt["created_at"] = ((datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()
                              if timestamp == "skewed_clock" else timestamp)
    grade(receipt, grades, operation)
