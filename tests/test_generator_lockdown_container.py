"""Opt-in, model-free hardening evidence using only the already staged image."""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get('EXCULPATA_GENERATOR_CONTAINER_TEST') != '1',
                               reason='requires explicitly enabled, already available pinned generator image')


def test_generator_lockdown_in_pinned_image(tmp_path):
    environment = os.environ.copy()
    for name in ('CONFIG', 'SECRETS', 'RUNTIME', 'STORAGE', 'STATE', 'MODEL', 'TRANSCRIPTION', 'LOCAL_ACCOUNT'):
        environment['EXCULPATA_' + name + '_ROOT'] = str(tmp_path)
    environment.update(EXCULPATA_TLS_CERT=str(tmp_path / 'synthetic.crt'),
                       EXCULPATA_TLS_KEY=str(tmp_path / 'synthetic.key'),
                       EXCULPATA_APP_ENV_FILE='exculpata.env')
    for filename in ('exculpata.env', 'recordbench.env', 'transcription.env'):
        # fixture-write: Empty synthetic Compose environment files for this test only.
        (tmp_path / filename).write_text('')
    service = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', str(ROOT / 'compose.yaml'), '--profile', 'ai',
        'config', '--format', 'json'], env=environment, text=True))['services']['generator']
    name = 'synthetic-generator-lockdown-' + uuid.uuid4().hex
    peer, network = name + '-peer', name + '-network'
    models = tmp_path / 'models'
    models.mkdir(mode=0o755)
    command = ['docker', 'run', '--pull', 'never', '--network', network,
               '--dns', service['dns'][0], '--dns-search', service['dns_search'][0],
               '--user', service['user'], '--pids-limit', str(service['pids_limit']),
               '--cpus', '1', '--memory', '2g', '--memory-swap', '2g']
    assert service['read_only'] is True
    command.append('--read-only')
    for key, flag in (('cap_drop', '--cap-drop'), ('security_opt', '--security-opt'), ('tmpfs', '--tmpfs')):
        for value in service[key]:
            command.extend([flag, value])
    for key, value in service['environment'].items():
        command.extend(['--env', key + '=' + str(value)])
    try:
        subprocess.run(['docker', 'network', 'create', '--internal', network], check=True, capture_output=True)
        subprocess.run([*command, '-d', '--name', peer, '--network-alias', 'retrieval',
                        '--health-cmd', "python3 -c \"import socket; socket.create_connection(('127.0.0.1',8080),2).close()\"",
                        '--health-interval', '1s', '--health-retries', '20',
                        '--entrypoint', 'python3', service['image'], '-m', 'http.server', '8080'],
                       check=True, capture_output=True, timeout=120)
        # The peer must be ready before any denial counts as isolation evidence.
        for _ in range(30):
            health = subprocess.check_output(['docker', 'inspect', '--format', '{{.State.Health.Status}}', peer], text=True)
            if health.strip() == 'healthy':
                break
            time.sleep(1)
        else:
            pytest.fail('synthetic peer did not become healthy')
        result = subprocess.run([*command, '--rm', '--name', name,
            '--mount', f'type=bind,src={models},dst=/models,readonly',
            '--mount', f'type=bind,src={ROOT / "tests/network_smoke/generator_lockdown_probe.py"},dst=/probe.py,readonly',
            '--entrypoint', 'python3', service['image'], '/probe.py',
            '--peer', 'retrieval', '--hardening'], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads(result.stdout)
        assert receipt['status'] == 'passed' and receipt['peer_control'] is True
        assert all(receipt['hardening'].values())
    finally:
        subprocess.run(['docker', 'rm', '--force', name, peer], capture_output=True, check=False)
        subprocess.run(['docker', 'network', 'rm', network], capture_output=True, check=False)
