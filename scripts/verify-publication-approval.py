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


class ApprovalError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ApprovalError("duplicate-json-field")
        result[key] = value
    return result


def decode(data):
    if len(data) > MAX_BYTES:
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


def policy_approvers(data, maintainers):
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
        if entry["status"] != "approved":
            raise ApprovalError("publication-exemption-pending")
        if not isinstance(entry["approver"], str) or entry["approver"] not in maintainers:
            raise ApprovalError("unauthorized-publication-approver")
        if any(not isinstance(value, str) or not value.strip() for value in entry.values()):
            raise ApprovalError("invalid-media-policy")
        if (re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                or entry["suffix"] not in {".gif", ".png", ".jpg", ".jpeg", ".webp", ".avi", ".flac",
                    ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm", ".wma", ".wmv"}):
            raise ApprovalError("invalid-media-policy")
        key = entry["sha256"], entry["suffix"]
        if key in seen:
            raise ApprovalError("duplicate-media-review")
        seen.add(key)
        if any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry[key]) is None
               for key in ("approved_on", "expires")):
            raise ApprovalError("invalid-media-review-date")
        approved = date.fromisoformat(entry["approved_on"])
        expiry = date.fromisoformat(entry["expires"])
        if not approved <= date.today() <= expiry or not 0 < (expiry - approved).days <= 180:
            raise ApprovalError("invalid-media-review-lifetime")
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
    return path in PROTECTED or path.startswith(".github/workflows/")


def verify_policy(base_policy, candidate_policy, maintainers, head, reviews, changed_files):
    approvers = policy_approvers(candidate_policy, maintainers)
    changed = base_policy != candidate_policy or any(
        protected(item.get("filename", "")) or protected(item.get("previous_filename", ""))
        for item in changed_files
    )
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
    def get(self, path):
        request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY + path,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Exculpata-publication-approval",
                     "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            data = response.read(MAX_BYTES + 1)
        return decode(data)

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


def verify_current(client, number, expected_head, root):
    maintainers = maintainers_from(trusted_file(root / APPROVERS))
    base_policy = trusted_file(root / POLICY)
    path = f"/pulls/{number}"
    pr = client.get(path)
    if (pr.get("state") != "open" or pr.get("base", {}).get("ref") != "main"
            or pr.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY
            or pr.get("head", {}).get("sha") != expected_head):
        raise ApprovalError("pull-request-head-or-base-mismatch")
    policy = client.policy(expected_head)
    files = client.pages(path + "/files")
    if len(files) != pr.get("changed_files"):
        raise ApprovalError("incomplete-change-list")
    reviews = client.pages(path + "/reviews")
    verify_policy(base_policy, policy, maintainers, expected_head, reviews, files)
    current = client.get(path)
    if (current.get("head", {}).get("sha") != expected_head or current.get("state") != "open"
            or current.get("base") != pr.get("base")):
        raise ApprovalError("pull-request-changed-during-verification")
    if client.pages(path + "/reviews") != reviews:
        raise ApprovalError("reviews-changed-during-verification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-request", type=int, required=True)
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    if args.pull_request <= 0 or re.fullmatch(r"[0-9a-f]{40}", args.expected_head) is None:
        raise ApprovalError("invalid-verification-target")
    verify_current(GitHub(), args.pull_request, args.expected_head, Path(__file__).resolve().parents[1])
    print("Publication approval verified against trusted maintainer identities and live GitHub reviews.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never publish candidate text, response bodies, metadata or credentials.
        raise SystemExit("Publication approval blocked: trusted verification did not complete or approval is missing.")
