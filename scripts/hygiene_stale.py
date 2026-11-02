#!/usr/bin/env python3
"""Turn locally captured repository metadata into a stale-hygiene report."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_report(data: dict, now: datetime) -> dict:
    cutoff = now - timedelta(days=14)
    open_pr_branches = {item["head"]["ref"] for item in data.get("pulls", [])}
    stale_branches = [
        {"name": item["name"], "updated_at": item["updated_at"]}
        for item in data.get("branches", [])
        if item["name"] not in open_pr_branches and parse_time(item["updated_at"]) < cutoff
    ]
    stale_issues = [
        {"number": item["number"], "updated_at": item["updated_at"],
         "kind": "pull" if "pull_request" in item else "issue"}
        for item in data.get("issues", [])
        if parse_time(item["updated_at"]) < cutoff
    ]
    registered = {Path(item["path"]).name for item in data.get("workflows", [])}
    present = {Path(item).name for item in data.get("workflow_files", [])}
    return {
        "generated_at": now.isoformat(),
        "findings": {
            "stale-branches": sorted(stale_branches, key=lambda item: item["name"]),
            "stale-issues": sorted(stale_issues, key=lambda item: item["number"]),
            "ghost-workflows": sorted(registered - present),
        },
        "errors": data.get("errors", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text())
        report = build_report(data, datetime.now(timezone.utc))
    except (OSError, ValueError, KeyError, TypeError) as error:
        report = {"findings": {}, "errors": [{"tool": "metadata", "error": str(error)}]}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
