"""Exercise scope decisions against real synthetic Git histories."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/classify-quality-change.py"
spec = importlib.util.spec_from_file_location("quality_scope", SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def git(*args):
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.PIPE).strip()


def save(path, content="Synthetic documentation\n"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit():
    git("add", "-A")
    git("commit", "-qm", "Synthetic change", "--allow-empty")
    return git("rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git("init", "-qb", "main")
    git("config", "user.name", "Synthetic Contributor")
    git("config", "user.email", "contributor@example.com")
    save("README.md")
    save("docs/old.md")
    base = commit()
    git("update-ref", "refs/remotes/origin/main", base)
    return base


def push(base, head, **extra):
    return {"before": base, "after": head,
            "repository": {"default_branch": "main"}, **extra}


@pytest.mark.parametrize("path,expected", [
    ("README.md", True), ("docs/nested/change.md", True),
    ("docs/name with spaces\nand newline.md", True),
    ("src/runtime.md", False), ("tests/fixtures/data.md", False),
    ("docs/script.py", False), ("docs/image.png", False),
    (".github/workflows/quality-gates.yml", False),
    ("scripts/classify-quality-change.py", False), ("pyproject.toml", False),
    ("Makefile", False), ("AGENTS.md", False), ("unknown.md", False),
])
def test_main_push_uses_narrow_documentation_allowlist(history, path, expected):
    save(path, "Changed synthetic content\n")
    head = commit()
    assert scope.classify("push", push(history, head), head, "refs/heads/main") is expected


def test_new_documentation_branch_and_pr_use_complete_diff(history):
    git("checkout", "-qb", "docs-change")
    save("docs/new.md")
    head = commit()
    assert scope.classify("push", push("0" * 40, head, created=True), head, "refs/heads/docs-change")
    event = {"pull_request": {"base": {"sha": history}, "head": {"sha": head}}}
    assert scope.classify("pull_request", event, head, "refs/pull/1/merge")


def test_docs_followup_cannot_hide_earlier_code_on_feature_branch(history):
    git("checkout", "-qb", "feature")
    save("src/changed.py", "value = 1\n")
    code = commit()
    save("docs/new.md")
    head = commit()
    assert not scope.classify("push", push(code, head), head, "refs/heads/feature")
    event = {"pull_request": {"base": {"sha": history}, "head": {"sha": head}}}
    assert not scope.classify("pull_request", event, head, "refs/pull/1/merge")


def test_doc_deletion_and_rename_but_not_code_rename(history):
    Path("docs/old.md").rename("docs/renamed.md")
    head = commit()
    assert scope.documentation_diff(history, head)
    Path("docs/renamed.md").unlink()
    deleted = commit()
    assert scope.documentation_diff(head, deleted)
    save("src/material.md")
    code = commit()
    Path("src/material.md").rename("docs/material.md")
    moved = commit()
    assert not scope.documentation_diff(code, moved)


@pytest.mark.parametrize("kind", ["symlink", "executable", "mixed", "empty"])
def test_non_plain_docs_and_empty_changes_require_full_gates(history, kind):
    if kind == "symlink":
        Path("docs/link.md").symlink_to("../README.md")
    elif kind == "executable":
        Path("README.md").chmod(0o755)
    elif kind == "mixed":
        save("docs/new.md")
        save("src/new.py")
    head = commit()
    assert not scope.classify("push", push(history, head), head, "refs/heads/main")


def test_tags_forced_deletions_unknown_events_and_wrong_checkout_require_full(history):
    save("docs/new.md")
    head = commit()
    event = push(history, head)
    assert not scope.classify("push", event, head, "refs/tags/v-synthetic")
    assert not scope.classify("push", {**event, "forced": True}, head, "refs/heads/main")
    assert not scope.classify("push", {**event, "deleted": True}, head, "refs/heads/main")
    assert not scope.classify("workflow_dispatch", event, head, "refs/heads/main")
    assert not scope.classify("push", event, history, "refs/heads/main")


@pytest.mark.parametrize("problem", ["missing-base", "missing-default", "invalid-json", "unknown-ref"])
def test_cli_falls_back_to_full_when_comparison_is_unavailable(history, tmp_path, problem):
    save("docs/new.md")
    head = commit()
    event = push(history, head)
    ref = "refs/heads/main"
    if problem == "missing-base":
        event["before"] = "f" * 40
    elif problem == "missing-default":
        event["repository"] = {}
    elif problem == "unknown-ref":
        ref = "refs/heads/feature"
        git("update-ref", "-d", "refs/remotes/origin/main")
    event_path = tmp_path / "event.json"
    event_path.write_text("{" if problem == "invalid-json" else json.dumps(event))
    output = tmp_path / "outputs"
    result = subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ,
        "GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": head, "GITHUB_REF": ref, "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True)
    assert result.returncode == 0
    assert output.read_text() == "docs_only=false\n"
    assert "full gates required" in result.stdout


def test_required_application_check_cannot_succeed_after_classification_or_publication_failure():
    workflow = (ROOT / ".github/workflows/quality-gates.yml").read_text()
    application = workflow.split("\n  application:\n", 1)[1].split("\n  postgres-integration:\n", 1)[0]
    # Keep the required checks fail-closed while application and publication run
    # concurrently. The existing required deployment check now enforces publication.
    assert "needs: [change-scope]\n" in application
    assert "if: ${{ always() }}" in application
    assert 'SCOPE_RESULT: ${{ needs.change-scope.result }}' in application
    assert 'run: test "$SCOPE_RESULT" = success' in application
    deployment = workflow.split("\n  deployment-contract:\n", 1)[1].split("\n  synthetic-browser:\n", 1)[0]
    assert "needs: [change-scope, publication-scan]" in deployment
    assert "if: ${{ always() && (needs.change-scope.result != 'success' || needs.publication-scan.result != 'success' || needs.change-scope.outputs.docs_only != 'true') }}" in deployment
    assert 'SCOPE_RESULT: ${{ needs.change-scope.result }}' in deployment
    assert 'PUBLICATION_RESULT: ${{ needs.publication-scan.result }}' in deployment
    assert 'test "$SCOPE_RESULT" = success && test "$PUBLICATION_RESULT" = success' in deployment
    assert "continue-on-error:" not in application + deployment
    for job in ("postgres-integration", "transcription"):
        block = workflow.split(f"\n  {job}:\n", 1)[1]
        assert block.startswith("    needs: change-scope\n    if: ${{ always() && (needs.change-scope.result != 'success' || needs.change-scope.outputs.docs_only != 'true') }}\n")
    browser = workflow.split("\n  synthetic-browser:\n", 1)[1]
    assert browser.startswith("    needs: change-scope\n    if: ${{ always() && contains(fromJSON('[\"merge_group\", \"schedule\", \"workflow_dispatch\"]'), github.event_name) && (needs.change-scope.result != 'success' || needs.change-scope.outputs.docs_only != 'true') }}\n")
    assert 'run: test "$SCOPE_RESULT" = success' in browser
    assert "  push:\n    branches: [main]\n" in workflow
    assert "  pull_request:\n  merge_group:\n  schedule:\n    - cron: '17 7 * * *'\n  workflow_dispatch:\n" in workflow
    assert "paths-ignore:" not in workflow and "paths:" not in workflow
