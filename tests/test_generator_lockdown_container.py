"""Opt-in, model-free hardening evidence using only the already staged image.

EXCULPATA_GENERATOR_CONTAINER_TEST_SUBNET selects a strict IPv4 subnet for creation;
EXCULPATA_GENERATOR_CONTAINER_TEST_NETWORK reuses an empty internal network.
Set at most one. With neither set, network commands and cleanup are unchanged.
"""
import ipaddress
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


def network_options(default_network):
    subnet = os.environ.get('EXCULPATA_GENERATOR_CONTAINER_TEST_SUBNET')
    network = os.environ.get('EXCULPATA_GENERATOR_CONTAINER_TEST_NETWORK')
    if subnet is not None and network is not None:
        pytest.fail('EXCULPATA_GENERATOR_CONTAINER_TEST_SUBNET and '
                    'EXCULPATA_GENERATOR_CONTAINER_TEST_NETWORK cannot both be set')
    if subnet is not None:
        try:
            parsed = ipaddress.ip_network(subnet, strict=True)
        except ValueError:
            pytest.skip('EXCULPATA_GENERATOR_CONTAINER_TEST_SUBNET must be a strict IPv4 network')
        if parsed.version != 4:
            pytest.skip('EXCULPATA_GENERATOR_CONTAINER_TEST_SUBNET must be a strict IPv4 network')
    return (default_network if network is None else network), subnet, network is not None


def prepare_network(network, subnet, reuse):
    if reuse:
        try:
            output = subprocess.check_output(['docker', 'network', 'inspect', network],
                                             text=True, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, OSError):
            pytest.fail('Could not inspect the requested existing network; it must exist')
        try:
            inspected = json.loads(output)
        except ValueError:
            pytest.fail('Invalid existing network inspect response')
        if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
            pytest.fail('Invalid existing network inspect response')
        if inspected[0].get('Internal') is not True:
            pytest.fail('The existing network must be internal')
        if inspected[0].get('Containers') != {}:
            pytest.fail('The existing network must have no attached containers')
        return False
    command = ['docker', 'network', 'create', '--internal']
    if subnet is not None:
        command.extend(['--subnet', subnet])
    subprocess.run([*command, network], check=True, capture_output=True)
    return True


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
    network, subnet, reuse = network_options(network)
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
        prepare_network(network, subnet, reuse)
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
        if not reuse:
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True, check=False)
