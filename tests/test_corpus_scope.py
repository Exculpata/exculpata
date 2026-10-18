"""Execute the workflow's scope classifier against real synthetic Git histories."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/corpus.yml'


def classifier_script():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    step = workflow.split('      - name: Classify full corpus changes conservatively\n', 1)[1]
    step = step.split('\n      - ', 1)[0]
    assert '        id: scope\n' in step
    return dedent(step.split('        run: |\n', 1)[1])


def isolated_git_environment():
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(key, None)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               PATH=str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', ''))
    return env


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()

    def git(*args):
        return subprocess.check_output(
            ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false', *args],
            cwd=root, env=isolated_git_environment(), text=True, stderr=subprocess.PIPE, timeout=15).strip()

    git('init', '-qb', 'main')
    git('config', 'user.name', 'Synthetic Contributor')
    git('config', 'user.email', 'contributor@example.test')
    git('config', 'diff.renames', 'true')
    return root, git


def commit(git):
    git('add', '-A')
    git('commit', '-qm', 'Synthetic scope change')
    return git('rev-parse', 'HEAD')


def classify(repository, base):
    root, _git = repository
    output = root.parent / 'scope-output'
    env = {**isolated_git_environment(), 'GITHUB_OUTPUT': str(output)}
    if base is None:
        env.pop('BASE_SHA', None)
    else:
        env['BASE_SHA'] = base
    subprocess.run(['bash', '-e', '-c', classifier_script()], cwd=root, env=env,
                   check=True, capture_output=True, text=True, timeout=20)
    return output.read_text(encoding='utf-8')


@pytest.mark.parametrize('before,after,required', [
    ('benchmarks/corpus/v1/manifest.json', 'archive/synthetic-manifest.json', True),
    ('scripts/corpus_check.py', 'tools/synthetic-check.py', True),
    ('.github/workflows/corpus.yml', 'archive/synthetic-workflow.yml', True),
    ('docs/synthetic-before.md', 'docs/synthetic-after.md', False),
])
def test_rename_preserves_original_corpus_scope(repository, before, after, required):
    root, git = repository
    original, moved = root / before, root / after
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text('Synthetic scope fixture, unchanged across its rename.\n')
    base = commit(git)
    moved.parent.mkdir(parents=True, exist_ok=True)
    original.rename(moved)
    commit(git)
    # Establish that Git actually recognizes the rename that caused the gap.
    assert git('diff', '--name-status', base + '...HEAD').startswith('R100\t')
    assert classify(repository, base) == 'full_required=' + str(required).lower() + '\n'


@pytest.mark.parametrize('base', [None, 'f' * 40])
def test_unavailable_base_keeps_full_corpus_required(repository, base):
    root, git = repository
    (root / 'README.md').write_text('Synthetic base fixture.\n')
    commit(git)
    assert classify(repository, base) == 'full_required=true\n'


def test_makefile_recipe_change_requires_full_corpus(repository):
    root, git = repository
    recipe = root / 'Makefile'
    recipe.write_text('corpus-check-full:\n\tpython scripts/corpus_check.py --full\n')
    base = commit(git)
    recipe.write_text('corpus-check-full:\n\tpython scripts/corpus_check.py\n')
    commit(git)
    assert classify(repository, base) == 'full_required=true\n'
