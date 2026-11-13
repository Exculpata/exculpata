"""Preflight and the single table of local execution environment differences."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tomllib

from scripts.ci_parity_expr import Unmapped


# Keep local overrides together. Workflow env still supplies suite-specific limits.
ENVIRONMENT = {
    'remove': ('PYTEST_ADDOPTS', 'CASE_REVIEW_POSTGRES_TEST_DSN'),
    'set': {'CI': 'true'},
    'worker_variable': 'PYTEST_XDIST_AUTO_NUM_WORKERS',
    'worker_cap': 4,
    'deltas': (
        'HEAD is tested, not GitHub\'s integration-candidate merge commit',
        'runner image and installed native tool versions are not reproduced',
        'existing venv is a shared dependency superset; bootstrap is NOT RUN',
        'Postgres/pgvector services, Docker, browsers and model downloads are not provisioned',
        'network installs/downloads and hosted artifacts/cache are NOT RUN',
        'cells run serially; hosted parallelism, scheduling and timing are not reproduced',
        'only pull_request is supported; other events are NOT RUN',
    ),
}
PROBE = '''import importlib.metadata, json, sys
print(json.dumps({"python": list(sys.version_info[:3]), "packages": {
    d.metadata["Name"].lower().replace("_", "-"): d.version
    for d in importlib.metadata.distributions() if d.metadata["Name"]
}}))
'''


class Refused(ValueError):
    """Missing prerequisites or unclean/unavailable checkout (exit 2)."""


def version_tuple(value):
    if not re.fullmatch(r'\d+(?:\.\d+)*', value):
        raise Unmapped(f'unsupported dependency version {value!r}; extend ci_parity_environment preflight')
    return tuple(int(part) for part in value.split('.'))


def satisfies(installed, specification):
    try:
        actual = version_tuple(installed)
    except Unmapped:
        return False
    comparisons = []
    for clause in specification.split(','):
        match = re.fullmatch(r'(==|>=|<=|>|<|!=)(\d+(?:\.\d+)*)', clause.strip())
        if not match:
            raise Unmapped(f'unsupported dependency constraint {clause!r}; extend ci_parity_environment preflight')
        op, wanted = match.groups()
        expected = version_tuple(wanted)
        width = max(len(actual), len(expected))
        left = actual + (0,) * (width - len(actual))
        right = expected + (0,) * (width - len(expected))
        comparisons.append({'==': left == right, '!=': left != right, '>=': left >= right,
                            '<=': left <= right, '>': left > right, '<': left < right}[op])
    return all(comparisons)


def preflight(root, venv, versions, installed=None):
    python = venv / 'bin' / 'python'
    if not python.is_file():
        raise Refused(f'missing venv Python: {python}')
    try:
        if installed is None:
            result = subprocess.run([str(python), '-I', '-c', PROBE], check=True,
                                    capture_output=True, text=True, timeout=30)
            installed = json.loads(result.stdout)
        project_path = root / 'pyproject.toml'
        data = tomllib.loads(project_path.read_text())
        requirements = data['project']['optional-dependencies']['dev']
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise Refused(f'cannot inspect venv/dev extras: {error}') from error
    if not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements):
        raise Unmapped('dev extras must be requirement strings; extend ci_parity_environment preflight')
    missing = []
    for requirement in requirements:
        match = re.fullmatch(r'([A-Za-z0-9_-]+)(.+)', requirement)
        if not match:
            raise Unmapped(f'unsupported dev requirement {requirement!r}; extend ci_parity_environment preflight')
        name, constraint = match.groups()
        # Validate the constraint even when the package is missing.
        satisfies('0', constraint)
        actual = installed['packages'].get(name.lower().replace('_', '-'))
        if actual is None or not satisfies(actual, constraint):
            missing.append(f'{requirement} (installed: {actual or "missing"})')
    for version in sorted(versions):
        expected = version_tuple(version)
        if tuple(installed['python'][:len(expected)]) != expected:
            missing.append(f'Python=={version} (installed: {".".join(map(str, installed["python"]))})')
    # python3 is used by real steps too; it must resolve to this same environment.
    for executable in ('python3',):
        path = venv / 'bin' / executable
        if not path.is_file() or path.resolve() != python.resolve():
            missing.append(f'{executable}: expected same interpreter as {python}')
    if missing:
        raise Refused('preflight missing/incompatible requirements: ' + '; '.join(missing))
    return installed


def local_environment(tree, temporary, venv, head, branch, event_path):
    env = dict(os.environ)
    for key in ENVIRONMENT['remove']:
        env.pop(key, None)
    env.update(ENVIRONMENT['set'])
    env.update({
        'PATH': str(venv / 'bin') + os.pathsep + env.get('PATH', ''),
        'VIRTUAL_ENV': str(venv), 'PYTHON': str(venv / 'bin' / 'python'),
        'PYTHONPATH': os.pathsep.join((str(tree / 'src'), str(tree / 'services' / 'transcription' / 'src'))),
        'RUNNER_TEMP': str(temporary), 'TMPDIR': str(temporary),
        'GITHUB_WORKSPACE': str(tree), 'GITHUB_SHA': head,
        'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_EVENT_PATH': str(event_path),
        'GITHUB_REF': 'refs/pull/0/merge', 'GITHUB_HEAD_REF': branch,
        'EXPECTED_INTEGRATION_SHA': head, 'EXPECTED_PUBLICATION_SHA': head,
        'EXPECTED_BASE_SHA': '', 'PUBLICATION_REF': 'refs/heads/' + branch,
    })
    return overrides(env)


def overrides(env):
    env = dict(env)
    variable = ENVIRONMENT['worker_variable']
    value = env.get(variable, str(ENVIRONMENT['worker_cap']))
    try:
        env[variable] = str(min(int(value), ENVIRONMENT['worker_cap']))
        if int(env[variable]) < 1:
            raise ValueError(value)
    except ValueError as error:
        raise Unmapped(f'invalid {variable}: {value!r}; see ci_parity_environment') from error
    return env


def describe():
    return [*ENVIRONMENT['deltas'],
            f'local platform: {platform.system()} {platform.release()} {platform.machine()}',
            'inherited environment removals: ' + ', '.join(ENVIRONMENT['remove']),
            f'{ENVIRONMENT["worker_variable"]}: workflow value capped at {ENVIRONMENT["worker_cap"]}; command -n auto unchanged',
            'only command path substitution: .venv/bin/ -> <venv>/bin/']
