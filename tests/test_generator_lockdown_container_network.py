"""Docker-free checks for the opt-in L2 network selection and cleanup."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call

import pytest

from tests import test_generator_lockdown_container as container

SUBNET = 'EXCULPATA_GENERATOR_CONTAINER_TEST_SUBNET'
NETWORK = 'EXCULPATA_GENERATOR_CONTAINER_TEST_NETWORK'


@pytest.fixture(autouse=True)
def network_environment(monkeypatch):
    monkeypatch.delenv(SUBNET, raising=False)
    monkeypatch.delenv(NETWORK, raising=False)


@pytest.fixture
def docker(monkeypatch):
    mock = Mock()
    mock.run.return_value = subprocess.CompletedProcess([], 0, '')
    monkeypatch.setattr(container.subprocess, 'run', mock.run)
    monkeypatch.setattr(container.subprocess, 'check_output', mock.check_output)
    return mock


@pytest.mark.parametrize('subnet', [None, '192.0.2.0/24'])
def test_network_create_arguments(monkeypatch, docker, subnet):
    if subnet is not None:
        monkeypatch.setenv(SUBNET, subnet)
    network, selected_subnet, reuse = container.network_options('synthetic-created')
    assert (network, selected_subnet, reuse) == ('synthetic-created', subnet, False)
    assert container.prepare_network(network, selected_subnet, reuse) is True
    args = ['docker', 'network', 'create', '--internal']
    if subnet is not None:
        args.extend(['--subnet', subnet])
    docker.run.assert_called_once_with([*args, network], check=True, capture_output=True)
    docker.check_output.assert_not_called()


@pytest.mark.parametrize('subnet', ['not-a-cidr', '192.0.2.1/24', '2001:db8::/64', ''])
def test_invalid_subnet_skips_before_docker(monkeypatch, docker, subnet):
    monkeypatch.setenv(SUBNET, subnet)
    with pytest.raises(pytest.skip.Exception, match='strict IPv4 network'):
        container.network_options('synthetic-created')
    assert docker.mock_calls == []


@pytest.mark.parametrize('subnet,network', [('192.0.2.0/24', 'synthetic-reused'), ('', '')])
def test_both_settings_fail_before_docker(monkeypatch, docker, subnet, network):
    monkeypatch.setenv(SUBNET, subnet)
    monkeypatch.setenv(NETWORK, network)
    with pytest.raises(pytest.fail.Exception, match='cannot both be set'):
        container.network_options('synthetic-created')
    assert docker.mock_calls == []


@pytest.mark.parametrize('metadata,reason', [
    ([{'Internal': False, 'Containers': {}}], 'must be internal'),
    ([{'Internal': True, 'Containers': {'synthetic-id': {}}}], 'attached containers'),
    ([], 'inspect response'),
    ([{'Internal': True}], 'attached containers'),
])
def test_reuse_refuses_unsafe_network(monkeypatch, docker, metadata, reason):
    monkeypatch.setenv(NETWORK, 'synthetic-reused')
    docker.check_output.return_value = json.dumps(metadata)
    options = container.network_options('synthetic-created')
    assert options == ('synthetic-reused', None, True)
    with pytest.raises(pytest.fail.Exception, match=reason):
        container.prepare_network(*options)
    docker.run.assert_not_called()


@pytest.mark.parametrize('error', [
    subprocess.CalledProcessError(1, ['docker', 'network', 'inspect']),
    OSError('synthetic inspect failure'),
])
def test_reuse_refuses_inspect_failure(monkeypatch, docker, error):
    monkeypatch.setenv(NETWORK, 'synthetic-reused')
    docker.check_output.side_effect = error
    with pytest.raises(pytest.fail.Exception, match='Could not inspect'):
        container.prepare_network(*container.network_options('synthetic-created'))
    docker.run.assert_not_called()


@pytest.mark.parametrize('output', ['not-json', '{}', '[null]', '[{}, {}]'])
def test_reuse_refuses_malformed_inspect(docker, output):
    docker.check_output.return_value = output
    with pytest.raises(pytest.fail.Exception, match='inspect response'):
        container.prepare_network('synthetic-reused', None, True)
    docker.run.assert_not_called()


@pytest.mark.parametrize('reuse', [False, True])
@pytest.mark.parametrize('probe_fails', [False, True])
def test_container_network_lifecycle(monkeypatch, tmp_path, docker, reuse, probe_fails):
    if reuse:
        monkeypatch.setenv(NETWORK, 'synthetic-reused')
    monkeypatch.setattr(container.uuid, 'uuid4', lambda: SimpleNamespace(hex='synthetic'))
    service = {'dns': ['127.0.0.1'], 'dns_search': ['.'], 'user': '1000:1000',
               'pids_limit': 4096, 'read_only': True, 'cap_drop': ['ALL'],
               'security_opt': ['no-new-privileges:true'], 'tmpfs': [],
               'environment': {}, 'image': 'synthetic-image'}
    outputs = [json.dumps({'services': {'generator': service}})]
    if reuse:
        outputs.append(json.dumps([{'Internal': True, 'Containers': {}}]))
    outputs.append('healthy')
    docker.check_output.side_effect = outputs
    receipt = json.dumps({'status': 'passed', 'peer_control': True, 'hardening': {'synthetic': True}})
    docker.run.return_value = subprocess.CompletedProcess([], int(probe_fails), receipt, '')
    if probe_fails:
        with pytest.raises(AssertionError):
            container.test_generator_lockdown_in_pinned_image(tmp_path)
    else:
        container.test_generator_lockdown_in_pinned_image(tmp_path)
    name = 'synthetic-generator-lockdown-synthetic'
    network = 'synthetic-reused' if reuse else name + '-network'
    runs = docker.run.call_args_list
    if reuse:
        assert len(runs) == 3
        docker.check_output.assert_any_call(
            ['docker', 'network', 'inspect', network], text=True, stderr=subprocess.PIPE)
    else:
        assert len(runs) == 5
        assert runs.pop(0) == call(['docker', 'network', 'create', '--internal', network],
                                   check=True, capture_output=True)
        assert runs.pop() == call(['docker', 'network', 'rm', network],
                                  capture_output=True, check=False)
    assert runs[-1] == call(['docker', 'rm', '--force', name, name + '-peer'],
                            capture_output=True, check=False)
    for run in runs[:2]:
        command = run.args[0]
        assert command[:6] == ['docker', 'run', '--pull', 'never', '--network', network]
    assert not any(c.args[0][:3] == ['docker', 'network', 'rm'] for c in runs)
    command = ['docker', 'run', '--pull', 'never', '--network', network,
               '--dns', '127.0.0.1', '--dns-search', '.', '--user', '1000:1000',
               '--pids-limit', '4096', '--cpus', '1', '--memory', '2g', '--memory-swap', '2g',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true']
    expected = [call.check_output([
        'docker', 'compose', '-f', str(container.ROOT / 'compose.yaml'), '--profile', 'ai',
        'config', '--format', 'json'], env=ANY, text=True)]
    if reuse:
        expected.append(call.check_output(['docker', 'network', 'inspect', network],
                                          text=True, stderr=subprocess.PIPE))
    else:
        expected.append(call.run(['docker', 'network', 'create', '--internal', network],
                                 check=True, capture_output=True))
    expected.extend([
        call.run([*command, '-d', '--name', name + '-peer', '--network-alias', 'retrieval',
                  '--health-cmd', "python3 -c \"import socket; socket.create_connection(('127.0.0.1',8080),2).close()\"",
                  '--health-interval', '1s', '--health-retries', '20',
                  '--entrypoint', 'python3', 'synthetic-image', '-m', 'http.server', '8080'],
                 check=True, capture_output=True, timeout=120),
        call.check_output(['docker', 'inspect', '--format', '{{.State.Health.Status}}', name + '-peer'], text=True),
        call.run([*command, '--rm', '--name', name,
                  '--mount', f'type=bind,src={tmp_path / "models"},dst=/models,readonly',
                  '--mount', f'type=bind,src={container.ROOT / "tests/network_smoke/generator_lockdown_probe.py"},dst=/probe.py,readonly',
                  '--entrypoint', 'python3', 'synthetic-image', '/probe.py',
                  '--peer', 'retrieval', '--hardening'], capture_output=True, text=True, timeout=120),
        call.run(['docker', 'rm', '--force', name, name + '-peer'], capture_output=True, check=False),
    ])
    if not reuse:
        expected.append(call.run(['docker', 'network', 'rm', network], capture_output=True, check=False))
    assert docker.mock_calls == expected
