#!/usr/bin/env python3
"""Check literal PR text for stale revision claims and presentation problems."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


SHA = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.IGNORECASE)
FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.IGNORECASE)
URL = re.compile(r"\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>`\"'\[\]]+", re.IGNORECASE)
CLAIM = re.compile(
    r"(?P<historical>\b(?:based[ \t]+on[ \t]+(?:current[ \t]+)?main\b|"
    r"regression[ \t]+baseline\b|starting[ \t]+commit\b|earlier[ \t]+(?:evidence|head)\b))|"
    r"(?P<current>\b(?:current[ \t]+(?:(?:PR|branch)[ \t]+)?head\b|latest[ \t]+head\b|"
    r"(?:on|against|at|for)[ \t]+this[ \t]+head\b|head[ \t]*:|current[ \t]*:))",
    re.IGNORECASE,
)
CLAUSE_BREAK = re.compile(r"[;]|[.!?](?:[ \t]+|$)")
STACKED = re.compile(r"\bstacked\s+on\s+#\d+\b", re.IGNORECASE)
REBASE = re.compile(r"\bafter\s+rebase\b", re.IGNORECASE)


def _mask_url(match: re.Match[str]) -> str:
    token = match.group()
    depth = 0
    end = len(token)
    for index, char in enumerate(token):
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                end = index
                break
            depth -= 1
    url = token[:end].rstrip(".,;!?")
    return " " * len(url) + match.group()[len(url):]


def check_body(body: str, head: str, base: str, allow: tuple[str, ...] = ()) -> list[str]:
    """Check explicit current-head claims, retaining labelled historical contexts."""
    if not FULL_SHA.fullmatch(head):
        return ["unsupported head SHA: use check_body with a full 40- or 64-hex SHA"]
    if not base or re.search(r"\s", base):
        return ["unsupported base: use check_body with a nonempty branch name"]
    if any(not FULL_SHA.fullmatch(sha) for sha in allow):
        return ["unsupported allowed SHA: use check_body with full 40- or 64-hex SHAs"]

    problems = []
    accepted = tuple(sha.lower() for sha in (head, *allow))
    for number, line in enumerate(body.splitlines(), start=1):
        for clause in CLAUSE_BREAK.split(URL.sub(_mask_url, line)):
            claims = list(CLAIM.finditer(clause))
            if not claims:
                continue
            for match in SHA.finditer(clause):
                preceding = [claim for claim in claims if claim.start() < match.start()]
                context = preceding[-1] if preceding else claims[0]
                if context.lastgroup == "historical":
                    continue
                if not any(sha.startswith(match.group().lower()) for sha in accepted):
                    problems.append(f"line {number}: stale commit SHA {match.group()}; "
                                    "current-head claim does not match --head or --allow")
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
    parser.add_argument("--allow", action="append", default=[], metavar="SHA",
                        help="full base or merge-base SHA to allow (repeatable)")
    args = parser.parse_args()
    try:
        body = sys.stdin.buffer.read().decode("utf-8") if args.body == "-" else Path(args.body).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("cannot read PR body: provide a readable UTF-8 file or stdin")
        return 1
    problems = check_body(body, args.head, args.base, tuple(args.allow))
    for problem in problems:
        print(problem)
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
