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
    advisories = 0
    for lock in sorted(path for root in ROOTS if root.exists() for path in root.rglob("*.lock")):
        command = ["pip-audit", "--requirement", str(lock), "--format", "json",
                   "--vulnerability-service", "osv"]
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=300)
            if result.returncode not in (0, 1):
                reports.append({"lock": str(lock), "tool": "pip-audit",
                                "error": result.stderr.strip()[:1000]})
                continue
            audit = json.loads(result.stdout)
            dependencies = audit["dependencies"]
            if not isinstance(dependencies, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("vulns", []), list)
                for item in dependencies
            ):
                raise ValueError("unsupported audit format")
            advisories += sum(len(item.get("vulns", [])) for item in dependencies)
            reports.append({"lock": str(lock), "result": audit})
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            reports.append({"lock": str(lock), "tool": "pip-audit", "error": str(error)})
    report = {"audits": reports, "findings": {"vulnerable-deps": {"advisories": advisories}},
              "errors": [item for item in reports if "error" in item]}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
