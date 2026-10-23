#!/usr/bin/env python3
"""Check literal PR text for stale revision claims and presentation problems."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


SHA = re.compile(r"\b[0-9a-fA-F]{7,}\b")
EARLIER = re.compile(r"\bearlier evidence:[ \t]*`?([0-9a-fA-F]{7,})\b", re.IGNORECASE)
STACKED = re.compile(r"\bstacked\s+on\s+#\d+\b", re.IGNORECASE)
REBASE = re.compile(r"\bafter\s+rebase\b", re.IGNORECASE)


def check_body(body: str, head: str, base: str) -> list[str]:
    """Scan plain UTF-8 text, with one explicit label per historical SHA."""
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", head):
        return ["unsupported head SHA: use check_body with a full 40- or 64-hex SHA"]
    if not base or re.search(r"\s", base):
        return ["unsupported base: use check_body with a nonempty branch name"]

    problems = []
    for number, line in enumerate(body.splitlines(), start=1):
        historical = {match.span(1) for match in EARLIER.finditer(line)}
        for match in SHA.finditer(line):
            if not head.lower().startswith(match.group().lower()) and match.span() not in historical:
                problems.append(f"line {number}: stale commit SHA {match.group()}; "
                                "use the current head or label Earlier evidence: <sha>")
    if base in {"main", "refs/heads/main"}:
        for pattern, description in ((STACKED, "Stacked on #"), (REBASE, "after rebase")):
            for match in pattern.finditer(body):
                number = body.count("\n", 0, match.start()) + 1
                claim = match.group() if description == "Stacked on #" else description
                problems.append(f"line {number}: {claim} is stale when the base is main")
    for match in re.finditer("\u2014", body):
        number = body.count("\n", 0, match.start()) + 1
        problems.append(f"line {number}: em dash; use plain punctuation")
    return sorted(problems, key=lambda problem: int(problem.split(":", 1)[0].split()[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("body", nargs="?", default="-", help="UTF-8 body file, or - for stdin (default)")
    parser.add_argument("--head", required=True, help="full PR head SHA")
    parser.add_argument("--base", required=True, help="PR base branch")
    args = parser.parse_args()
    try:
        body = sys.stdin.buffer.read().decode("utf-8") if args.body == "-" else Path(args.body).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("cannot read PR body: provide a readable UTF-8 file or stdin")
        return 1
    problems = check_body(body, args.head, args.base)
    for problem in problems:
        print(problem)
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
