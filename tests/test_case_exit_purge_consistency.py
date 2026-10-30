"""Keep the bounded model, TLC configuration and runner in step without Java."""

import hashlib
import os
from pathlib import Path
import re
import subprocess

import pytest

MODEL = Path(__file__).resolve().parents[1] / 'formal' / 'case_exit_purge'


def test_config_checks_all_ruled_properties():
    config = (MODEL / 'CaseExitPurge.cfg').read_text()
    sections = re.split(r'\b(SPECIFICATION|CONSTANTS|INVARIANTS?|PROPERT(?:Y|IES))\b', config)
    invariants, properties = set(), set()
    for key, value in zip(sections[1::2], sections[2::2]):
        if key in {'INVARIANT', 'INVARIANTS'}:
            invariants.update(value.split())
        elif key in {'PROPERTY', 'PROPERTIES'}:
            properties.update(value.split())
    assert invariants == {'S1', 'S1b', 'S1c', 'S2', 'S3', 'S4', 'S5', 'S6'}
    assert 'L1' in properties


def test_runner_checks_every_model_mutant():
    model_mutants = set(re.findall(r'Mutant\s*=\s*"([^"]+)"',
        (MODEL / 'CaseExitPurge.tla').read_text()))
    loop = re.search(r'for (?:invariant|mutant) in ([^;]+); do',
        (MODEL / 'check.sh').read_text())
    assert loop is not None, 'Keep the explicit mutant list in check.sh inspectable.'
    runner_mutants = loop.group(1).split()
    assert len(runner_mutants) == len(set(runner_mutants))
    assert set(runner_mutants) == model_mutants
    assert {'S1b_name', 'S1c_mode_flip'} <= model_mutants


@pytest.fixture
def runner_environment(tmp_path):
    java = tmp_path / 'java'
    # fixture-write: Fake the Java version and TLC invocation, never starting a JVM.
    java.write_text('#!/bin/sh\nif [ "$1" = -version ]; then\n'
        '  printf \'%s\\n\' "$SYNTHETIC_JAVA_VERSION" >&2\n'
        'else\n  echo "synthetic TLC invoked"\nfi\n')
    # fixture-write: Make only this synthetic executable runnable.
    java.chmod(0o700)
    jar = tmp_path / 'synthetic.jar'
    # fixture-write: Deliberately not a real jar, for checksum admission tests.
    jar.write_bytes(b'synthetic tool bytes\n')
    env = dict(PATH=f'{tmp_path}:{os.environ["PATH"]}', TMPDIR=str(tmp_path),
        TLA2TOOLS_JAR=str(jar), SYNTHETIC_JAVA_VERSION='openjdk version "21.0.1"')
    return env, hashlib.sha256(jar.read_bytes()).hexdigest()


@pytest.mark.parametrize('version,accepted', [
    ('openjdk version "17.0.1"', False), ('java version "1.8.0_432"', False),
    ('unrecognized version', False), ('openjdk version "21.0.1"', True),
    ('openjdk version "25-ea"', True)])
def test_java_requires_recognizable_major_at_least_21(runner_environment, version, accepted):
    env, digest = runner_environment
    env.update(SYNTHETIC_JAVA_VERSION=version, TLA2TOOLS_SHA256=digest)
    result = subprocess.run(['sh', str(MODEL / 'check.sh')], env=env,
        capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is accepted
    if accepted:
        assert 'synthetic TLC invoked' in result.stdout
    else:
        assert 'Java 21 or newer is required' in result.stderr
        assert 'synthetic TLC invoked' not in result.stdout


@pytest.mark.parametrize('override,accepted', [(None, False), ('0' * 64, False),
    ('skip', True), ('actual', True)])
def test_jar_pin_requires_explicit_override(runner_environment, override, accepted):
    env, digest = runner_environment
    if override is not None:
        env['TLA2TOOLS_SHA256'] = digest if override == 'actual' else override
    result = subprocess.run(['sh', str(MODEL / 'check.sh')], env=env,
        capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is accepted
    if override is not None:
        assert 'Explicit TLA2TOOLS_SHA256 override' in result.stdout + result.stderr
    if accepted:
        assert 'synthetic TLC invoked' in result.stdout
    else:
        assert 'synthetic TLC invoked' not in result.stdout
