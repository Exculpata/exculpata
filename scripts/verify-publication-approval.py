#!/usr/bin/env python3
"""Read-only verification from a trusted base checkout; never execute PR code."""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import stat
import urllib.request

REPOSITORY = "Exculpata/exculpata"
POLICY = "scripts/publication-media-reviews.json"
APPROVERS = "scripts/publication-approvers.json"
PROTECTED = {
    POLICY, APPROVERS, "scripts/publication-check.py",
    "scripts/verify-publication-approval.py", "scripts/install-publication-hook.py",
    "scripts/pre-push-publication.py", "scripts/check-publication-candidate.py",
    "scripts/publication_home_owners.py", "scripts/legacy_name_check.py",
    ".github/CODEOWNERS", "AGENTS.md", "Makefile",
}
MAX_BYTES = 512 * 1024
MAX_TREE_BYTES = 8 * 1024 * 1024


class ApprovalError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ApprovalError("duplicate-json-field")
        result[key] = value
    return result


def decode(data, limit=MAX_BYTES):
    if len(data) > limit:
        raise ApprovalError("approval-data-too-large")
    return json.loads(data, object_pairs_hook=unique_object)


def maintainers_from(data):
    policy = decode(data)
    if (not isinstance(policy, dict) or set(policy) != {"version", "maintainers"}
            or type(policy["version"]) is not int or policy["version"] != 1
            or not isinstance(policy["maintainers"], list) or not policy["maintainers"]):
        raise ApprovalError("invalid-maintainer-policy")
    result = {}
    for entry in policy["maintainers"]:
        if (not isinstance(entry, dict) or set(entry) != {"login", "id"}
                or not isinstance(entry["login"], str)
                or re.fullmatch(r"[A-Za-z0-9-]{1,39}", entry["login"]) is None
                or type(entry["id"]) is not int or entry["id"] <= 0
                or entry["login"] in result or entry["id"] in result.values()):
            raise ApprovalError("invalid-maintainer-policy")
        result[entry["login"]] = entry["id"]
    return result


def policy_approvers(data, maintainers, *, allow_pending=False):
    policy = decode(data)
    if (not isinstance(policy, dict) or set(policy) != {"version", "reviews"}
            or type(policy["version"]) is not int or policy["version"] != 2
            or not isinstance(policy["reviews"], list)):
        raise ApprovalError("invalid-media-policy")
    approvers = set()
    seen = set()
    for entry in policy["reviews"]:
        if (not isinstance(entry, dict) or set(entry) != {
                "sha256", "suffix", "reason", "approver", "expires", "status", "approved_on"}):
            raise ApprovalError("invalid-media-policy")
        if any(not isinstance(entry[key], str) or not entry[key].strip()
               for key in ("sha256", "suffix", "reason", "expires", "status")):
            raise ApprovalError("invalid-media-policy")
        if entry["status"] == "pending":
            if entry["approver"] is not None or entry["approved_on"] is not None:
                raise ApprovalError("invalid-media-policy")
        elif entry["status"] == "approved":
            if not isinstance(entry["approver"], str) or entry["approver"] not in maintainers:
                raise ApprovalError("unauthorized-publication-approver")
            if not isinstance(entry["approved_on"], str):
                raise ApprovalError("invalid-media-review-date")
        else:
            raise ApprovalError("invalid-media-policy")
        if (re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                or entry["suffix"] not in {".gif", ".png", ".jpg", ".jpeg", ".webp", ".avi", ".flac",
                    ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm", ".wma", ".wmv"}):
            raise ApprovalError("invalid-media-policy")
        key = entry["sha256"], entry["suffix"]
        if key in seen:
            raise ApprovalError("duplicate-media-review")
        seen.add(key)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["expires"]) is None:
            raise ApprovalError("invalid-media-review-date")
        expiry = date.fromisoformat(entry["expires"])
        if entry["status"] == "pending":
            if not allow_pending:
                raise ApprovalError("publication-exemption-pending")
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry["approved_on"]) is None:
            raise ApprovalError("invalid-media-review-date")
        approved = date.fromisoformat(entry["approved_on"])
        if approved > date.today() or not 0 < (expiry - approved).days <= 180:
            raise ApprovalError("invalid-media-review-lifetime")
        if expiry >= date.today():
            approvers.add(entry["approver"])
    return approvers


def authorized_reviews(reviews, maintainers, head, digest):
    latest = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ApprovalError("invalid-review-response")
        user = review.get("user") or {}
        login = user.get("login")
        if login not in maintainers:
            continue
        if (user.get("type") != "User" or type(user.get("id")) is not int
                or user.get("id") != maintainers[login]
                or type(review.get("id")) is not int):
            raise ApprovalError("reviewer-identity-mismatch")
        if login not in latest or review["id"] > latest[login]["id"]:
            latest[login] = review
    authorized = set()
    statement = "Publication exemptions approved for policy SHA-256: " + digest
    for login, review in latest.items():
        # COMMENTED plus this explicit attestation supports a maintainer's own PR;
        # GitHub does not permit approving one's own PR with state APPROVED.
        if (review.get("state") not in {"APPROVED", "COMMENTED"}
                or review.get("commit_id") != head or not review.get("submitted_at")
                or statement not in (review.get("body") or "").splitlines()):
            continue
        datetime.fromisoformat(review["submitted_at"].replace("Z", "+00:00"))
        authorized.add(login)
    return authorized


def protected(path):
    return path in PROTECTED or path.endswith("/AGENTS.md") or path.startswith(".github/workflows/")


def verify_policy(base_policy, candidate_policy, maintainers, head, reviews, changed_files):
    changed = base_policy != candidate_policy or any(
        protected(item.get("filename", "")) or protected(item.get("previous_filename", ""))
        for item in changed_files
    )
    # A byte-identical trusted pending request grants nothing and need not
    # prevent unrelated work. New policy or protected changes remain strict.
    approvers = policy_approvers(candidate_policy, maintainers, allow_pending=not changed)
    if not changed:
        return
    authorized = authorized_reviews(reviews, maintainers, head, hashlib.sha256(candidate_policy).hexdigest())
    if not authorized or not approvers <= authorized:
        raise ApprovalError("maintainer-review-required-for-exact-head-and-policy")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ApprovalError("unexpected-api-redirect")


class GitHub:
    """Fixed public API origin, no token, no candidate-controlled URLs or artifacts."""
    def get(self, path, *, limit=MAX_BYTES):
        request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + path,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Exculpata-publication-approval",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            data = response.read(limit + 1)
        return decode(data, limit)

    def pages(self, path):
        result = []
        for page in range(1, 101):
            batch = self.get(path + f"?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ApprovalError("invalid-paginated-response")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise ApprovalError("api-pagination-limit")

    def tree(self, commit_sha):
        commit = self.get("/git/commits/" + commit_sha)
        if not isinstance(commit, dict):
            raise ApprovalError("invalid-immutable-commit")
        tree_sha = commit.get("tree", {}).get("sha")
        if (commit.get("sha") != commit_sha or not isinstance(tree_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", tree_sha) is None):
            raise ApprovalError("invalid-immutable-commit")
        value = self.get("/git/trees/" + tree_sha + "?recursive=1", limit=MAX_TREE_BYTES)
        if (not isinstance(value, dict) or value.get("sha") != tree_sha
                or value.get("truncated") is not False or not isinstance(value.get("tree"), list)
                or len(value["tree"]) > 100_000):
            raise ApprovalError("incomplete-immutable-tree")
        result = {}
        modes = {"blob": {"100644", "100755", "120000"}, "tree": {"040000"}, "commit": {"160000"}}
        for entry in value["tree"]:
            if not isinstance(entry, dict):
                raise ApprovalError("invalid-immutable-tree-entry")
            path, kind, mode, sha = (entry.get(key) for key in ("path", "type", "mode", "sha"))
            if (not isinstance(path, str) or not path or path in result
                    or any(part in {"", ".", ".."} for part in path.split("/"))
                    or kind not in modes or mode not in modes[kind]
                    or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None):
                raise ApprovalError("invalid-immutable-tree-entry")
            result[path] = (kind, mode, sha)
        return result

    def changed_files(self, base_sha, head_sha):
        # Compare immutable complete trees; the PR files endpoint can race with
        # a moving head, and the compare endpoint truncates its files list.
        base, head = self.tree(base_sha), self.tree(head_sha)
        return [{"filename": path} for path in sorted(base.keys() | head.keys())
                if base.get(path) != head.get(path)]

    def policy(self, head):
        value = self.get("/contents/" + POLICY + "?ref=" + head)
        if (not isinstance(value, dict) or value.get("type") != "file"
                or value.get("encoding") != "base64" or type(value.get("size")) is not int
                or not 0 <= value["size"] <= MAX_BYTES):
            raise ApprovalError("invalid-candidate-policy-file")
        data = base64.b64decode(value["content"].replace("\n", ""), validate=True)
        if len(data) != value["size"]:
            raise ApprovalError("invalid-candidate-policy-size")
        return data


def trusted_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise ApprovalError("unsafe-trusted-policy-file")
    return path.read_bytes()


def verify_current(client, number, expected_head, root, trusted_base):
    maintainers = maintainers_from(trusted_file(root / APPROVERS))
    base_policy = trusted_file(root / POLICY)
    path = f"/pulls/{number}"
    pr = client.get(path)
    if (pr.get("state") != "open" or pr.get("base", {}).get("ref") != "main"
            or pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("head", {}).get("sha") != expected_head
            or pr.get("base", {}).get("sha") != trusted_base):
        raise ApprovalError("pull-request-head-or-base-mismatch")
    policy = client.policy(expected_head)
    files = client.changed_files(trusted_base, expected_head)
    reviews = client.pages(path + "/reviews")
    verify_policy(base_policy, policy, maintainers, expected_head, reviews, files)
    current = client.get(path)
    if (current.get("head", {}).get("sha") != expected_head or current.get("state") != "open"
            or current.get("base", {}).get("ref") != "main"
            or current.get("base", {}).get("sha") != trusted_base
            or current.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY):
        raise ApprovalError("pull-request-changed-during-verification")
    if client.pages(path + "/reviews") != reviews:
        raise ApprovalError("reviews-changed-during-verification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--trusted-base", required=True)
    args = parser.parse_args()
    if (args.pull_request <= 0 or any(re.fullmatch(r"[0-9a-f]{40}", value) is None
                                      for value in (args.expected_head, args.trusted_base))):
        raise ApprovalError("invalid-verification-target")
    verify_current(GitHub(), args.pull_request, args.expected_head,
                   Path(__file__).resolve().parents[1], args.trusted_base)
    print("Publication approval verified against trusted maintainer identities and live GitHub reviews.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never publish candidate text, response bodies, metadata or credentials.
        raise SystemExit("Publication approval blocked: trusted verification did not complete or approval is missing.")
