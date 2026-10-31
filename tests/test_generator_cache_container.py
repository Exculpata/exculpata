"""Opt-in native cache check using the pinned, already available generator image."""

from case_intelligence.legacy_names import (
    LEGACY_ENV_PREFIX,
    LEGACY_STEM,
)
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _either(suffix: str, default: str | None = None) -> str | None:
    """Prefer the current probe variable and accept the previous name."""
    return os.environ.get("EXCULPATA_" + suffix) or os.environ.get(LEGACY_ENV_PREFIX + suffix) or default


@pytest.mark.skipif(_either("GENERATOR_CONTAINER_TEST") != "1",
                   reason="Enable explicitly with the pinned generator image already available")
def test_generator_cache_can_load_native_compiler_output(tmp_path):
    compose = ROOT / 'compose.yaml'
    environment = os.environ.copy()
    for key in set(re.findall(r'\$\{([A-Z_]+):\?', compose.read_text())):
        environment[key] = str(tmp_path / key.lower())
    config_root = Path(environment[f"{LEGACY_ENV_PREFIX}CONFIG_ROOT"])
    config_root.mkdir()
    for filename in (f"{LEGACY_STEM}.env", 'transcription.env'):
        (config_root / filename).touch()
    config = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', str(compose), '--profile', 'ai',
        'config', '--format', 'json'], env=environment, text=True))
    service = config['services']['generator']
    name = f"{LEGACY_STEM}-native-cache-probe-" + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '--pull', 'never',
        '--label', 'com.docker.compose.project=' + _either("TEST_PROJECT", name),
        '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--user', service['user'],
        '--security-opt', 'no-new-privileges', '--cpus', '0.25',
        '--memory', '256m', '--memory-swap', '256m', '--pids-limit', '64']
    for mount in service['tmpfs']:
        command.extend(['--tmpfs', mount])
    command.extend(['--env', 'VLLM_CACHE_ROOT=' + service['environment']['VLLM_CACHE_ROOT']])
    probe = '''
import _json, ctypes, json, os, shutil
from pathlib import Path
root = Path(os.environ['VLLM_CACHE_ROOT'])
assert root == Path('/tmp/vllm-cache')
artifact = root / 'synthetic-native-probe.so'
shutil.copyfile(_json.__file__, artifact)
ctypes.CDLL(str(artifact))
artifact.unlink()
print(json.dumps({'native_cache_load': True}))
'''
    try:
        result = subprocess.run([*command, '--entrypoint', 'python3', service['image'], '-c', probe],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {'native_cache_load': True}
    finally:
        subprocess.run(['docker', 'rm', '--force', name], capture_output=True, check=False)


@pytest.mark.parametrize('service_name,suffix', [
    ('retrieval', 'RETRIEVAL_TEST_IMAGE'),
    ('transcription-worker', 'WORKER_TEST_IMAGE'),
])
def test_native_runtime_can_compile_and_load_local_launcher(tmp_path, service_name, suffix):
    image = _either(suffix)
    if not image:
        pytest.skip('Set EXCULPATA_' + suffix + ' or the previous name to an already built image')
    compose = ROOT / 'compose.yaml'
    environment = os.environ.copy()
    for key in set(re.findall(r'\$\{([A-Z_]+):\?', compose.read_text())):
        environment[key] = str(tmp_path / key.lower())
    config_root = Path(environment[f"{LEGACY_ENV_PREFIX}CONFIG_ROOT"])
    config_root.mkdir()
    for filename in (f"{LEGACY_STEM}.env", 'transcription.env'):
        (config_root / filename).touch()
    config = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', str(compose), '--profile', 'ai',
        '--profile', 'transcription', 'config', '--format', 'json'], env=environment, text=True))
    service = config['services'][service_name]
    name = f"{LEGACY_STEM}-compiler-probe-" + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '--pull', 'never',
        '--label', 'com.docker.compose.project=' + _either("TEST_PROJECT", name),
        '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
        '--cpus', '0.5', '--memory', '512m', '--memory-swap', '512m', '--pids-limit', '64']
    for mount in service['tmpfs']:
        command.extend(['--tmpfs', mount])
    for key in ('TMPDIR', 'TRITON_CACHE_DIR'):
        command.extend(['--env', key + '=' + service['environment'][key]])
    probe = f"""
import ctypes, os, subprocess, tempfile
from pathlib import Path
root = Path(os.environ['TMPDIR'])
assert root.name in {{'retrieval-native', 'transcription-native'}}
assert Path(os.environ['TRITON_CACHE_DIR']).is_relative_to(root)
with tempfile.TemporaryDirectory() as directory:
    artifact = Path(directory) / 'synthetic-native-probe.so'
    assert artifact.is_relative_to(root)
    subprocess.run(['gcc','-shared','-fPIC','-x','c','-o',str(artifact),'-'],
        input=b'int {LEGACY_STEM}_value(void) {{ return 42; }}', check=True)
    assert ctypes.CDLL(str(artifact)).{LEGACY_STEM}_value() == 42
print('native launcher compilation and loading verified')
"""
    try:
        result = subprocess.run([*command, '--entrypoint', 'python3', image, '-c', probe],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert 'native launcher compilation and loading verified' in result.stdout
    finally:
        subprocess.run(['docker', 'rm', '--force', name], capture_output=True, check=False)


def test_worker_packaged_vad_loads_with_read_only_staged_cache(tmp_path):
    image = _either("WORKER_TEST_IMAGE")
    if not image:
        pytest.skip("Set EXCULPATA_WORKER_TEST_IMAGE or the previous name to an already built image")
    compose = ROOT / 'compose.yaml'
    environment = os.environ.copy()
    for key in set(re.findall(r'\$\{([A-Z_]+):\?', compose.read_text())):
        environment[key] = str(tmp_path / key.lower())
    config_root = Path(environment[f"{LEGACY_ENV_PREFIX}CONFIG_ROOT"])
    config_root.mkdir()
    for filename in (f"{LEGACY_STEM}.env", 'transcription.env'):
        (config_root / filename).touch()
    config = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', str(compose), '--profile', 'transcription',
        'config', '--format', 'json'], env=environment, text=True))
    service = config['services']['transcription-worker']
    model_root = tmp_path / 'models'
    (model_root / 'huggingface/hub').mkdir(parents=True)
    name = f"{LEGACY_STEM}-vad-cache-probe-" + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '--pull', 'never',
        '--label', 'com.docker.compose.project=' + _either("TEST_PROJECT", name),
        '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
        '--cpus', '1', '--memory', '3g', '--memory-swap', '3g', '--pids-limit', '128',
        '--env', 'OMP_NUM_THREADS=1', '--env', 'OPENBLAS_NUM_THREADS=1',
        '--mount', f'type=bind,src={model_root},dst=/models,readonly']
    for mount in service['tmpfs']:
        command.extend(['--tmpfs', mount])
    for key in ('HF_HOME','TORCH_HOME','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','TMPDIR','TRITON_CACHE_DIR'):
        command.extend(['--env', key + '=' + service['environment'][key]])
    probe = '''
import torch
from whisperx.vads.pyannote import load_vad_model
load_vad_model(torch.device('cpu'))
print('packaged VAD loaded without modifying staged model cache')
'''
    try:
        result = subprocess.run([*command, '--entrypoint', 'python3', image, '-c', probe],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert 'packaged VAD loaded without modifying staged model cache' in result.stdout
        assert sorted(str(path.relative_to(model_root)) for path in model_root.rglob('*')) == ['huggingface','huggingface/hub']
    finally:
        subprocess.run(['docker', 'rm', '--force', name], capture_output=True, check=False)
