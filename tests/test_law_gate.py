"""Offline law-gate acceptance: real Git objects and synthetic recorded API JSON."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import urllib.error

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_gate():
    spec = importlib.util.spec_from_file_location("law_gate", ROOT / "scripts/law-gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Repo:
    def __init__(self, root, gate):
        self.root, self.gate, self.responses, self.statuses = root, gate, {}, []
        self.git("init", "-b", "main")
        self.git("remote", "add", "origin", str(root))
        for path in ("docs/FIRST_RUN.md", "src/a.py", "pyproject.toml", "requirements/application.lock"):
            self.write(path, "original\n")
        self.base = self.snapshot()
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        # fixture-write: Synthetic files in an isolated temporary Git repository.
        target.write_bytes(text if isinstance(text, bytes) else text.encode())

    def snapshot(self, subject="synthetic (#1)", parents=None):
        self.git("add", ".")
        parents = parents if parents is not None else ([self.git("rev-parse", "HEAD")] if (self.root / ".git/refs/heads/main").exists() else [])
        args = [arg for parent in parents for arg in ("-p", parent)]
        head = self.git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                        "commit-tree", self.git("write-tree"), *args, "-m", subject)
        self.git("update-ref", "HEAD", head)
        return head

    def pr(self, proof=True, labels=(), author="neilofneils404", number=1):
        head = self.git("rev-parse", "HEAD")
        self.git("update-ref", f"refs/pull/{number}/head", head)
        pr = {"number": number, "head": {"sha": head}, "base": {"ref": "main"},
              "labels": [{"name": label} for label in labels], "user": {"login": author}}
        self.responses[f"pulls/{number}"] = pr
        self.responses[f"issues/{number}/comments?per_page=100&page=1"] = [comment(head)] if proof else []
        return pr

    def urlopen(self, request, **kwargs):
        prefix = "https://api.github.com/repos/synthetic/repo/"
        assert request.full_url.startswith(prefix)
        key = request.full_url[len(prefix):]
        if request.data is not None:
            assert key.startswith("statuses/")
            self.statuses.append((key[9:], json.loads(request.data)))
            payload = {}
        else:
            payload = self.responses[key]
            if isinstance(payload, Exception):
                raise payload
        return io.BytesIO(json.dumps(payload).encode())

    def check(self, pr, rule=None):
        result = self.gate.run({"pull_request": pr, "number": pr["number"]})
        sha, status = self.statuses[-1]
        assert sha == pr["head"]["sha"] and status["context"] == "law-gate"
        assert status["state"] == ("failure" if rule else "success")
        assert result == (1 if rule else 0)
        if rule:
            assert rule in status["description"]


def comment(head, kind="spec-approved specs/synthetic.md", author="neilofneils404"):
    return {"body": f"law: {kind} @ {head}", "user": {"login": author},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    gate = load_gate()
    repo = Repo(tmp_path, gate)
    monkeypatch.chdir(tmp_path)
    for name, value in {"LAW_APPROVER": "neilofneils404", "GITHUB_REPOSITORY": "synthetic/repo", "GITHUB_TOKEN": "synthetic"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(gate.urllib.request, "urlopen", repo.urlopen)
    return repo


@pytest.mark.parametrize("lines,exception,label,fail", [(499, False, False, False), (500, False, False, False),
    (501, False, False, True), (501, True, False, False), (501, False, True, True)])
def test_case1_size(repo, lines, exception, label, fail):
    repo.write("src/new.py", "synthetic\n" * lines)
    head = repo.snapshot()
    pr = repo.pr(labels=["size-exception"] if label else [])
    if exception:
        repo.responses["issues/1/comments?per_page=100&page=1"].append(comment(head, "size-exception"))
    repo.check(pr, "R1" if fail else None)


def test_case2_exclusions_and_renames(repo):
    repo.write("requirements/application.lock", "original\n" + "lock\n" * 3000)
    repo.write("src/a.py", "original\n" + "code\n" * 20)
    repo.snapshot()
    repo.check(repo.pr())
    repo.git("mv", "src/a.py", "a.lock")
    repo.write("a.lock", "code\n" * 501)
    repo.snapshot()
    repo.check(repo.pr(), "R1")


def test_case3_base(repo):
    repo.write("src/new.py", "code\n" * 10)
    repo.snapshot()
    pr = repo.pr()
    pr["base"]["ref"] = "feature/a"
    repo.check(pr, "R2")


@pytest.mark.parametrize("variant", ["valid", "old", "dependabot[bot]", "github-actions[bot]", "prose", "short", "path", "label", "edited", "deleted"])
def test_case4_proofs(repo, variant):
    pr = repo.pr(labels=["spec-approved"])
    proof = comment(pr["head"]["sha"])
    if variant == "old": proof = comment("f" * 40)
    if variant.endswith("[bot]"): proof["user"]["login"] = variant
    if variant == "prose": proof["body"] = "Approved"
    if variant == "short": proof["body"] = proof["body"][:-1]
    if variant == "path": proof["body"] = proof["body"].replace("specs/", "")
    if variant == "edited": proof["updated_at"] = "2026-01-02T00:00:00Z"
    repo.responses["issues/1/comments?per_page=100&page=1"] = [] if variant in {"label", "deleted"} else [proof]
    repo.check(pr, None if variant == "valid" else "R3")


@pytest.mark.parametrize("path,lines,fail", [("docs/FIRST_RUN.md", 3, False), ("src/a.py", 1, True),
    ("tests/new.py", 1, True), (".github/workflows/new.yml", 1, True), ("new.md", 1, True), ("docs/FIRST_RUN.md", 41, True), ("docs/FIRST_RUN.md", 40, False)])
def test_case5_mechanical(repo, path, lines, fail):
    repo.write(path, "original\n" + "typo\n" * lines)
    repo.snapshot()
    repo.check(repo.pr(False, ["mechanical"]), "R3" if fail else None)
    if fail:
        repo.check(repo.pr(True, ["mechanical"]), "R3")


def test_case5_merge_main(repo):
    old = repo.base
    repo.write("src/a.py", "main update\n")
    main = repo.snapshot()
    repo.git("update-ref", "refs/remotes/origin/main", main)
    repo.snapshot(parents=[old, main])
    repo.check(repo.pr(False, ["mechanical"]))
    repo.write("src/a.py", "conflict edit\n")
    repo.snapshot(parents=[old, main])
    repo.check(repo.pr(False, ["mechanical"]), "R3")


@pytest.mark.parametrize("author,extra,fail", [("dependabot[bot]", False, False), ("dependabot[bot]", True, True), ("neilofneils404", False, True)])
def test_case6_dependabot(repo, author, extra, fail):
    for path in ["pyproject.toml", "requirements/application.lock"] + (["tests/test_dependency_updates.py"] if extra else []):
        repo.write(path, "dependency\n")
    repo.snapshot()
    repo.check(repo.pr(False, author=author), "R3" if fail else None)


@pytest.mark.parametrize("labels,proof,fail", [(["review-fail-1"], False, False), (["review-fail-2"], False, True),
    (["review-fail-2"], True, False), (["review-fail-2", "spec-rewritten"], False, True)])
def test_case7_two_strikes(repo, labels, proof, fail):
    pr = repo.pr(labels=labels)
    if proof:
        repo.responses["issues/1/comments?per_page=100&page=1"].append(comment(pr["head"]["sha"], "spec-rewritten specs/synthetic.md"))
    repo.check(pr, "R4" if fail else None)


def test_case8_reruns(repo):
    repo.check(repo.pr(False, ["mechanical"]))
    repo.check(repo.pr(False), "R3")
    pr = repo.pr()
    assert repo.gate.run({"issue": {"number": 1, "pull_request": {}}, "action": "created"}) == 0
    assert repo.statuses[-1][0] == pr["head"]["sha"]
    repo.responses["issues/1/comments?per_page=100&page=1"] = []
    assert repo.gate.run({"issue": {"number": 1, "pull_request": {}}, "action": "deleted"}) == 1
    repo.responses["pulls/1"] = urllib.error.HTTPError("synthetic", 404, "missing", {}, None)
    assert repo.gate.run({"issue": {"number": 1, "pull_request": {}}, "action": "edited"}) == 1
    assert repo.statuses[-1][0] == pr["head"]["sha"] and repo.statuses[-1][1]["state"] == "failure"


@pytest.mark.parametrize("variant", ["pass", "strike", "subject", "404", "stale", "trailing"])
def test_case9_merge_group(repo, variant, capsys):
    repo.write("src/a.py", "first\n")
    first = repo.snapshot("first (#1)")
    repo.pr()
    repo.write("src/a.py", "second\n")
    head = repo.snapshot({"subject": "missing number", "trailing": "Follow up to (#1) (#2)"}.get(variant, "second (#2)"))
    repo.pr(number=2, labels=["review-fail-2"] if variant in {"strike", "trailing"} else [])
    if variant == "404": repo.responses["pulls/2"] = urllib.error.HTTPError("synthetic", 404, "missing", {}, None)
    if variant == "stale": repo.responses["issues/2/comments?per_page=100&page=1"] = [comment(first)]
    assert repo.gate.run({"merge_group": {"base_sha": repo.base, "head_sha": head}}) == (0 if variant == "pass" else 1)
    assert repo.statuses[-1][0] == head
    if variant in {"strike", "trailing"}: assert "#2" in capsys.readouterr().out


def test_case10_trusted_code_and_workflow(repo):
    repo.write("scripts/law-gate.py", "raise SystemExit(0)\n")
    repo.snapshot()
    repo.check(repo.pr(False), "R3")
    workflow = yaml.safe_load((ROOT / ".github/workflows/law-gate.yml").read_text())
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read", "statuses": "write"}
    assert workflow["env"]["LAW_APPROVER"] == "neilofneils404"
    events = workflow.get("on", workflow.get(True))
    assert events["pull_request_target"]["types"] == ["opened", "synchronize", "reopened", "edited", "labeled", "unlabeled"]
    assert events["issue_comment"]["types"] == ["created", "edited", "deleted"] and "merge_group" in events
    job = workflow["jobs"]["law-gate-run"]
    assert "github.event.issue.pull_request" in job["if"]
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.merge_group.base_sha || 'refs/heads/main' }}"
    assert checkout["with"]["persist-credentials"] is False
    assert len(checkout["uses"].split("@")[1].split()[0]) == 40
    assert [step["run"] for step in job["steps"] if "run" in step] == ["python3 scripts/law-gate.py"]


@pytest.mark.parametrize("value", [None, ""])
def test_missing_approver_fails_closed(repo, monkeypatch, value):
    if value is None: monkeypatch.delenv("LAW_APPROVER")
    else: monkeypatch.setenv("LAW_APPROVER", value)
    repo.check(repo.pr(), "LAW_APPROVER")


def test_pagination_all_rules_and_binary_report(repo, capsys):
    repo.write("src/new.py", "synthetic\n" * 501)
    repo.write("asset.bin", b"\0synthetic")
    head = repo.snapshot()
    pr = repo.pr(False, ["review-fail-2"])
    pr["base"]["ref"] = "other"
    repo.check(pr, "R1")
    report = capsys.readouterr().out
    assert all(rule in report for rule in ("R1", "R2", "R3", "R4", "501:", "asset.bin", "Binary (0)"))
    pr["base"]["ref"], pr["labels"] = "main", []
    repo.responses["issues/1/comments?per_page=100&page=1"] = [comment("f" * 40)] * 100
    repo.responses["issues/1/comments?per_page=100&page=2"] = [comment(head), comment(head, "size-exception")]
    repo.check(pr)


@pytest.mark.parametrize("old,new,expected", [("src/a.py", "package-lock.json", 501),
    ("requirements/a.lock", "nested/uv.lock", 0), ("package-lock.json", "src/new.py", 501)])
def test_rename_exclusion_requires_both_paths(repo, old, new, expected):
    original = "".join(f"synthetic {n}\n" for n in range(2000))
    repo.write(old, original)
    base = repo.snapshot()
    (repo.root / new).parent.mkdir(parents=True, exist_ok=True)
    repo.git("mv", "-f", old, new)
    repo.write(new, original + "added\n" * 501)
    head = repo.snapshot()
    rows = repo.gate.changes(base, head)
    assert ((old, new), expected, False) in rows


def test_mechanical_deletion(repo):
    # fixture-write: Remove a synthetic tracked document in the temporary repo.
    (repo.root / "docs/FIRST_RUN.md").unlink()
    repo.snapshot()
    repo.check(repo.pr(False, ["mechanical"]), "R3")
