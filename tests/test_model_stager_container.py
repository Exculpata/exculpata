"""Opt-in cache-boundary regression against a locally built stager image."""

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
IMAGE = os.environ.get(f"{LEGACY_ENV_PREFIX}STAGER_TEST_IMAGE")


@pytest.mark.skipif(not IMAGE, reason=f"Set {LEGACY_ENV_PREFIX}STAGER_TEST_IMAGE to an already built model-stager image")
def test_hub_auxiliary_cache_is_writable_inside_model_mount(tmp_path):
    environment = os.environ.copy()
    compose = ROOT / 'compose.yaml'
    for key in set(re.findall(r'\$\{([A-Z_]+):\?', compose.read_text())):
        environment[key] = str(tmp_path / key.lower())
    config_root = Path(environment[f"{LEGACY_ENV_PREFIX}CONFIG_ROOT"])
    config_root.mkdir()
    for filename in (f"{LEGACY_STEM}.env", 'transcription.env'):
        (config_root / filename).touch()
    config = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', str(compose), '--profile', 'tools',
        'config', '--format', 'json'], env=environment, text=True))
    service = config['services']['model-stager']
    assert service['read_only'] is True
    model_root = tmp_path / 'models'
    model_root.mkdir(mode=0o700)
    name = f"{LEGACY_STEM}-cache-probe-" + uuid.uuid4().hex
    command = ['docker', 'run', '--rm', '--name', name, '--pull', 'never',
        '--label', 'com.docker.compose.project=' + os.environ.get(f"{LEGACY_ENV_PREFIX}TEST_PROJECT", name),
        '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
        '--cpus', '0.5', '--memory', '256m', '--memory-swap', '256m', '--pids-limit', '64',
        '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m',
        '--mount', f'type=bind,src={model_root},dst=/models']
    for key, value in service.get('environment', {}).items():
        command.extend(['--env', f'{key}={value}'])
    probe = '''
import json
from pathlib import Path
from huggingface_hub.constants import HF_HUB_CACHE, HF_XET_CACHE
from huggingface_hub.utils._runtime import is_xet_available
assert not is_xet_available(), 'Reference staging must use bounded HTTP transport'
root = Path('/models').resolve()
for value in (HF_HUB_CACHE, HF_XET_CACHE):
    path = Path(value)
    assert path.resolve().is_relative_to(root), 'Auxiliary cache escapes the model mount'
    path.mkdir(parents=True, exist_ok=True)
    target = path / 'synthetic-cache-probe'
    target.write_bytes(b'synthetic cache probe')
    assert target.read_bytes() == b'synthetic cache probe'
    target.unlink()
print(json.dumps({'hub_cache_writable': True, 'xet_cache_writable': True, 'xet_transport_enabled': False}))
'''
    try:
        result = subprocess.run([*command, '--entrypoint', 'python', IMAGE, '-c', probe],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {'hub_cache_writable': True, 'xet_cache_writable': True, 'xet_transport_enabled': False}
    finally:
        # The unpredictable name belongs only to the synthetic container above.
        subprocess.run(['docker', 'rm', '--force', name], capture_output=True, check=False)
