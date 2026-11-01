"""Exercise the docs-only workflow in an isolated dependency environment."""

import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
from time import monotonic

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    os.environ.get('EXCULPATA_DOCS_VENV_TEST') != '1',
    reason='set EXCULPATA_DOCS_VENV_TEST=1 to exercise the network-dependent fresh venv',
)
@pytest.mark.skipif(importlib.util.find_spec('venv') is None, reason='venv is unavailable')
def test_docs_only_contracts_with_workflow_dependencies(tmp_path):
    steps = yaml.safe_load((ROOT / '.github/workflows/quality-gates.yml').read_text())[
        'jobs']['application-shards']['steps']
    install = next(step for step in steps if step['name'] ==
                   'Install lightweight documentation test dependencies')
    contracts = next(step for step in steps if step['name'] ==
                     'Run documentation and scope contracts')
    contract_args = shlex.split(contracts['run'])
    assert contract_args[:3] == ['python', '-m', 'pytest']
    environment = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        environment.pop(name, None)
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    environment['PIP_RETRIES'] = '0'
    environment['PIP_TIMEOUT'] = '10'
    environment['PIP_CACHE_DIR'] = str(tmp_path / 'pip-cache')
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    started = monotonic()
    # fixture-write: disposable isolated venv and package files under tmp_path only.
    try:
        created = subprocess.run(
            [sys.executable, '-m', 'venv', str(tmp_path / 'venv')],
            capture_output=True, text=True, timeout=30, env=environment,
        )
    except subprocess.TimeoutExpired:
        pytest.skip('venv creation exceeded 30 seconds')
    if created.returncode:
        pytest.skip('venv creation unavailable; rerun with venv support')
    python = tmp_path / 'venv' / 'bin' / 'python'
    environment['PATH'] = str(python.parent) + os.pathsep + environment.get('PATH', '')
    environment['TMPDIR'] = str(tmp_path)
    unavailable = (
        'ConnectionError', 'Connection refused', 'Network is unreachable',
        'Temporary failure in name resolution', 'ReadTimeoutError',
        'Failed to establish a new connection', '403 Client Error', '403 Forbidden',
        'HTTP error 403',
    )
    for attempt in range(2):
        # fixture-write: workflow installs only into the disposable venv under tmp_path.
        try:
            installed = subprocess.run(
                ['bash', '-e', '-o', 'pipefail', '-c', install['run']],
                capture_output=True, text=True, timeout=60, env=environment, cwd=ROOT,
            )
        except subprocess.TimeoutExpired:
            pytest.skip('docs-only dependency installation exceeded 60 seconds')
        output = installed.stdout + installed.stderr
        if installed.returncode == 0:
            break
        if not any(message in output for message in unavailable):
            pytest.fail(output)
        if attempt == 1:
            pytest.skip('docs-only package index unavailable after one retry; rerun with package access')
    setup_seconds = monotonic() - started
    started = monotonic()
    # fixture-write: contract subprocess fixtures and caches stay under tmp_path.
    result = subprocess.run(
        [str(python), *contract_args[1:], '--basetemp', str(tmp_path / 'contracts'),
         '-o', 'tmp_path_retention_policy=failed', '-p', 'no:cacheprovider'],
        capture_output=True, text=True, timeout=180, env=environment, cwd=ROOT,
    )
    elapsed = monotonic() - started
    print(f'Fresh docs-only environment: setup={setup_seconds:.2f}s contracts={elapsed:.2f}s')
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
