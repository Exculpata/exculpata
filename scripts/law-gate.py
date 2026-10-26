#!/usr/bin/env python3
"""Evaluate trusted Git diffs and immutable, head-specific issue-comment proof."""
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

LOCK = re.compile(r"(?:requirements/[^/]*\.lock|deploy/[^/]+/requirements[^/]*\.lock|services/transcription/requirements[^/]*\.lock|(?:.*/)?(?:uv\.lock|poetry\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock))")
DOC = re.compile(r"(?:docs/.*|[^/]*)\.md")
DEPENDENCY = re.compile(r"(?:(?:services/transcription/)?pyproject\.toml|requirements/[^/]*\.in|services/transcription/[^/]*\.in|deploy/[^/]+/requirements[^/]*\.in)")
FORBIDDEN = re.compile(r"(?:src/|tests/|scripts/|\.github/|config/|pyproject\.toml$)")
PROOF = re.compile(r"law: (spec-approved specs/[a-z0-9._-]+\.md|size-exception|spec-rewritten specs/[a-z0-9._-]+\.md) @ ([0-9a-fA-F]{40})")


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("unsupported SHA: use run with a full Git SHA")
    return value


def git(*args):
    return subprocess.check_output(["git", *args], text=True)


def api(path, payload=None):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def proofs(number, head):
    approver = os.environ.get("LAW_APPROVER")
    if not approver:
        raise ValueError("LAW_APPROVER must be set and nonempty")
    accepted, page = set(), 1
    while True:
        comments = api(f"issues/{number}/comments?per_page=100&page={page}")
        if not isinstance(comments, list):
            raise ValueError("unsupported comments: use run with issue-comment API JSON")
        for comment in comments:
            if (comment.get("user", {}).get("login") != approver or not comment.get("created_at")
                    or comment.get("updated_at") != comment["created_at"]):
                continue
            for line in (comment.get("body") or "").splitlines():
                match = PROOF.fullmatch(line)
                if match and match[2] == head:
                    accepted.add(match[1].split()[0])
        if len(comments) < 100:
            return accepted
        page += 1


def changes(base, head):
    tokens = iter(git("diff", "--no-ext-diff", "--numstat", "-z", "-M", base, head, "--").split("\0")[:-1])
    rows = []
    for token in tokens:
        added, deleted, path = token.split("\t", 2)
        paths = (path,) if path else (next(tokens), next(tokens))
        binary = added == "-" or deleted == "-"
        excluded = all(LOCK.fullmatch(p) or p == "scripts/qa-env.cjs" for p in paths)
        count = 0 if binary or excluded else int(added) + int(deleted)
        rows.append((paths, count, binary))
    return rows


def evaluate(pr, base, head):
    proof = proofs(pr["number"], sha(pr["head"]["sha"]))
    labels = {label["name"] for label in pr["labels"]}
    rows = changes(base, head)
    paths = [path for names, _, _ in rows for path in names]
    total = sum(count for _, count, _ in rows)
    failures, details = [], [f"Counted lines: {total}"]
    if total > 500 and "size-exception" not in proof:
        failures.append("R1 size")
        details += [f"{count}: {names!r}" for names, count, _ in sorted(rows, key=lambda row: row[1], reverse=True)[:10]]
    details += [f"Binary (0): {names!r}" for names, _, binary in rows if binary]
    if pr["base"]["ref"] != "main":
        failures.append("R2 base")
    mechanical = (all((DOC.fullmatch(p) or LOCK.fullmatch(p)) and not FORBIDDEN.match(p) for p in paths)
                  and not git("diff", "--no-ext-diff", "--name-only", "-z", "-M", "--diff-filter=AD", base, head, "--")
                  and sum(count for names, count, _ in rows if any(p.endswith(".md") for p in names)) <= 40)
    dependency = pr["user"]["login"] == "dependabot[bot]" and all(LOCK.fullmatch(p) or DEPENDENCY.fullmatch(p) for p in paths)
    if (("mechanical" in labels and not mechanical) or
            not ("spec-approved" in proof or ("mechanical" in labels and mechanical) or dependency)):
        failures.append("R3 spec/mechanical")
    if "review-fail-2" in labels and "spec-rewritten" not in proof:
        failures.append("R4 two strikes")
    return failures, details


def run(event):
    group = event.get("merge_group")
    target = group["head_sha"] if group else event.get("pull_request", {}).get("head", {}).get("sha")
    failures, details = [], []
    try:
        if group:
            base, target = sha(group["base_sha"]), sha(target)
            git("fetch", "--no-tags", "origin", target)
            commits = git("rev-list", "--first-parent", "--reverse", f"{base}..{target}").splitlines()
            if not commits:
                raise ValueError("unsupported empty merge group: use run with queued commits")
            candidates = [(commit, re.search(r"\(#([1-9][0-9]*)\)", git("show", "-s", "--format=%s", commit).strip())) for commit in commits]
        else:
            number = int(event.get("number") or event["issue"]["number"])
            git("fetch", "--no-tags", "origin", f"pull/{number}/head")
            target = sha(git("rev-parse", "FETCH_HEAD").strip())
            candidates = [(None, number)]
        for commit, reference in candidates:
            number = reference[1] if group and reference else reference
            label = f"#{number}" if number else str(commit)
            try:
                if not number:
                    raise ValueError("missing (#N) subject")
                pr = api(f"pulls/{number}")
                pr_head = sha(pr["head"]["sha"])
                if group:
                    base, head = git("rev-parse", f"{commit}^1").strip(), commit
                else:
                    if pr_head != target:
                        raise ValueError("PR head changed during fetch; rerun law-gate")
                    base, head = git("merge-base", "origin/main", target).strip(), target
                broken, report = evaluate(pr, base, head)
                failures += [f"{label} {rule}" for rule in broken]
                details += [f"{label} {item}" for item in report]
            except Exception as error:
                failures.append(f"{label} {type(error).__name__}: {error}")
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")
    report = "\n".join(failures + details) or "All law-gate rules passed"
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(report)
    status = {"context": "law-gate", "state": "failure" if failures else "success",
              "description": ("; ".join(failures) if failures else "All law-gate rules passed")[:140]}
    if os.environ.get("GITHUB_RUN_ID"):
        status["target_url"] = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    api(f"statuses/{sha(target)}", status)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(run(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())))
