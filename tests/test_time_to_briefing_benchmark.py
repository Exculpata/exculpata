"""The synthetic timing receipt stays portable without enforcing performance targets."""
from __future__ import annotations

import getpass
import importlib.util
import json
import math
from pathlib import Path, PureWindowsPath
import pstats
import re
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark-time-to-briefing.py"
STAGES = {"selection", "intake", "extraction", "indexing", "automatic_discovery",
          "can_query", "build_briefing"}


def reject_non_json_constant(value):
    raise ValueError(f"Non-JSON constant: {value}")


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def assert_seconds(value):
    # Sampled stages may finish between observations and correctly record zero.
    assert type(value) in (int, float)
    assert math.isfinite(value) and value >= 0


def test_twenty_file_receipt_shape_and_privacy(tmp_path):
    output = tmp_path / "receipt.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--sizes", "20", "--output", str(output),
         "--temp-root", str(tmp_path), "--timeout", "120", "--main-ref", "HEAD"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    raw = output.read_text(encoding="utf-8")
    receipt = json.loads(raw, parse_constant=reject_non_json_constant)
    assert set(receipt) == {"schema_version", "git", "environment", "configuration", "runs"}
    assert receipt["schema_version"] == 1
    git = receipt["git"]
    assert set(git) == {"commit", "clean", "main_commit", "briefing_available_on_main"}
    assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", git["commit"])
    assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", git["main_commit"])
    assert type(git["clean"]) is bool
    assert type(git["briefing_available_on_main"]) is bool
    environment = receipt["environment"]
    assert set(environment) == {"python", "cpu_count"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", environment["python"])
    assert type(environment["cpu_count"]) is int and environment["cpu_count"] > 0
    configuration = receipt["configuration"]
    assert set(configuration) == {"seed", "poll_seconds", "ingestion_workers", "profiled", "runtime", "density"}
    assert type(configuration["seed"]) is int
    assert_seconds(configuration["poll_seconds"])
    assert type(configuration["ingestion_workers"]) is int
    assert configuration["ingestion_workers"] > 0
    assert configuration["profiled"] is False
    assert configuration["runtime"] == "sqlite_basic_offline"
    assert configuration["density"] == "sparse"

    assert len(receipt["runs"]) == 1
    run = receipt["runs"][0]
    assert set(run) == {"files", "corpus", "stages", "readiness", "automatic_discovery",
                        "peak_rss_bytes", "database_bytes", "profile", "outcome", "budget_exhausted_seconds"}
    assert run["files"] == 20
    assert run["outcome"] == "complete"
    assert run["budget_exhausted_seconds"] is None
    assert set(run["corpus"]) == {"sha256", "bytes", "type_counts"}
    assert re.fullmatch(r"[0-9a-f]{64}", run["corpus"]["sha256"])
    assert type(run["corpus"]["bytes"]) is int and run["corpus"]["bytes"] > 0
    assert run["corpus"]["type_counts"] == {"txt": 5, "eml": 5, "csv": 5, "pdf": 5}
    assert set(run["stages"]) == STAGES
    for name, stage in run["stages"].items():
        assert set(stage) == {"status", "wall_seconds", "completed_seconds"}
        if name == "build_briefing" and not git["briefing_available_on_main"]:
            assert stage == {"status": "not_available", "wall_seconds": None, "completed_seconds": None}
        else:
            assert stage["status"] == "complete"
            assert_seconds(stage["wall_seconds"])
            assert_seconds(stage["completed_seconds"])
    assert run["readiness"] == {"total_count": 20, "saved_count": 20, "extracted_count": 20,
                                "searchable_count": 20, "attention_count": 0, "can_query": True}
    assert run["automatic_discovery"] == {
        "sources_ready": 20, "sources_complete": 20, "sources_attention": 0,
        "sources_pending": 0, "unsealed_sources": 0, "budget_reached": False,
    }
    for key in ("peak_rss_bytes", "database_bytes"):
        assert type(run[key]) is int and run[key] > 0
    assert run["profile"] is None

    for value in strings(receipt):
        assert not Path(value).is_absolute()
        assert not PureWindowsPath(value).is_absolute()
    for private_value in (str(ROOT), str(tmp_path), str(Path.home()), socket.gethostname(), getpass.getuser()):
        assert private_value not in raw
    assert sorted(path.name for path in tmp_path.iterdir()) == ["receipt.json"]


def test_discovery_budget_is_reported_without_claiming_completion_or_ending_intake():
    spec = importlib.util.spec_from_file_location("time_to_briefing_benchmark", SCRIPT)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    record = SimpleNamespace(
        total_count=20, saved_count=20, extracted_count=18, searchable_count=18,
        attention_count=0, can_query=True, extracting_count=2, indexing_count=0,
    )
    progress = {"sources_ready": 18, "sources_complete": 10, "sources_attention": 0,
                "sources_pending": 8, "unsealed_sources": 0, "budget_reached": True}
    bench = SimpleNamespace(
        workspace=SimpleNamespace(matter_readiness=lambda _matter_id: record),
        automatic_discovery_progress=lambda _matter: progress,
    )
    observer = benchmark.ReadinessObserver(
        bench, SimpleNamespace(matter_id="synthetic-matter"), 20, benchmark.time.perf_counter(),
    )
    assert observer.sample() is False
    assert observer.stages["can_query"]["status"] == "pending"
    assert observer.stages["automatic_discovery"]["status"] == "budget_exhausted"
    assert observer.stages["automatic_discovery"]["completed_seconds"] is None
    assert_seconds(observer.budget_exhausted_seconds)
    first_exhaustion = observer.budget_exhausted_seconds

    record.extracted_count = record.searchable_count = 20
    record.extracting_count = 0
    progress.update(sources_ready=20, sources_pending=8, unsealed_sources=2, budget_reached=False)
    assert observer.sample() is False
    assert observer.budget_exhausted_seconds == first_exhaustion
    progress.update(sources_pending=10, unsealed_sources=0, budget_reached=True)
    assert observer.sample() is True
    assert observer.stages["can_query"]["status"] == "complete"
    assert observer.stages["automatic_discovery"]["status"] == "budget_exhausted"
    assert observer.stages["automatic_discovery"]["completed_seconds"] is None
    assert observer.budget_exhausted_seconds == first_exhaustion
    assert observer.discovery == progress
    assert observer.discovery["sources_complete"] < observer.discovery["sources_ready"]


def test_profile_captures_background_work_without_competing_profilers():
    spec = importlib.util.spec_from_file_location("time_to_briefing_profile", SCRIPT)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    completed = []

    def background_work():
        completed.append(sum(range(20)))

    with benchmark.profile_all_threads(True) as profiles:
        worker = threading.Thread(target=background_work)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert completed == [190]
    assert any(key[2] == "background_work" for key in pstats.Stats(profiles[0]).stats)


@pytest.mark.parametrize("argument", ["--output", "--temp-root"])
@pytest.mark.parametrize("symlink", [False, True])
def test_rejects_artifact_paths_inside_checkout(tmp_path, argument, symlink):
    target = ROOT
    if symlink:
        target = tmp_path / "checkout-link"
        target.symlink_to(ROOT, target_is_directory=True)
    output = tmp_path / "receipt.json"
    invalid = target / "benchmark-test-must-not-write.json" if argument == "--output" else target
    command = [sys.executable, str(SCRIPT), "--sizes", "20", "--output", str(output)]
    command += [argument, str(invalid)]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 2
    assert f"argument {argument}:" in completed.stderr
    assert not output.exists()
    assert not (ROOT / "benchmark-test-must-not-write.json").exists()
