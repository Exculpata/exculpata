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
    ('src/case_intelligence/pilot_uploads.py', 'docs/archived-ingestion.md', True),
    ('services/ocr/Dockerfile', 'docs/archived-runtime.md', True),
    ('config/models.json', 'docs/archived-models.md', True),
    ('docs/synthetic-before.md', 'src/synthetic_new_runtime.py', True),
    ('future-runtime/dependency.lock', 'docs/archived-lock.md', True),
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


@pytest.mark.parametrize('path', [
    'src/case_intelligence/pilot_uploads.py', 'src/case_intelligence/media_evidence.py',
    'src/case_intelligence/workbench.py', 'services/transcription/Dockerfile',
    'services/ocr/requirements.txt', 'config/models.json', 'pyproject.toml',
    'uv.lock', 'Dockerfile', 'compose.yml', 'scripts/bootstrap-dev.py',
    '.github/actions/runtime/action.yml',
])
def test_product_and_runtime_dependency_changes_require_full_corpus(repository, path):
    root, git = repository
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('Synthetic original runtime dependency.\n')
    base = commit(git)
    target.write_text('Synthetic changed runtime dependency.\n')
    commit(git)
    assert classify(repository, base) == 'full_required=true\n'


@pytest.mark.parametrize('path,required', [
    ('docs/synthetic.md', False), ('docs/nested/synthetic.rst', False), ('README.md', False),
    ('CONTRIBUTING.md', False), ('CHANGELOG.md', False), ('LICENSE', False), ('NOTICE', False),
    ('docs/runtime.py', True), ('docs/settings.json', True), ('future-runtime/dependency.lock', True),
    ('tests/test_synthetic.py', True), ('benchmarks/corpus/README.md', True),
])
@pytest.mark.parametrize('operation', ['add', 'delete'])
def test_only_explicit_documentation_changes_may_skip_full(repository, path, required, operation):
    root, git = repository
    (root / 'sentinel.txt').write_text('Synthetic unchanged sentinel.\n')
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if operation == 'delete': target.write_text('Synthetic removable content.\n')
    base = commit(git)
    if operation == 'delete': target.unlink()
    else: target.write_text('Synthetic added content.\n')
    commit(git)
    assert classify(repository, base) == 'full_required=' + str(required).lower() + '\n'


def test_fast_and_full_workflow_lanes_keep_distinct_recognition_contracts():
    workflow = WORKFLOW.read_text()
    fast, full = workflow.split('\n  full:\n', 1)
    assert 'make corpus-check PYTHON=python' in fast
    assert 'make corpus-check-full' not in fast
    assert 'make corpus-check-full PYTHON=python' in full
    assert "if: github.event_name != 'pull_request' || needs.fast.outputs.full_required == 'true'" in full
    assert 'HF_HUB_OFFLINE:' in full and 'Stage pinned ASR snapshot' in full
