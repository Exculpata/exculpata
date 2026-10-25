"""Exercise CI corpus protection against real synthetic filesystem changes."""
import os
from pathlib import Path
import stat
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/corpus-write-guard.sh"


@pytest.fixture
def corpus_repository(tmp_path):
    corpus = tmp_path / "benchmarks/corpus"
    corpus.mkdir(parents=True)
    tracked = corpus / "expected.json"
    # fixture-write: Standalone synthetic corpus copy for a disposable Git repo.
    tracked.write_text('{"synthetic": true}\n')
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    env.update(GIT_AUTHOR_NAME="Synthetic Contributor", GIT_AUTHOR_EMAIL="synthetic@example.test",
               GIT_COMMITTER_NAME="Synthetic Contributor", GIT_COMMITTER_EMAIL="synthetic@example.test")

    def git(*args, **kwargs):
        return subprocess.check_output(["git", *args], cwd=tmp_path, env=env,
                                       text=True, timeout=15, **kwargs).strip()

    git("init", "-q")
    git("add", "benchmarks/corpus")
    tree = git("write-tree")
    revision = git("commit-tree", tree, input="Synthetic corpus baseline\n")
    git("update-ref", "HEAD", revision)
    yield tmp_path, corpus, tracked, env
    # Restore directory permissions so temporary cleanup can remove its children.
    for path in corpus.rglob("*"):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    corpus.chmod(corpus.stat().st_mode | stat.S_IWUSR)


def run_guard(repository, mode):
    root, _corpus, _tracked, env = repository
    return subprocess.run(["bash", str(GUARD), mode], cwd=root, env=env,
                          capture_output=True, text=True, timeout=15)


def test_unchanged_corpus_passes_and_is_read_only(corpus_repository):
    _root, corpus, tracked, _env = corpus_repository
    prepared = run_guard(corpus_repository, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    for path in (corpus, tracked):
        assert path.stat().st_mode & 0o222 == 0
    checked = run_guard(corpus_repository, "check")
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "staged"])
def test_changed_corpus_fails(corpus_repository, mutation):
    root, corpus, tracked, env = corpus_repository
    prepared = run_guard(corpus_repository, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    # Deliberately bypass permissions to exercise the independent post-run check.
    target = tracked if mutation != "untracked" else corpus / "new-synthetic.json"
    writable = corpus if mutation == "untracked" else tracked
    writable.chmod(writable.stat().st_mode | stat.S_IWUSR)
    # fixture-write: Deliberate synthetic corpus corruption must fail the guard.
    target.write_text('{"synthetic": "changed"}\n')
    if mutation == "staged":
        subprocess.run(["git", "add", "benchmarks/corpus"], cwd=root, env=env,
                       check=True, timeout=15)
    checked = run_guard(corpus_repository, "check")
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert target.name in checked.stdout + checked.stderr


@pytest.mark.parametrize("workflow,job,test_command", [
    ("corpus.yml", "fast", "python -m pytest"),
    ("corpus.yml", "full", "make corpus-check-full"),
    ("quality-gates.yml", "application", "-m pytest -n auto"),
])
def test_workflows_bracket_tests_with_the_shared_guard(workflow, job, test_command):
    data = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = data["jobs"][job]["steps"]
    prepare = next(i for i, step in enumerate(steps)
                   if step.get("run") == "bash scripts/corpus-write-guard.sh prepare")
    check = next(i for i, step in enumerate(steps)
                 if step.get("run") == "bash scripts/corpus-write-guard.sh check")
    test = next(i for i, step in enumerate(steps) if test_command in step.get("run", ""))
    assert prepare < test < check
    assert "always()" in steps[check]["if"]
