"""Synthetic GitHub responses exercise the trusted approval boundary offline."""
import base64
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publication_approval", ROOT / "scripts/verify-publication-approval.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
HEAD = "a" * 40
BASE = "d" * 40
MAINTAINERS = {"synthetic-maintainer": 123}


def policy(**changes):
    entry = dict(sha256="b" * 64, suffix=".gif", reason="Synthetic pixel review",
                 approver="synthetic-maintainer", status="approved",
                 approved_on=date.today().isoformat(),
                 expires=(date.today() + timedelta(days=30)).isoformat())
    entry.update(changes)
    return json.dumps(dict(version=2, reviews=[entry])).encode()


def review(data, **changes):
    value = dict(id=1, user=dict(login="synthetic-maintainer", id=123, type="User"),
                 state="APPROVED", commit_id=HEAD, submitted_at="2026-10-17T00:00:00Z",
                 body="Publication exemptions approved for policy SHA-256: " + hashlib.sha256(data).hexdigest())
    value.update(changes)
    return value


def check(data, reviews, files=None):
    VERIFY.verify_policy(b"trusted previous policy", data, MAINTAINERS, HEAD, reviews,
                         files or [{"filename": VERIFY.POLICY}])


@pytest.mark.parametrize("state", ["APPROVED", "COMMENTED"])
def test_exact_head_explicit_maintainer_review_passes(state):
    data = policy()
    check(data, [review(data, state=state)])


@pytest.mark.parametrize("change", [
    {"state": "DISMISSED"}, {"state": "CHANGES_REQUESTED"}, {"state": "PENDING"},
    {"commit_id": "c" * 40}, {"body": "Looks good"}, {"submitted_at": None},
    {"body": "Publication exemptions approved for policy SHA-256: " + "0" * 64},
    {"user": {"login": "synthetic-maintainer", "id": 123, "type": "Bot"}},
    {"user": {"login": "synthetic-maintainer", "id": 456, "type": "User"}},
    {"user": {"login": "synthetic-agent", "id": 789, "type": "User"}},
])
def test_forged_stale_or_nonapproval_review_fails(change):
    data = policy()
    with pytest.raises(VERIFY.ApprovalError):
        check(data, [review(data, **change)])


def test_later_dismissal_or_change_request_revokes_approval():
    data = policy()
    for state in ("DISMISSED", "CHANGES_REQUESTED", "COMMENTED"):
        with pytest.raises(VERIFY.ApprovalError):
            check(data, [review(data, id=2, state=state, body=""), review(data)])


@pytest.mark.parametrize("changes", [
    {"approver": "codex-synthetic-fixture-review"},
    {"status": "pending", "approver": None, "approved_on": None},
    {"expires": (date.today() + timedelta(days=181)).isoformat()},
    {"expires": (date.today() - timedelta(days=1)).isoformat()},
    {"approved_on": (date.today() + timedelta(days=1)).isoformat()},
])
def test_policy_string_or_pending_request_is_not_approval(changes):
    data = policy(**changes)
    with pytest.raises(VERIFY.ApprovalError):
        check(data, [review(data)])


@pytest.mark.parametrize("path", sorted(VERIFY.PROTECTED) + [".github/workflows/new.yml"])
def test_protected_changes_require_review_even_with_unchanged_policy(path):
    data = policy()
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.verify_policy(data, data, MAINTAINERS, HEAD, [], [{"filename": path}])
    VERIFY.verify_policy(data, data, MAINTAINERS, HEAD, [review(data)], [{"filename": path}])


def test_renaming_protected_file_does_not_remove_review_requirement():
    data = policy()
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.verify_policy(data, data, MAINTAINERS, HEAD, [],
            [{"filename": "ordinary.txt", "previous_filename": VERIFY.APPROVERS}])


def test_unrelated_change_reuses_already_trusted_policy():
    data = policy()
    VERIFY.verify_policy(data, data, MAINTAINERS, HEAD, [], [{"filename": "ordinary.txt"}])


def test_unrelated_change_reuses_unchanged_trusted_pending_request(trusted_root):
    data = policy(status="pending", approver=None, approved_on=None)
    assert VERIFY.policy_approvers(data, MAINTAINERS, allow_pending=True) == set()
    (trusted_root / VERIFY.POLICY).write_bytes(data)
    client = FakeGitHub(data)
    client.files = [{"filename": "ordinary.txt"}]
    client.reviews = []
    VERIFY.verify_current(client, 1, HEAD, trusted_root, BASE)


@pytest.mark.parametrize("changed,path", [
    (False, VERIFY.POLICY), (True, VERIFY.POLICY), (True, "ordinary.txt"),
])
def test_pending_request_cannot_authorize_protected_or_new_policy(changed, path):
    data = policy(status="pending", approver=None, approved_on=None)
    base = policy() if changed else data
    with pytest.raises(VERIFY.ApprovalError, match="publication-exemption-pending"):
        VERIFY.verify_policy(base, data, MAINTAINERS, HEAD, [review(data)],
                             [{"filename": path}])


def test_activating_pending_request_requires_exact_head_approval():
    pending = policy(status="pending", approver=None, approved_on=None)
    approved = policy()
    # Content equality is checked independently of the reported path list.
    with pytest.raises(VERIFY.ApprovalError, match="maintainer-review-required"):
        VERIFY.verify_policy(pending, approved, MAINTAINERS, HEAD, [],
                             [{"filename": "ordinary.txt"}])
    VERIFY.verify_policy(pending, approved, MAINTAINERS, HEAD, [review(approved)],
                         [{"filename": VERIFY.POLICY}])


@pytest.mark.parametrize("changes", [
    {"approver": "synthetic-maintainer"}, {"approved_on": date.today().isoformat()},
    {"expires": "2026-99-99"}, {"sha256": "invalid"}, {"status": "unknown"},
])
def test_unrelated_change_does_not_skip_pending_schema_validation(changes):
    entry = dict(status="pending", approver=None, approved_on=None)
    entry.update(changes)
    data = policy(**entry)
    with pytest.raises(ValueError):
        VERIFY.verify_policy(data, data, MAINTAINERS, HEAD, [], [{"filename": "ordinary.txt"}])


class FakeGitHub:
    def __init__(self, data):
        self.data = data
        self.pr = dict(state="open", head=dict(sha=HEAD), changed_files=2,
                       base=dict(ref="main", sha=BASE, repo=dict(full_name=VERIFY.REPOSITORY)))
        self.files = [{"filename": VERIFY.APPROVERS}, {"filename": "scripts/verify-publication-approval.py"}]
        self.reviews = [review(data)]
        self.requests = []
        self.after = None
        self.comparisons = []

    def get(self, path):
        self.requests.append(path)
        return deepcopy(self.after if self.after and len(self.requests) > 1 else self.pr)

    def policy(self, head):
        assert head == HEAD
        return self.data

    def changed_files(self, base, head):
        assert (base, head) == (BASE, HEAD)
        self.comparisons.append((base, head))
        return self.files

    def pages(self, path):
        assert path.endswith("/reviews")
        return self.reviews


@pytest.fixture
def trusted_root(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / VERIFY.APPROVERS).write_text(json.dumps(dict(version=1,
        maintainers=[dict(login="synthetic-maintainer", id=123)])))
    (tmp_path / VERIFY.POLICY).write_bytes(policy())
    return tmp_path


def test_candidate_authority_and_verifier_are_never_fetched_or_executed(trusted_root):
    client = FakeGitHub(policy())
    VERIFY.verify_current(client, 1, HEAD, trusted_root, BASE)
    assert client.requests == ["/pulls/1", "/pulls/1"]
    assert client.comparisons == [(BASE, HEAD)]
    client.data = policy(approver="synthetic-agent")
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.verify_current(client, 1, HEAD, trusted_root, BASE)


@pytest.mark.parametrize("failure", ["head", "base", "closed", "stale-base", "race", "base-race"])
def test_live_target_changes_or_incomplete_api_data_fail(trusted_root, failure):
    client = FakeGitHub(policy())
    if failure == "head":
        client.pr["head"]["sha"] = "f" * 40
    elif failure == "base":
        client.pr["base"]["ref"] = "other"
    elif failure == "closed":
        client.pr["state"] = "closed"
    elif failure == "stale-base":
        client.pr["base"]["sha"] = "e" * 40
    elif failure == "base-race":
        client.after = deepcopy(client.pr)
        client.after["base"]["sha"] = "e" * 40
    else:
        client.after = deepcopy(client.pr)
        client.after["head"]["sha"] = "f" * 40
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.verify_current(client, 1, HEAD, trusted_root, BASE)


def test_api_pagination_exhaustion_and_bad_responses_fail(monkeypatch):
    client = VERIFY.GitHub()
    monkeypatch.setattr(client, "get", lambda path: [{}] * 100)
    with pytest.raises(VERIFY.ApprovalError, match="pagination-limit"):
        client.pages("/pulls/1/reviews")
    monkeypatch.setattr(client, "get", lambda path: {})
    with pytest.raises(VERIFY.ApprovalError):
        client.pages("/pulls/1/reviews")


def test_content_fetch_only_accepts_bounded_file_data(monkeypatch):
    client = VERIFY.GitHub()
    data = policy()
    value = dict(type="file", encoding="base64", size=len(data), content=base64.b64encode(data).decode())
    monkeypatch.setattr(client, "get", lambda path: value)
    assert client.policy(HEAD) == data
    value["type"] = "symlink"
    with pytest.raises(VERIFY.ApprovalError):
        client.policy(HEAD)


def test_duplicate_keys_and_symlinked_authority_fail(trusted_root):
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.decode(b'{"version":1,"version":2}')
    path = trusted_root / VERIFY.APPROVERS
    data = path.read_bytes()
    path.unlink()
    (trusted_root / "other.json").write_bytes(data)
    path.symlink_to(trusted_root / "other.json")
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.verify_current(FakeGitHub(policy()), 1, HEAD, trusted_root, BASE)


def test_workflow_executes_only_trusted_base_with_no_persistent_credentials():
    # Static tripwire supplements the behavioral trust tests; activation is a maintainer step.
    workflow = (ROOT / ".github/workflows/publication-approval.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert '--trusted-base "$GITHUB_SHA"' in workflow
    assert "python3 -I scripts/verify-publication-approval.py" in workflow
    assert "secrets." not in workflow and "write" not in workflow
    assert "pull_request.head.sha" not in workflow.split("with:", 1)[1].split("- name:", 1)[0]


@pytest.mark.parametrize("value", [
    [], {"version": True, "maintainers": []},
    {"version": 1, "maintainers": [{"login": "synthetic", "id": True}]},
    {"version": 1, "maintainers": [{"login": "synthetic", "id": 123}] * 2},
    {"version": 1, "maintainers": [{"login": "synthetic", "id": 123}, {"login": "other", "id": 123}]},
])
def test_malformed_or_ambiguous_authority_is_rejected(value):
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.maintainers_from(json.dumps(value).encode())


@pytest.mark.parametrize("field", ["approved_on", "expires"])
def test_dates_require_iso_calendar_form(field):
    data = policy(**{field: date.today().strftime("%Y%m%d")})
    with pytest.raises(VERIFY.ApprovalError):
        check(data, [review(data)])


def test_review_revocation_during_verification_fails(trusted_root):
    client = FakeGitHub(policy())
    original = client.pages
    reads = 0
    def pages(path):
        nonlocal reads
        if path.endswith("/reviews"):
            reads += 1
            if reads == 2:
                return [review(client.data, state="DISMISSED")]
        return original(path)
    client.pages = pages
    with pytest.raises(VERIFY.ApprovalError, match="reviews-changed"):
        VERIFY.verify_current(client, 1, HEAD, trusted_root, BASE)


def test_expired_entries_are_valid_but_grant_no_active_authorization():
    data = policy(approved_on=(date.today() - timedelta(days=40)).isoformat(),
                  expires=(date.today() - timedelta(days=10)).isoformat())
    assert VERIFY.policy_approvers(data, MAINTAINERS) == set()
    VERIFY.verify_policy(data, data, MAINTAINERS, HEAD, [], [{"filename": "ordinary.txt"}])
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.verify_policy(data, data, MAINTAINERS, HEAD, [], [{"filename": VERIFY.POLICY}])


def test_mutable_pr_file_counts_cannot_hide_protected_changes(trusted_root):
    client = FakeGitHub(policy())
    client.pr["changed_files"] = 0  # A transient ordinary-diff head is irrelevant.
    client.reviews = []
    with pytest.raises(VERIFY.ApprovalError, match="maintainer-review-required"):
        VERIFY.verify_current(client, 1, HEAD, trusted_root, BASE)
    assert client.comparisons == [(BASE, HEAD)]
    assert all(not path.endswith("/files") for path in client.requests)


def tree_entry(path, sha="1" * 40, mode="100644", kind="blob"):
    return dict(path=path, sha=sha, mode=mode, type=kind)


def test_changed_paths_are_pinned_to_immutable_commits_and_complete_trees(monkeypatch):
    client = VERIFY.GitHub()
    base_tree, head_tree = "b" * 40, "c" * 40
    responses = {
        "/git/commits/" + BASE: dict(sha=BASE, tree=dict(sha=base_tree)),
        "/git/commits/" + HEAD: dict(sha=HEAD, tree=dict(sha=head_tree)),
        "/git/trees/" + base_tree + "?recursive=1": dict(sha=base_tree, truncated=False, tree=[
            tree_entry(VERIFY.POLICY), tree_entry("ordinary.txt"), tree_entry("mode.txt")]),
        "/git/trees/" + head_tree + "?recursive=1": dict(sha=head_tree, truncated=False, tree=[
            tree_entry("renamed.json"), tree_entry("ordinary.txt"), tree_entry("mode.txt", mode="100755")]),
    }
    calls = []
    def get(path, **kwargs):
        calls.append((path, kwargs))
        return responses[path]
    monkeypatch.setattr(client, "get", get)
    assert client.changed_files(BASE, HEAD) == [
        {"filename": "mode.txt"}, {"filename": "renamed.json"}, {"filename": VERIFY.POLICY}]
    assert [path for path, _ in calls] == [
        "/git/commits/" + BASE, "/git/trees/" + base_tree + "?recursive=1",
        "/git/commits/" + HEAD, "/git/trees/" + head_tree + "?recursive=1"]
    assert all(kwargs.get("limit") == VERIFY.MAX_TREE_BYTES for path, kwargs in calls if "/trees/" in path)


@pytest.mark.parametrize("failure", ["truncated", "missing-truncation", "wrong-sha", "duplicate", "bad-path", "bad-mode"])
def test_incomplete_or_invalid_immutable_trees_fail_closed(monkeypatch, failure):
    client = VERIFY.GitHub()
    tree_sha = "b" * 40
    value = dict(sha=tree_sha, truncated=False, tree=[tree_entry(VERIFY.POLICY)])
    if failure == "truncated":
        value["truncated"] = True
    elif failure == "missing-truncation":
        del value["truncated"]
    elif failure == "wrong-sha":
        value["sha"] = "c" * 40
    elif failure == "duplicate":
        value["tree"] *= 2
    elif failure == "bad-path":
        value["tree"][0]["path"] = "../outside"
    else:
        value["tree"][0]["mode"] = "unknown"
    def get(path, **kwargs):
        return dict(sha=BASE, tree=dict(sha=tree_sha)) if "/commits/" in path else value
    monkeypatch.setattr(client, "get", get)
    with pytest.raises(VERIFY.ApprovalError):
        client.tree(BASE)


def test_oversized_json_remains_fail_closed():
    with pytest.raises(VERIFY.ApprovalError):
        VERIFY.decode(b" " * (VERIFY.MAX_TREE_BYTES + 1), VERIFY.MAX_TREE_BYTES)
