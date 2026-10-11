"""Exercise the hosted command, including an abruptly exiting xdist worker."""

import os
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
    return argv


def run_synthetic(tmp_path, source):
    (tmp_path / 'test_synthetic.py').write_text(source)
    env = dict(os.environ)
    # Do not inherit a caller's selection, tracing or plugin configuration.
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_PLUGINS', None)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    # Load only the required distribution plugin. Bound this nested synthetic
    # run independently of the outer suite's worker count to avoid oversubscription.
    env['PYTEST_XDIST_AUTO_NUM_WORKERS'] = '2'
    command = application_command(tmp_path) + ['-p', 'xdist.plugin']
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
