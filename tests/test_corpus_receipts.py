"""Receipts must distinguish executed working-tree code from a commit."""
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECKER_SOURCE = (ROOT / 'scripts/corpus_check.py').read_text()
RECOGNITION_SOURCE = (ROOT / 'scripts/corpus_recognition.py').read_text()


def checkout(tmp_path, *, change_during_run=False, change_corpus_during_run=None):
    root = tmp_path / 'checkout'
    root.mkdir()
    archive = subprocess.check_output(['git', 'archive', 'HEAD'], cwd=ROOT)
    with tarfile.open(fileobj=BytesIO(archive)) as source:
        source.extractall(root, filter='data')
    (root / 'scripts/corpus_check.py').write_text(CHECKER_SOURCE)
    (root / 'scripts/corpus_recognition.py').write_text(RECOGNITION_SOURCE)
    if change_during_run:
        product = root / 'scripts/corpus_product.py'
        product.write_text(product.read_text() + '''
_original_observe = observe
def observe(*args, **kwargs):
    result = _original_observe(*args, **kwargs)
    path = Path(__file__)
    path.write_text(path.read_text() + '\\nSYNTHETIC_RUNTIME_CHANGE = True\\n')
    return result
''')
    if change_corpus_during_run:
        product = root / 'scripts/corpus_product.py'
        product.write_text(product.read_text() + f'''
_original_observe = observe
def observe(root, *args, **kwargs):
    result = _original_observe(root, *args, **kwargs)
    path = root / {change_corpus_during_run!r}
    path.write_bytes(path.read_bytes() + b' ')
    return result
''')
    for command in (
        ['init', '-q'], ['config', 'user.name', 'Synthetic Corpus'],
        ['config', 'user.email', 'synthetic@example.invalid'],
        ['add', '.'], ['commit', '-qm', 'Synthetic receipt baseline'],
    ):
        subprocess.run(['git', *command], cwd=root, check=True, capture_output=True)
    return root


def run_receipt(root, report, *, corpus=None):
    return subprocess.run([sys.executable, str(root / 'scripts/corpus_check.py'),
                           '--report', str(report), *(['--corpus', str(corpus)] if corpus else [])], cwd=root, capture_output=True, text=True,
                          env={**os.environ, 'PYTHONPATH': str(root / 'src')}, timeout=90)


@pytest.mark.parametrize('mutation', ['clean', 'tracked', 'untracked'])
def test_receipt_identifies_the_executed_working_tree(tmp_path, mutation):
    root = checkout(tmp_path)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    script = root / 'scripts/corpus_check.py'
    extra = root / 'scripts/synthetic_extra.py'
    if mutation == 'tracked':
        script.write_text(script.read_text() + '\nSYNTHETIC_WORKTREE_CHANGE = True\n')
    elif mutation == 'untracked':
        extra.write_text('SYNTHETIC_UNTRACKED_INPUT = True\n')
    report = tmp_path / 'receipt.json'
    result = run_receipt(root, report)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(report.read_text())
    assert receipt['passed'] is True
    if mutation == 'clean':
        assert receipt['head'] == head
    else:
        assert receipt['head'] is None
        basis = receipt['code_basis']
        assert basis['base_head'] == head and basis['dirty'] is True
        assert basis['files_sha256']['scripts/corpus_check.py'] == hashlib.sha256(script.read_bytes()).hexdigest()
        if mutation == 'untracked':
            assert basis['files_sha256']['scripts/synthetic_extra.py'] == hashlib.sha256(extra.read_bytes()).hexdigest()
        digest = hashlib.sha256(json.dumps(basis['files_sha256'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        assert basis['implementation_sha256'] == digest


def test_receipt_refuses_code_changes_during_observation(tmp_path):
    root = checkout(tmp_path, change_during_run=True)
    report = tmp_path / 'receipt.json'
    result = run_receipt(root, report)
    assert result.returncode == 1
    assert not report.exists()
    assert 'Repository code changed during corpus observation' in result.stderr


@pytest.mark.parametrize('location', ['external', 'ignored'])
@pytest.mark.parametrize('changed_input', [None, 'manifest.json', 'expected.json', 'media/bicycle.pdf'])
def test_receipt_refuses_corpus_input_changes_outside_repository_inventory(tmp_path, location, changed_input):
    root = checkout(tmp_path, change_corpus_during_run=changed_input)
    corpus = tmp_path / 'external-corpus' if location == 'external' else root / '.corpus-receipt-input'
    if location == 'ignored':
        (root / '.git/info/exclude').write_text('.corpus-receipt-input/\n')
    shutil.copytree(root / 'benchmarks/corpus/v1', corpus)
    report = tmp_path / 'receipt.json'
    result = run_receipt(root, report, corpus=corpus)
    if changed_input is None:
        assert result.returncode == 0, result.stderr
        receipt = json.loads(report.read_text())
        assert receipt['passed'] is True
        for name in ('manifest.json', 'expected.json', 'media/bicycle.pdf'):
            assert receipt['corpus_files_sha256'][name] == hashlib.sha256((corpus / name).read_bytes()).hexdigest()
        return
    assert result.returncode == 1
    assert not report.exists()
    assert 'Corpus inputs changed during observation' in result.stderr
