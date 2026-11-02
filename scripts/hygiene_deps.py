#!/usr/bin/env python3
"""Run the installed dependency auditor over local hash-lock files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


ROOTS = (Path("requirements"), Path("services/transcription"), Path("deploy/model-stager"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for lock in sorted(path for root in ROOTS if root.exists() for path in root.rglob("*.lock")):
        command = ["pip-audit", "--requirement", str(lock), "--format", "json",
                   "--vulnerability-service", "osv"]
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=300)
            if result.returncode not in (0, 1):
                reports.append({"lock": str(lock), "tool": "pip-audit",
                                "error": result.stderr.strip()[:1000]})
                continue
            reports.append({"lock": str(lock), "result": json.loads(result.stdout)})
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            reports.append({"lock": str(lock), "tool": "pip-audit", "error": str(error)})
    args.output.write_text(json.dumps({"audits": reports}, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
