"""Exercise the hosted command, including an abruptly exiting xdist worker."""

import os
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]


def application_command(tmp_path):
    workflow = (ROOT / '.github/workflows/quality-gates.yml').read_text()
    step = workflow.split('      - name: Run application suite\n', 1)[1]
    command = re.search(r'        run: >-\n((?:          .+\n)+)', step)[1]
    command = command.replace('${{ runner.temp }}', str(tmp_path))
    argv = shlex.split(command)
    argv[0] = sys.executable
    argv[argv.index('-n') + 1] = '1'
    argv += ['--basetemp', str(tmp_path / 'pt'), '-o', 'tmp_path_retention_policy=failed']
    return argv


def run_synthetic(tmp_path, source):
    # fixture-write: Synthetic pytest source exercises failure and crash reports.
    (tmp_path / 'test_synthetic.py').write_text(source)
    # fixture-write: Substitute the shard manifest with one synthetic test file.
    (tmp_path / 'application-test-files.txt').write_text('test_synthetic.py\n')
    env = dict(os.environ)
    # Do not inherit a caller's selection, tracing or plugin configuration.
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_PLUGINS', None)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    # Load the distribution and diagnostics plugins explicitly, just as the
    # hosted suite discovers them. Bound this nested synthetic
    # run independently of the outer suite's worker count to avoid oversubscription.
    env['PYTEST_XDIST_AUTO_NUM_WORKERS'] = '1'
    env['PYTHONPATH'] = str(ROOT)
    command = application_command(tmp_path) + ['-p', 'xdist.plugin', '-p', 'tests.worker_diagnostics']
    return subprocess.run(command, cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)


def test_completed_report_preserves_failure_skip_and_following_test(tmp_path):
    result = run_synthetic(tmp_path, '''import pytest

def test_failure():
    print("SYNTHETIC_CAPTURE_MARKER")
    assert 1 == 2

def test_skip():
    pytest.skip("synthetic prerequisite absent")

def test_after_failure():
    pass
''')
    assert result.returncode == 1
    assert re.search(r'FAILED .*test_failure\b', result.stdout)
    assert re.search(r'PASSED .*test_after_failure\b', result.stdout)
    assert 'synthetic prerequisite absent' in result.stdout
    assert 'slowest 25 durations' in result.stdout
    assert 'SYNTHETIC_CAPTURE_MARKER' not in result.stdout
    report = (tmp_path / 'application-results.xml').read_text()
    suite = ET.fromstring(report).find('testsuite')
    assert suite.attrib['tests'] == '3'
    assert suite.attrib['failures'] == '1'
    assert suite.attrib['skipped'] == '1'
    assert 'SYNTHETIC_CAPTURE_MARKER' not in report
    assert suite.findall('.//system-out') == []
    assert suite.findall('.//system-err') == []
    diagnostics = tmp_path / 'application-workers'
    assert (diagnostics / 'runner-start.json').is_file()
    exits = [json.loads(path.read_text()) for path in diagnostics.glob('*-exit.json')]
    assert len(exits) == 1 and not any(row['crashed'] for row in exits)


@pytest.mark.parametrize('phase', ['setup', 'call', 'teardown'])
def test_failed_node_survives_abrupt_exit_without_session_summary(tmp_path, phase):
    fixture = '''import os
import pytest

pytestmark = pytest.mark.xdist_group("synthetic-crash-sequence")

@pytest.fixture
def synthetic_fixture():
    SETUP
    yield
    TEARDOWN

def test_failure(synthetic_fixture):
    CALL

def test_abrupt_exit():
    os._exit(7)
'''.replace('SETUP', 'assert False' if phase == 'setup' else 'pass').replace(
        'TEARDOWN', 'assert False' if phase == 'teardown' else 'pass').replace(
        'CALL', 'assert False' if phase == 'call' else 'pass')
    result = run_synthetic(tmp_path, fixture)
    # The controller survives worker exit 7 and fails the run with a crash
    # diagnostic and a complete report, retaining the preceding failure too.
    assert result.returncode == 1
    outcome = 'FAILED' if phase == 'call' else 'ERROR'
    assert re.search(rf'{outcome} .*test_synthetic.py::test_failure\b', result.stdout)
    assert 'test_synthetic.py::test_abrupt_exit' in result.stdout
    assert 'crashed while running' in result.stdout
    suite = ET.parse(tmp_path / 'application-results.xml').getroot().find('testsuite')
    assert int(suite.attrib['failures']) + int(suite.attrib['errors']) == 2
    assert suite.attrib['skipped'] == '0'
    cases = suite.findall('testcase')
    assert any(case.attrib['name'].startswith('test_failure') and
               (case.find('failure') is not None or case.find('error') is not None)
               for case in cases)
    assert any('test_abrupt_exit' in case.attrib['name'] and
               case.find('error') is not None for case in cases)
    exits = [json.loads(path.read_text()) for path in
             (tmp_path / 'application-workers').glob('*-exit.json')]
    crashes = [row for row in exits if row['crashed']]
    assert len(crashes) == 1 and crashes[0]['returncode'] == 7
    assert crashes[0]['active']['test'].startswith('test_synthetic.py::test_abrupt_exit')


def test_diagnostics_do_not_consume_a_test_clock(tmp_path):
    result = run_synthetic(tmp_path, '''import time
import pytest

pytestmark = pytest.mark.xdist_group("synthetic-clock-sequence")

@pytest.fixture
def finite_clock(monkeypatch):
    clock = iter([123.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    yield

def test_clock(finite_clock):
    assert time.monotonic() == 123.0

def test_after_clock():
    assert time.monotonic() > 0
''')
    assert result.returncode == 0, result.stdout + result.stderr
    suite = ET.parse(tmp_path / 'application-results.xml').getroot().find('testsuite')
    assert suite.attrib['tests'] == '2'
    assert suite.attrib['errors'] == suite.attrib['failures'] == '0'
