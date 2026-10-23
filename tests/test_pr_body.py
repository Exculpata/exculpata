"""Synthetic CLI regressions for stale pull-request descriptions."""

from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-pr-body.py"
HEAD = "a1234567" * 5
OLD = "b2345678" * 5


def run_check(body, *, base="main", head=HEAD, path=None):
    command = [sys.executable, str(SCRIPT), "--head", head, "--base", base]
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
    (f"Validated {OLD[:7]}.", "stale commit SHA"),
    (f"https://example.com/commit/{OLD}", "stale commit SHA"),
    ("Stacked on #42", "Stacked on #42"),
    ("Ready AFTER REBASE", "after rebase"),
    ("Synthetic \u2014 description", "em dash"),
    (f"Earlier evidence: {OLD[:7]}; current tests {OLD[:8]}", "stale commit SHA"),
    (f"Earlier evidence:\n{OLD}", "stale commit SHA"),
])
def test_rejects_stale_claims(body, problem):
    result = run_check(body)
    assert result.returncode == 1
    assert problem in result.stdout
    assert result.stderr == ""


def test_reports_each_problem_with_line_number_from_file(tmp_path):
    result = run_check(
        f"Validated {OLD[:7]}.\nStacked on #42 after rebase \u2014 ready.\n",
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
    assert run_check(head + "a", head=head).returncode == 1


@pytest.mark.parametrize("base", ["", "main\nbranch"])
def test_invalid_base_fails_explicitly(base):
    result = run_check("Synthetic description", base=base)
    assert result.returncode == 1
    assert "unsupported base" in result.stdout


def test_non_main_still_checks_sha_and_punctuation():
    result = run_check(f"{OLD} \u2014 synthetic", base="feature/parent")
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
