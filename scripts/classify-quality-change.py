#!/usr/bin/env python3
"""Allow inexpensive Quality checks only for proven, plain documentation changes."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


ROOT_DOCUMENTS = {
    "README.md", "CONTRIBUTING.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md",
    "SECURITY.md", "CODE_OF_CONDUCT.md",
}
MAX_DIFF_BYTES = 8 * 1024 * 1024


def git(*args: str) -> bytes:
    return subprocess.check_output(
        ["git", *args], stderr=subprocess.PIPE, timeout=30,
    ).strip(b"\n")


def oid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) or not value.strip("0"):
        raise ValueError("Unknown revision")
    return value


def documentation_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or any(part in {".", ".."} for part in path.split("/")):
        return False
    return path in ROOT_DOCUMENTS or (
        path.startswith("docs/") and path.endswith(".md")
        and all(not part.startswith(".") for part in parts)
    )


def documentation_diff(base: str, head: str) -> bool:
    # No rename detection: moving runtime material into docs retains the deletion.
    raw = git("diff", "--raw", "-z", "--no-renames", base, head, "--")
    if not raw or len(raw) > MAX_DIFF_BYTES:
        return False
    fields = raw.split(b"\0")
    if fields[-1] != b"" or len(fields) % 2 != 1:
        return False
    for header, name in zip(fields[0:-1:2], fields[1:-1:2]):
        old_mode, new_mode, _old_blob, _new_blob, status = header.split()
        if old_mode not in {b":100644", b":000000"} or new_mode not in {b"100644", b"000000"}:
            return False
        if status not in {b"A", b"M", b"D"} or not documentation_path(name.decode("utf-8")):
            return False
    return True


def classify(event_name: str, event: dict, expected: str, ref: str) -> bool:
    expected = oid(expected)
    if git("rev-parse", "HEAD").decode() != expected:
        return False
    if event_name == "pull_request":
        pr = event["pull_request"]
        base, head = oid(pr["base"]["sha"]), oid(pr["head"]["sha"])
        ancestor = oid(git("merge-base", base, head).decode())
        return documentation_diff(ancestor, head)
    if event_name != "push" or not ref.startswith("refs/heads/") or event.get("deleted"):
        return False
    head = oid(event["after"])
    if head != expected or event.get("forced"):
        return False
    default = event["repository"]["default_branch"]
    default_ref = "refs/remotes/origin/" + default
    git("check-ref-format", default_ref)
    before = event["before"]
    if before.strip("0"):
        before = oid(before)
        git("merge-base", "--is-ancestor", before, head)
        if not documentation_diff(before, head):
            return False
    elif not event.get("created"):
        return False
    # Include the whole feature branch, not just its last docs-only commit.
    # For a new branch this also supplies its comparison base without API lists.
    if ref != "refs/heads/" + default:
        ancestor = oid(git("merge-base", default_ref, head).decode())
        return documentation_diff(ancestor, head)
    return bool(before.strip("0"))


def browser_changed(event_name: str, event: dict, expected: str) -> bool:
    """Fail open to browser coverage unless a complete PR diff rules it out."""
    if event_name != "pull_request" or git("rev-parse", "HEAD").decode() != oid(expected):
        return True
    pr = event["pull_request"]
    base, head = oid(pr["base"]["sha"]), oid(pr["head"]["sha"])
    ancestor = oid(git("merge-base", base, head).decode())
    # Include additions, modifications, deletions and both sides of renames.
    raw = git("diff", "--name-only", "-z", "--no-renames", ancestor, head, "--")
    if not raw or len(raw) > MAX_DIFF_BYTES or not raw.endswith(b"\0"):
        return True
    return any(
        path.startswith(b"scripts/browser-accept-")
        or path == b".github/workflows/quality-gates.yml"
        for path in raw[:-1].split(b"\0")
    )


def main() -> None:
    browser = True
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        docs_only = classify(os.environ["GITHUB_EVENT_NAME"], event,
                             os.environ["GITHUB_SHA"], os.environ["GITHUB_REF"])
        browser = browser_changed(os.environ["GITHUB_EVENT_NAME"], event,
                                  os.environ["GITHUB_SHA"])
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        docs_only = False
    value = str(docs_only).lower()
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"docs_only={value}\n")
        output.write(f"browser_changed={str(browser).lower()}\n")
    print("Quality scope: plain documentation only" if docs_only else "Quality scope: full gates required")


if __name__ == "__main__":
    main()
