"""Synthetic CLI regressions for stale pull-request descriptions."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-pr-body.py"
HEAD = "a1234567" * 5
OLD = "b2345678" * 5


def run_check(body, *, base="main", head=HEAD, path=None, allow=()):
    command = [sys.executable, str(SCRIPT), "--head", head, "--base", base]
    for sha in allow:
        command.extend(["--allow", sha])
    if path is not None:
        # fixture-write: Synthetic PR body exercises the file-input CLI.
        path.write_text(body, encoding="utf-8")
        command.append(str(path))
    return subprocess.run(command, input=body, text=True, capture_output=True, check=False)


@pytest.mark.parametrize("body", [
    "Synthetic change with no revision claim.\n",
    f"Validated `{HEAD}` and {HEAD[:7].upper()}.\n",
    f"Earlier evidence: `{OLD}` failed before the fix.\nCurrent: {HEAD[:12]}.\n",
    f"- earlier evidence: {OLD[:7]} failed before the fix.\n",
    "Short token abc123 and nonhex abcdefg are not SHA citations.\n",
])
def test_accepts_current_head_and_explicit_history(body):
    result = run_check(body)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("body,problem", [
    (f"Current head: {OLD[:7]}.", "stale commit SHA"),
    ("Stacked on #42", "Stacked on #42"),
    ("Ready AFTER REBASE", "after rebase"),
    ("Synthetic \u2014 description", "em dash"),
    (f"Earlier evidence: {OLD[:7]}; on this head: {OLD[:8]}", "stale commit SHA"),
    (f"Earlier evidence:\nHead: {OLD}", "stale commit SHA"),
])
def test_rejects_stale_claims(body, problem):
    result = run_check(body)
    assert result.returncode == 1
    assert problem in result.stdout
    assert result.stderr == ""


def test_reports_each_problem_with_line_number_from_file(tmp_path):
    result = run_check(
        f"Current head: {OLD[:7]}.\nStacked on #42 after rebase \u2014 ready.\n",
        path=tmp_path / "body.md",
    )
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("line 1:")
    assert all(line.startswith("line 2:") for line in lines[1:])


@pytest.mark.parametrize("base", ["feature/parent", "main-fix"])
def test_stack_language_is_allowed_for_other_bases(base):
    result = run_check("Stacked on #42; after rebase.", base=base)
    assert result.returncode == 0


def test_main_ref_is_also_main():
    result = run_check("after\nrebase", base="refs/heads/main")
    assert result.returncode == 1
    assert result.stdout == "line 1: after rebase is stale when the base is main\n"


def test_sha256_head_and_abbreviations():
    head = "c1234567" * 8
    assert run_check(f"Validated {head} and {head[:7]}", head=head).returncode == 0
    assert run_check(f"Current head: {head} and {head[:7]}", head=head).returncode == 0


@pytest.mark.parametrize("base", ["", "main\nbranch"])
def test_invalid_base_fails_explicitly(base):
    result = run_check("Synthetic description", base=base)
    assert result.returncode == 1
    assert "unsupported base" in result.stdout


def test_non_main_still_checks_sha_and_punctuation():
    result = run_check(f"Head: {OLD} \u2014 synthetic", base="feature/parent")
    assert result.returncode == 1
    assert len(result.stdout.splitlines()) == 2


def test_explicit_stdin_and_invalid_utf8():
    command = [sys.executable, str(SCRIPT), "--head", HEAD, "--base", "main", "-"]
    clean = subprocess.run(command, input=b"Synthetic description", capture_output=True, check=False)
    assert clean.returncode == 0
    invalid = subprocess.run(command, input=b"\xff", capture_output=True, check=False)
    assert invalid.returncode == 1
    assert b"cannot read PR body" in invalid.stdout
    assert invalid.stderr == b""


@pytest.mark.parametrize("head", ["", "abc123", "z" * 40])
def test_invalid_head_fails_explicitly(head):
    result = run_check("Synthetic description", head=head)
    assert result.returncode == 1
    assert "unsupported" in result.stdout
    assert "check_body" in result.stdout


def test_missing_file_fails_without_traceback(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--head", HEAD, "--base", "main",
         str(tmp_path / "missing.md")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "cannot read PR body" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("token", [
    "39784257560", "6270997206", "Defaced", "abcdefa", "abc123",
    "d1234567" * 8, "sha256:" + "d1234567" * 8,
    "sha256: " + "d1234567" * 8, "d1234567" * 5 + "a",
    "prefix_b2345678", "b2345678_suffix", "prefixb2345678word",
    f"https://example.com/commit/{OLD}?head={OLD[:7]}#comment-{OLD[:8]}",
    f"[run](https://example.com/{OLD})", f"<https://example.com/{OLD}>",
    f"www.example.com/{OLD}", f"https://example.com/reference/({OLD})",
])
def test_non_commit_tokens_are_ignored_in_current_head_claims(token):
    result = run_check(f"Current head evidence: {token}")
    assert result.returncode == 0, result.stdout
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("length", [7, 8, 39, 40])
def test_mixed_hex_tokens_with_word_boundaries_are_checked(length):
    token = OLD[:length].upper()
    result = run_check(f"Head: `{token}`")
    assert result.returncode == 1
    assert f"stale commit SHA {token}" in result.stdout


@pytest.mark.parametrize("url", [
    f"https://example.com/{OLD}", f"[run](https://example.com/{OLD})",
    f"<https://example.com/{OLD}>",
])
def test_ignoring_urls_preserves_neighboring_citations_and_sentence_boundaries(url):
    stale = run_check(f"Head: {url}, `{OLD[:7]}`")
    assert stale.returncode == 1
    assert stale.stdout.count("stale commit SHA") == 1
    clean = run_check(f"Head: {url}. Historical tests failed on {OLD[:7]}.")
    assert clean.returncode == 0, clean.stdout


@pytest.mark.parametrize("claim", [
    "Current head is", "Head:", "on this head", "against this head",
    "current PR head:", "latest head:", "Current:",
])
def test_explicit_current_head_claims_reject_old_revisions(claim):
    result = run_check(f"{claim} `{OLD[:7]}`")
    assert result.returncode == 1
    assert "stale commit SHA" in result.stdout


@pytest.mark.parametrize("body", [
    f"Tests failed on {OLD[:7]} before the fix.",
    f"Hosted review covers {OLD[:7]} only.",
    f"Based on main {OLD}. Current head: {HEAD[:7]}.",
    f"Local validation was on earlier head `{OLD}`. The current head is `{HEAD[:7]}`.",
    f"Head: {HEAD[:7]}; Earlier evidence: {OLD[:7]}.",
    f"Head: {HEAD[:7]}. Tests failed on {OLD[:7]} before the fix.",
    f"Tests failed on {OLD[:7]} before the fix. Head: {HEAD[:7]}.",
    f"Current head: {HEAD[:7]}\nTests failed on {OLD[:7]} before the fix.",
])
def test_historical_citations_do_not_require_a_special_label(body):
    result = run_check(body)
    assert result.returncode == 0, result.stdout
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("label", [
    "Based on main", "Based on current main", "Regression baseline",
    "starting commit", "Earlier evidence", "earlier head",
])
@pytest.mark.parametrize("separator", ["; ", ". ", ", "])
def test_historical_labels_stop_at_the_next_current_claim(label, separator):
    result = run_check(f"{label}: {OLD[:7]}{separator}Current head: {OLD[:8]}")
    assert result.returncode == 1
    assert result.stdout.count("stale commit SHA") == 1
    assert f"stale commit SHA {OLD[:8]}" in result.stdout
    clean = run_check(f"Current head: {HEAD[:7]}, {label}: {OLD[:7]}")
    assert clean.returncode == 0, clean.stdout


def test_base_and_merge_base_allowances_are_repeatable():
    base, merge_base = OLD, "c3456789" * 5
    result = run_check(
        f"Current head comparison: {HEAD[:7]} {base[:8].upper()} {merge_base}",
        allow=(base, merge_base),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    stale = run_check(f"Head: {OLD[:7]}", allow=(merge_base,))
    assert stale.returncode == 1
    assert "stale commit SHA" in stale.stdout


@pytest.mark.parametrize("sha", ["", "abc123", "z" * 40])
def test_invalid_allowance_fails_explicitly(sha):
    result = run_check("Synthetic description", allow=(sha,))
    assert result.returncode == 1
    assert "unsupported allowed SHA" in result.stdout
    assert "check_body" in result.stdout


@pytest.mark.parametrize("name", ["pr342-old", "pr342-fixed", "pr359", "pr361", "pr362"])
def test_synthetic_pr_body_fixtures(name):
    fixture = json.loads((SCRIPT.parent.parent / "tests/fixtures/pr-body" / f"{name}.json").read_text())
    result = run_check(fixture["body"], head=fixture["headRefOid"], base=fixture["baseRefName"])
    print(f"\n{name}: head={fixture['headRefOid']}; exit={result.returncode}")
    print(result.stdout, end="" if result.stdout else "(no output)\n")
    assert result.stderr == ""
    if name == "pr342-old":
        assert result.returncode == 1
        assert result.stdout.count("stale commit SHA") == 2
        assert "00c4628" in result.stdout
    else:
        assert result.returncode == 0, result.stdout
        assert result.stdout == ""
