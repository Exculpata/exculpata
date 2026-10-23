#!/usr/bin/env python3
"""Read-only weekly GitHub velocity report; see docs/VELOCITY_REPORT.md."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import statistics
import subprocess
import sys
from urllib.parse import urlencode

REPOSITORY = "Exculpata/exculpata"
BASE = f"repos/{REPOSITORY}"
TIMELINE = '''query($number: Int!, $cursor: String) {
  repository(owner: "Exculpata", name: "exculpata") {
    pullRequest(number: $number) {
      timelineItems(first: 100, after: $cursor, itemTypes:
        [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) {
        nodes { __typename
          ... on AddedToMergeQueueEvent { createdAt }
          ... on RemovedFromMergeQueueEvent { createdAt }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}'''


def gh_json(endpoint, query=None, variables=None):
    """The only process/network boundary: REST GETs or a fixed GraphQL query."""
    args = ["gh", "api", "--method", "POST" if query else "GET", endpoint]
    if query:
        args += ["-f", f"query={query}"]
        for key, value in (variables or {}).items():
            if value is not None:
                args += ["-F", f"{key}={value}"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError("GitHub read failed; check gh authentication, permissions and rate limits")
    payload = json.loads(result.stdout)
    if isinstance(payload, dict) and payload.get("errors"):
        raise ValueError("GraphQL read failed; report is incomplete")
    return payload


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if len(value) != 10:
            raise ValueError("timestamps require a timezone")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pages(path, params=None, key=None):
    page = 1
    while True:
        payload = gh_json(f"{BASE}/{path}?{urlencode({**(params or {}), 'per_page': 100, 'page': page})}")
        if key and payload["total_count"] > 1000:
            raise ValueError("Actions query exceeds GitHub's 1,000-run cap; report unavailable")
        rows = payload[key] if key else payload
        if key and len(rows) < min(100, payload["total_count"] - (page - 1) * 100):
            raise ValueError("Actions page is incomplete; report unavailable")
        yield rows
        if len(rows) < 100 or (key and page * 100 >= payload["total_count"]):
            break
        page += 1


def queue_events(number):
    cursor = None
    events = []
    while True:
        payload = gh_json("graphql", TIMELINE, {"number": number, "cursor": cursor})
        connection = payload["data"]["repository"]["pullRequest"]["timelineItems"]
        events.extend(connection["nodes"])
        info = connection["pageInfo"]
        if not info["hasNextPage"]:
            return events
        if not info["endCursor"] or info["endCursor"] == cursor:
            raise ValueError("Queue timeline pagination stalled")
        cursor = info["endCursor"]


def collect(since):
    pulls, events = [], {}
    for batch in pages("pulls", {"state": "all", "sort": "updated", "direction": "desc"}):
        for pr in batch:
            if timestamp(pr["updated_at"]) < since:
                break
            detail = gh_json(f"{BASE}/pulls/{pr['number']}")
            pulls.append(detail)
            events[str(pr["number"])] = queue_events(pr["number"])
        if any(timestamp(pr["updated_at"]) < since for pr in batch):
            break
    # Include retained pre-window CI history for the selected PRs' lifetimes.
    start = min([since] + [timestamp(pr["created_at"]) for pr in pulls])
    runs = [run for batch in pages("actions/runs", {"created": f">={start.isoformat()}"}, "workflow_runs") for run in batch]
    return {"pulls": pulls, "events": events, "runs": runs}


def median(values):
    return statistics.median(values) if values else None


def green_gap(runs, queued):
    before = [run for run in runs if timestamp(run["created_at"]) <= queued]
    if not before:
        return None
    sha = max(before, key=lambda run: timestamp(run["created_at"]))["head_sha"]
    latest = {}
    for run in sorted(before, key=lambda run: timestamp(run["created_at"])):
        if run["head_sha"] == sha:
            latest[run["workflow_id"]] = run
    if any(run["status"] != "completed" or run["conclusion"] != "success"
           or timestamp(run["updated_at"]) > queued for run in latest.values()):
        return None
    green = max(timestamp(run["updated_at"]) for run in latest.values())
    return (queued - green).total_seconds() / 60


def summarize(data, since, until):
    weeks = []
    monday = (since - timedelta(days=since.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    while monday < until:
        start, end = max(since, monday), min(until, monday + timedelta(days=7))
        merged = [pr for pr in data["pulls"] if pr["merged_at"] and start <= timestamp(pr["merged_at"]) < end]
        tested, comments, gaps = {}, {}, []
        drops = 0
        for pr in data["pulls"]:
            number = str(pr["number"])
            runs = [r for r in data["runs"] if any(p["number"] == pr["number"] for p in r["pull_requests"])]
            if pr in merged:
                tested[number] = len({r["head_sha"] for r in runs
                                      if r["status"] in {"in_progress", "completed"} and r.get("run_started_at")
                                      and timestamp(r["run_started_at"]) < timestamp(pr["merged_at"])})
                comments[number] = pr["review_comments"]
            for event in data["events"][number]:
                when = timestamp(event["createdAt"])
                if not start <= when < end:
                    continue
                if event["__typename"] == "RemovedFromMergeQueueEvent":
                    drops += 1
                elif event["__typename"] == "AddedToMergeQueueEvent":
                    gap = green_gap(runs, when)
                    if gap is not None:
                        gaps.append(gap)
        durations = sorted((timestamp(r["updated_at"]) - timestamp(r["run_started_at"])).total_seconds() / 60
                           for r in data["runs"] if r["status"] == "completed" and r.get("run_started_at")
                           and start <= timestamp(r["created_at"]) < end
                           and timestamp(r["run_started_at"]) <= timestamp(r["updated_at"]) <= until)
        weeks.append({
            "week_start": monday.date().isoformat(),
            "merged_prs": len(merged),
            "open_to_merge_hours_median": median([(timestamp(p["merged_at"]) - timestamp(p["created_at"])).total_seconds() / 3600 for p in merged]),
            "commits_tested_per_pr": tested,
            "review_comments_per_pr": comments,
            "green_to_queue_minutes_median": median(gaps),
            "green_to_queue_samples": len(gaps),
            "ci_runs": len(durations),
            "ci_duration_minutes_median": median(durations),
            "ci_duration_minutes_p90": durations[math.ceil(len(durations) * .9) - 1] if durations else None,
            "merge_queue_drops": drops,
        })
        monday += timedelta(days=7)
    return {"repository": REPOSITORY, "since": since.isoformat(), "until": until.isoformat(),
            "measurement": "Retained Actions history; green and durations are proxies. See docs/VELOCITY_REPORT.md.",
            "weeks": weeks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=(datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat(),
                        help="inclusive UTC date or timezone-qualified ISO timestamp (default: seven days ago)")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        since = timestamp(args.since)
        until = datetime.now(timezone.utc)
        if since >= until:
            raise ValueError("--since must be before now")
    except ValueError as exc:
        parser.error(str(exc))
    try:
        result = summarize(collect(since), since, until)
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"Velocity report unavailable ({type(exc).__name__}); check gh access and API response completeness.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{REPOSITORY} weekly velocity (UTC)\n{result['measurement']}")
        for week in result["weeks"]:
            print(f"\nWeek {week['week_start']}")
            for key, value in week.items():
                if key != "week_start":
                    print(f"  {key}: {'n/a' if value is None else value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
