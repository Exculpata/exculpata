"""Exercise scope decisions against real synthetic Git histories."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from tests.runtime_probes import browser_workflow_condition


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
    assert output.read_text() == "docs_only=false\nbrowser_changed=true\n"
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
    assert browser.startswith("    needs: change-scope\n    if: >-\n")
    assert "browser_changed: ${{ steps.scope.outputs.browser_changed }}" in workflow
    assert "continue-on-error:" not in browser
    assert 'run: test "$SCOPE_RESULT" = success' in browser
    assert "  push:\n    branches: [main]\n" in workflow
    assert "  pull_request:\n  merge_group:\n  schedule:\n    - cron: '17 7 * * *'\n  workflow_dispatch:\n" in workflow
    assert "paths-ignore:" not in workflow and "paths:" not in workflow


@pytest.mark.parametrize("change", ["added", "modified", "deleted", "renamed-in", "renamed-out"])
@pytest.mark.parametrize("path", [
    "scripts/browser-accept-synthetic.py", "scripts/run-browser-acceptance.py",
    "scripts/synthetic_browser_environment.py", "scripts/browser_appearance.py",
    "scripts/browser_case_exit.py", "scripts/install-ci-browser-deps.py",
    "scripts/demo.py", "scripts/exculpata_theme_contrast.py",
    "config/browser-testing.json", "pyproject.toml",
    "tests/test_browser_acceptance_runner.py", "scripts/classify-quality-change.py",
    "scripts/qa-people-browser.py", "scripts/nested/new-browser-tool.sh",
    "scripts/browser-fixtures/setup.txt", "scripts/run-browser-acceptance.py.example",
    "src/synthetic/static/scripts/new asset.js",
    "src/synthetic/nested/static/icons/new.svg",
    "src/synthetic/templates/components/new.html", "src/templates/new.html",
])
def test_browser_pr_uses_complete_diff_after_documentation_followup(history, change, path):
    path = Path(path)
    source = Path("scripts/other.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    source.parent.mkdir(parents=True, exist_ok=True)
    if change in {"modified", "deleted", "renamed-out"}:
        save(path, "# Synthetic journey\n")
    elif change == "renamed-in":
        save(source, "# Synthetic journey\n")
    commit()
    git("checkout", "-qb", "browser-change")
    if change in {"added", "modified"}:
        save(path, "# Changed synthetic journey\n")
    elif change == "deleted":
        path.unlink()
    elif change == "renamed-in":
        source.rename(path)
    else:
        path.rename(source)
    commit()
    save("docs/followup.md")
    head = commit()
    # Target-branch changes must not hide a script added by the PR.
    git("checkout", "main")
    save("docs/target.md")
    target = commit()
    git("merge", "--no-ff", "browser-change", "-m", "Synthetic integration")
    integration = git("rev-parse", "HEAD")
    event = {"pull_request": {"base": {"sha": target}, "head": {"sha": head}}}
    assert scope.browser_changed("pull_request", event, integration)
    assert not scope.classify("pull_request", event, integration, "refs/pull/1/merge")


@pytest.mark.parametrize("path,expected", [
    ("scripts/browser-accept-new-journey.py", True),
    ("scripts/browser-accept-name with spaces\nand newline.sh", True),
    (".github/workflows/quality-gates.yml", True),
    ("scripts/run-browser-acceptance.py", True),
    ("scripts/synthetic_browser_environment.py", True),
    ("scripts/browser_appearance.py", True),
    ("scripts/browser_case_exit.py", True),
    ("scripts/browser_new_helper.py", True),
    ("scripts/install-ci-browser-deps.py", True),
    ("scripts/demo.py", True),
    ("scripts/exculpata_theme_contrast.py", True),
    ("config/browser-testing.json", True),
    ("pyproject.toml", True),
    ("tests/test_browser_acceptance_runner.py", True),
    ("scripts/classify-quality-change.py", True),
    ("docs/browser-accept-guide.md", False),
    ("docs/browser_appearance.py", False),
    ("config/unrelated.json", False),
    ("scripts/run-browser-acceptance.py.example", True),
    ("scripts/qa-people-browser.py", True),
    ("scripts/nested/new-browser-tool.sh", True),
    ("scripts/browser-fixtures/setup.txt", True),
    ("src/case_intelligence/static/workbench.js", True),
    ("src/case_intelligence/templates/workbench.html", True),
    ("src/synthetic/nested/static/new.css", True),
    ("src/static/new.txt", True),
    ("src/templates/new.html", True),
    ("src/synthetic/statics/helper.py", False),
    ("src/synthetic/static_helper.py", False),
    ("docs/templates/guide.md", False),
    ("docs/static/diagram.svg", False),
    ("scripts/other.py", False),
    ("src/browser-accept-synthetic.py", False),
])
def test_browser_scope_cli_outputs(history, tmp_path, path, expected):
    save(path)
    head = commit()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "base": {"sha": history}, "head": {"sha": head},
    }}))
    output = tmp_path / "outputs"
    subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ,
        "GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_SHA": head, "GITHUB_REF": "refs/pull/1/merge",
        "GITHUB_OUTPUT": str(output)}, check=True, capture_output=True)
    assert f"browser_changed={str(expected).lower()}\n" in output.read_text()


@pytest.mark.parametrize("raw", [b"", b"unterminated", b"x" * (scope.MAX_DIFF_BYTES + 1)],
                         ids=["empty", "unterminated", "oversized"])
def test_browser_unknown_diff_requires_coverage(history, monkeypatch, raw):
    real_git = scope.git
    monkeypatch.setattr(scope, "git", lambda *args: raw if args[0] == "diff" else real_git(*args))
    event = {"pull_request": {"base": {"sha": history}, "head": {"sha": history}}}
    assert scope.browser_changed("pull_request", event, history)


@pytest.mark.parametrize("problem", ["missing-base", "wrong-checkout", "invalid-json"])
def test_browser_pr_cli_requires_coverage_when_comparison_unavailable(history, tmp_path, problem):
    save("docs/new.md")
    head = commit()
    event = {"pull_request": {"base": {"sha": history}, "head": {"sha": head}}}
    if problem == "missing-base":
        event["pull_request"]["base"]["sha"] = "f" * 40
    event_path = tmp_path / "event.json"
    event_path.write_text("{" if problem == "invalid-json" else json.dumps(event))
    output = tmp_path / "outputs"
    subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ,
        "GITHUB_EVENT_PATH": str(event_path), "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_SHA": history if problem == "wrong-checkout" else head,
        "GITHUB_REF": "refs/pull/1/merge", "GITHUB_OUTPUT": str(output)},
        check=True, capture_output=True)
    assert output.read_text() == "docs_only=false\nbrowser_changed=true\n"


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "schedule", "workflow_dispatch", "push"])
@pytest.mark.parametrize("result", ["success", "failure", "cancelled"])
@pytest.mark.parametrize("docs", ["true", "false", ""])
@pytest.mark.parametrize("browser", ["true", "false", ""])
def test_browser_workflow_event_and_scope_decisions(event, result, docs, browser):
    actual = browser_workflow_condition(event, result, docs, browser)
    expected = (
        event in {"merge_group", "schedule", "workflow_dispatch"}
        and (result != "success" or docs != "true")
    ) or (event == "pull_request" and (result != "success" or browser != "false"))
    assert actual is expected


# Exact changed paths from PR #280; no source contents are copied.
PR_280_PATHS = (
    'docs/TIMELINE_DRAFT.md',
    'scripts/browser-accept-timeline-draft.py',
    'scripts/run-browser-acceptance.py',
    'src/case_intelligence/static/timeline-draft.css',
    'src/case_intelligence/templates/workbench_matter_tabs.html',
    'src/case_intelligence/templates/workbench_timeline_draft.html',
    'src/case_intelligence/templates/workbench_work_product_tabs.html',
    'src/case_intelligence/timeline_builder.py',
    'src/case_intelligence/timeline_dates.py',
    'src/case_intelligence/timeline_exports.py',
    'src/case_intelligence/timeline_jobs.py',
    'src/case_intelligence/timeline_promotion.py',
    'src/case_intelligence/timeline_routes.py',
    'src/case_intelligence/workbench.py',
    'tests/fixtures/synthetic/workflow_eval/chronology.json',
    'tests/snapshots/timeline_harbor_street.json',
    'tests/snapshots/workbench_routes.json',
    'tests/snapshots/workbench_routes_acceptance.json',
    'tests/test_browser_acceptance_runner.py',
    'tests/test_compact_matter_navigation.py',
    'tests/test_timeline_builder.py',
    'tests/test_timeline_dates.py',
    'tests/test_timeline_jobs.py',
    'tests/test_timeline_routes.py',
)


@pytest.mark.parametrize("paths,expected", [
    (PR_280_PATHS, True),
    (("scripts/run-browser-acceptance.py",), True),
    (("src/case_intelligence/static/workbench.js",), True),
    (("src/case_intelligence/templates/workbench.html",), True),
    (("scripts/qa-people-browser.py",), True),
    (("docs/guide.md",), False),
], ids=["pr-280", "harness-only", "static-js", "workbench-template",
        "new-browser-name", "docs-only"])
def test_browser_regression_file_lists(history, monkeypatch, paths, expected):
    real_git = scope.git

    def complete_diff(*args):
        if args[:3] == ("diff", "--name-only", "-z"):
            assert args[3:] == ("--no-renames", history, history, "--")
            return b"\0".join(path.encode() for path in paths) + b"\0"
        return real_git(*args)

    monkeypatch.setattr(scope, "git", complete_diff)
    event = {"pull_request": {"base": {"sha": history}, "head": {"sha": history}}}
    assert scope.browser_changed("pull_request", event, history) is expected
