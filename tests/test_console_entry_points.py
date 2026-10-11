"""Installed command compatibility, using help and synthetic delegation only."""
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from case_intelligence import cli

ROOT = Path(__file__).resolve().parents[1]
ALIASES = {
    'recordbench': 'exculpata',
    'recordbench-workbench': 'exculpata-workbench',
    'case-intelligence-workbench': 'exculpata-workbench',
    'recordbench-retrieval-worker': 'exculpata-retrieval-worker',
    'case-review-bench': 'python -m case_intelligence.review_bench',
}
COMMANDS = [*ALIASES, 'exculpata', 'exculpata-workbench',
            'exculpata-retrieval-worker', 'transcription-v2']


def test_distribution_names_and_shared_alias_targets():
    app = distribution('exculpata')
    transcription = distribution('exculpata-transcription-v2')
    entries = {entry.name: entry.value for entry in app.entry_points}
    assert set(entries) == set(COMMANDS) - {'transcription-v2'}
    for old, new in ALIASES.items():
        if old != 'case-review-bench':
            assert entries[old] == entries[new]
    assert {entry.name: entry.value for entry in transcription.entry_points} == {
        'transcription-v2': 'transcription_v2.cli:main'}


@pytest.mark.parametrize('command', COMMANDS)
def test_installed_command_help(command):
    executable = Path(sys.executable).parent / command
    result = subprocess.run([str(executable), '--help'], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout.lower()
    if command in ALIASES:
        assert result.stderr.splitlines() == [
            f'Deprecation notice: {command} is deprecated; use {ALIASES[command]}.']
    else:
        assert result.stderr == ''


@pytest.mark.parametrize('command,wrapper,module', [
    ('recordbench', cli.admin, 'admin_cli'),
    ('recordbench-workbench', cli.workbench, 'workbench'),
    ('case-intelligence-workbench', cli.workbench, 'workbench'),
    ('recordbench-retrieval-worker', cli.retrieval_worker, 'retrieval_worker'),
    ('case-review-bench', cli.review_bench, 'review_bench'),
    ('exculpata', cli.admin, 'admin_cli'),
    ('exculpata-workbench', cli.workbench, 'workbench'),
    ('exculpata-retrieval-worker', cli.retrieval_worker, 'retrieval_worker'),
])
@pytest.mark.parametrize('suffix', ['', '.exe'])
def test_shim_preserves_arguments_return_and_failure(monkeypatch, capsys, command, wrapper, module, suffix):
    arguments = [command + suffix, 'synthetic-option', 'synthetic-value']
    monkeypatch.setattr(sys, 'argv', arguments)
    calls = []
    def main():
        calls.append(list(sys.argv))
        return 17
    def load(name):
        assert name == 'case_intelligence.' + module
        return SimpleNamespace(main=main)
    monkeypatch.setattr(cli, 'import_module', load)
    assert wrapper() == 17
    assert calls == [arguments]
    output = capsys.readouterr()
    assert output.out == ''
    assert len(output.err.splitlines()) == (1 if command in ALIASES else 0)
    def fail():
        raise SystemExit(23)
    main = fail
    with pytest.raises(SystemExit) as error:
        wrapper()
    assert error.value.code == 23


@pytest.mark.parametrize('prefix', ['EXCULPATA', 'RECORDBENCH'])
def test_compose_package_entry_points_exist(tmp_path, prefix):
    compose = ROOT / 'compose.yaml'
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('EXCULPATA_', 'RECORDBENCH_', 'COMPOSE_'))}
    for name in ('CONFIG_ROOT', 'SECRETS_ROOT', 'RUNTIME_ROOT', 'STORAGE_ROOT',
                 'TRANSCRIPTION_ROOT', 'STATE_ROOT', 'MODEL_ROOT', 'TLS_CERT', 'TLS_KEY'):
        environment[f'{prefix}_{name}'] = str(tmp_path / name.lower())
    config_root = Path(environment[f'{prefix}_CONFIG_ROOT'])
    config_root.mkdir()
    for filename in ('recordbench.env', 'transcription.env'):
        (config_root / filename).touch()
    config = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', str(compose), '--profile', '*',
        'config', '--format', 'json'], env=environment, text=True))
    services = config['services']
    assert services['account-admin']['entrypoint'] == ['recordbench']
    assert services['transcription-cleanup']['command'][0] == 'transcription-v2'
    installed = {entry.name for name in ('exculpata', 'exculpata-transcription-v2')
                 for entry in distribution(name).entry_points}
    referenced = set()
    for service in services.values():
        for key in ('entrypoint', 'command'):
            tokens = service.get(key) or []
            if isinstance(tokens, str):
                tokens = [tokens]
            for token in tokens:
                referenced.update(re.findall(
                    r'\b(?:recordbench(?:-workbench|-retrieval-worker)?|'
                    r'exculpata(?:-workbench|-retrieval-worker)?|transcription-v2)\b', token))
    assert referenced == {'recordbench', 'transcription-v2'}
    assert referenced <= installed
    for dockerfile in (ROOT / 'Dockerfile', ROOT / 'services/transcription/Dockerfile'):
        for line in dockerfile.read_text().splitlines():
            if line.startswith('CMD ['):
                command = json.loads(line[4:])[0]
                if command != 'streamlit':
                    assert command in installed
