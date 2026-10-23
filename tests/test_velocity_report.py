"""Offline velocity reporting with wholly synthetic API responses."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def report():
    spec = importlib.util.spec_from_file_location("velocity_report", ROOT / "scripts/velocity-report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def data():
    return json.loads((ROOT / "tests/fixtures/velocity/report.json").read_text())


def test_weekly_metrics(report, data):
    result = report.summarize(data, report.timestamp("2026-10-05"), report.timestamp("2026-10-19"))
    week, empty = result["weeks"]
    assert week["week_start"] == "2026-10-05"
    assert week["merged_prs"] == 2
    assert week["open_to_merge_hours_median"] == 48
    assert week["commits_tested_per_pr"] == {"1": 2, "2": 0}
    assert week["review_comments_per_pr"] == {"1": 4, "2": 0}
    assert week["green_to_queue_minutes_median"] == 27.5
    assert week["green_to_queue_samples"] == 2
    assert week["ci_duration_minutes_median"] == 20
    assert week["ci_duration_minutes_p90"] == 30
    assert week["merge_queue_drops"] == 1
    assert empty["open_to_merge_hours_median"] is None
    assert empty["ci_duration_minutes_p90"] is None


def test_merged_pr_runs_match_commit_shas_without_pr_links(report, data):
    assert all(not run["pull_requests"] for run in data["runs"])
    unrelated = {**data["runs"][0], "id": 99, "head_sha": "synthetic-unrelated"}
    data["runs"].append(unrelated)
    week = report.summarize(data, report.timestamp("2026-10-05"), report.timestamp("2026-10-12"))["weeks"][0]
    assert week["commits_tested_per_pr"] == {"1": 2, "2": 0}
    assert week["green_to_queue_samples"] == 2


def test_queue_drops_exclude_merge_removals_and_fetch_reason(report, data):
    reasons = {event.get("reason") for event in data["events"]["1"]}
    assert {"merged", "failed_checks"} <= reasons
    week = report.summarize(data, report.timestamp("2026-10-05"), report.timestamp("2026-10-12"))["weeks"][0]
    assert week["merge_queue_drops"] == 1
    assert "RemovedFromMergeQueueEvent { createdAt reason }" in report.TIMELINE


def test_ci_durations_are_split_by_workflow_name(report, data):
    week = report.summarize(data, report.timestamp("2026-10-05"), report.timestamp("2026-10-12"))["weeks"][0]
    assert week["ci_duration_minutes_by_workflow"] == {
        "Quality": {"ci_runs": 2, "ci_duration_minutes_median": 15, "ci_duration_minutes_p90": 20},
        "Integration": {"ci_runs": 1, "ci_duration_minutes_median": 30, "ci_duration_minutes_p90": 30},
    }


@pytest.mark.parametrize("variable", ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"])
def test_cli_errors_explain_failure_without_tokens(report, monkeypatch, capsys, variable):
    token = "synthetic-sensitive-token"
    monkeypatch.setenv(variable, token)

    def fail(since):
        raise ValueError(f"Actions page is incomplete; {variable}={token}")

    monkeypatch.setattr(report, "collect", fail)
    assert report.main(["--since", "2026-10-05", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Actions page is incomplete" in captured.err
    assert token not in captured.err


def test_error_redaction_covers_token_formats_and_authorization_headers(report):
    secrets = ["ghp_" + "s" * 36, "github_pat_" + "s" * 82, "Bearer synthetic-secret", "token synthetic-secret"]
    for secret in secrets:
        assert report.error_message(ValueError(f"synthetic read failure: {secret}")) == "synthetic read failure: [redacted]"


def test_failure_and_running_runs_are_not_green(report, data):
    data["runs"][0]["conclusion"] = "failure"
    data["runs"][2]["status"] = "in_progress"
    result = report.summarize(data, report.timestamp("2026-10-05T00:30:00Z"), report.timestamp("2026-10-12"))
    week = result["weeks"][0]
    assert week["green_to_queue_samples"] == 0
    assert week["ci_duration_minutes_median"] == 20


def test_cli_json_and_text(report, data, monkeypatch, capsys):
    monkeypatch.setattr(report, "collect", lambda since: data)
    assert report.main(["--since", "2026-10-05", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repository"] == "Exculpata/exculpata"
    assert report.main(["--since", "2026-10-05"]) == 0
    assert "open_to_merge_hours_median: 48" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        report.main(["--since", "not-a-date"])


def test_gh_boundary_read_only_and_errors(report, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout='{"ok": true}', returncode=0)

    monkeypatch.setattr(report.subprocess, "run", run)
    assert report.gh_json("repos/Exculpata/exculpata/pulls") == {"ok": True}
    args, kwargs = calls[0]
    assert args[:4] == ["gh", "api", "--method", "GET"]
    assert kwargs["timeout"] > 0
    assert report.gh_json("graphql", "query { viewer { login } }")["ok"]
    assert calls[-1][0][3] == "POST"
    monkeypatch.setattr(report.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout='{"errors": [{"message": "synthetic"}]}', returncode=0))
    with pytest.raises(ValueError, match="GraphQL"):
        report.gh_json("graphql", "query { viewer { login } }")


def test_collection_paginates_rest_and_queue_history(report, data, monkeypatch):
    calls = []
    old = {**data["pulls"][1], "updated_at": "2026-09-01T00:00:00Z"}

    def recorded(endpoint, query=None, variables=None):
        calls.append(endpoint)
        if "/pulls?" in endpoint:
            return [data["pulls"][0]] * 100 if "&page=1" in endpoint else [old]
        if endpoint.endswith("/pulls/1"):
            return data["pulls"][0]
        if "/pulls/1/commits?" in endpoint:
            return [{"sha": sha} for sha in data["commits"]["1"]]
        if endpoint == "graphql":
            second = variables["cursor"] is not None
            return {"data": {"repository": {"pullRequest": {"timelineItems": {
                "nodes": data["events"]["1"] if second else [],
                "pageInfo": {"hasNextPage": not second, "endCursor": "synthetic-cursor"},
            }}}}}
        assert "actions/runs?" in endpoint
        return {"total_count": 101, "workflow_runs": [data["runs"][0]] * 100 if "&page=1" in endpoint else [data["runs"][1]]}

    monkeypatch.setattr(report, "gh_json", recorded)
    result = report.collect(report.timestamp("2026-10-05"))
    assert len(result["runs"]) == 101
    assert all(p["number"] == 1 for p in result["pulls"])
    assert result["events"]["1"] == data["events"]["1"]
    assert result["commits"]["1"] == data["commits"]["1"]
    assert any("pulls?" in c and "page=2" in c for c in calls)


def test_incomplete_reads_fail_without_partial_report(report, monkeypatch, capsys):
    monkeypatch.setattr(report, "gh_json", lambda *a, **k: {"total_count": 1001, "workflow_runs": []})
    with pytest.raises(ValueError, match="1,000-run cap"):
        list(report.pages("actions/runs", key="workflow_runs"))

    def fail(since):
        raise ValueError("synthetic read failure")

    monkeypatch.setattr(report, "collect", fail)
    assert report.main(["--since", "2026-10-05", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unavailable" in captured.err


def test_unmerged_drops_and_exact_week_boundaries(report, data):
    data["pulls"][0]["merged_at"] = None
    data["events"]["1"].append({"__typename": "RemovedFromMergeQueueEvent", "createdAt": "2026-10-12T00:00:00Z", "reason": "manual"})
    first, second = report.summarize(data, report.timestamp("2026-10-05"), report.timestamp("2026-10-19"))["weeks"]
    assert first["commits_tested_per_pr"] == {"2": 0}
    assert first["merge_queue_drops"] == second["merge_queue_drops"] == 1


def test_future_runs_and_unstarted_commits_are_excluded(report, data):
    data["runs"][0]["status"] = "queued"
    data["runs"][1]["created_at"] = "2026-10-07T00:00:00Z"
    data["runs"][1]["run_started_at"] = "2026-10-07T00:00:00Z"
    data["runs"][2]["run_started_at"] = None
    week = report.summarize(data, report.timestamp("2026-10-05"), report.timestamp("2026-10-12"))["weeks"][0]
    assert week["commits_tested_per_pr"]["1"] == 0


def test_exact_actions_cap_does_not_request_an_unavailable_page(report, data, monkeypatch):
    calls = []

    def recorded(endpoint):
        calls.append(endpoint)
        assert len(calls) <= 10, "must stop at total_count"
        return {"total_count": 1000, "workflow_runs": [data["runs"][0]] * 100}

    monkeypatch.setattr(report, "gh_json", recorded)
    assert len([r for batch in report.pages("actions/runs", key="workflow_runs") for r in batch]) == 1000
    assert len(calls) == 10


def test_short_actions_page_cannot_silently_truncate_report(report, data, monkeypatch):
    monkeypatch.setattr(report, "gh_json", lambda *a, **k: {
        "total_count": 101, "workflow_runs": data["runs"],
    })
    with pytest.raises(ValueError, match="incomplete"):
        list(report.pages("actions/runs", key="workflow_runs"))


def test_collection_splits_capped_actions_ranges_without_overlap(report, data, monkeypatch):
    windows = []
    accepted = []
    old_pr = {**data["pulls"][0], "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z"}

    def recorded(endpoint, query=None, variables=None):
        if "/pulls?" in endpoint:
            return [old_pr]
        if endpoint.endswith("/pulls/1"):
            return old_pr
        if "/pulls/1/commits?" in endpoint:
            return [{"sha": sha} for sha in data["commits"]["1"]]
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {"timelineItems": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}}
        params = parse_qs(urlsplit(endpoint).query)
        bounds = params["created"][0].split("..")
        if len(bounds) == 1:
            return {"total_count": 1001, "workflow_runs": []}
        start, end = map(report.timestamp, bounds)
        windows.append((start, end))
        if (end - start).total_seconds() > 7 * 86400:
            return {"total_count": 1001, "workflow_runs": []}
        accepted.append((start, end))
        runs = [r for r in data["runs"] if start <= report.timestamp(r["created_at"]) <= end]
        return {"total_count": len(runs), "workflow_runs": runs}

    monkeypatch.setattr(report, "gh_json", recorded)
    result = report.collect(report.timestamp("2026-10-12"))
    assert {run["id"] for run in result["runs"]} == {11, 12, 13}
    assert len(result["runs"]) == 3
    assert len(windows) > len(accepted) > 1
    accepted.sort()
    assert accepted[0][0] == report.timestamp(old_pr["created_at"])
    assert accepted[-1][1] >= report.timestamp("2026-10-12")
    assert all(right[0] - left[1] == report.timedelta(seconds=1)
               for left, right in zip(accepted, accepted[1:]))


def test_actions_cap_within_one_second_fails_instead_of_recursing(report, monkeypatch):
    monkeypatch.setattr(report, "gh_json", lambda *a, **k: {"total_count": 1001, "workflow_runs": []})
    instant = report.timestamp("2026-10-05")
    with pytest.raises(ValueError, match="within one second"):
        report.workflow_runs(instant, instant)


@pytest.mark.parametrize("count", [3, 251])
def test_incomplete_pr_commit_history_fails_without_partial_metrics(report, data, monkeypatch, count):
    def recorded(endpoint, query=None, variables=None):
        if "/pulls?" in endpoint:
            return [data["pulls"][0]]
        if endpoint.endswith("/pulls/1"):
            return {**data["pulls"][0], "commits": count}
        if "/pulls/1/commits?" in endpoint:
            return [{"sha": sha} for sha in data["commits"]["1"]]
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {"timelineItems": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}}
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(report, "gh_json", recorded)
    with pytest.raises(ValueError, match="commit"):
        report.collect(report.timestamp("2026-10-05"))
