import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
RUN = 'https://github.com/example/sample/actions/runs/1'
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

class API:
    def __init__(self):
        self.issues = [{'number': 7, 'title': '[hygiene] lint', 'labels': []}]
    def snapshot(self):
        return self.issues

def test_create_edit_zero_and_unlabeled():
    h, api = load('hygiene_issue'), API()
    report = {'findings': {'lint': {'src/sample.py': 2}}, 'logs': 'DO NOT COPY'}
    decision, body = h.decide('lint', report, api.snapshot(), RUN, lambda body: True)
    assert decision == {'action': 'create', 'title': '[hygiene] lint', 'number': None}
    assert 'DO NOT COPY' not in body
    api.issues.append({'number': 8, 'title': decision['title'], 'labels': [{'name': 'automated-hygiene'}], 'body': body})
    decision, body = h.decide('lint', {'findings': {'lint': {}}}, api.snapshot(), RUN, lambda body: True)
    assert decision == {'action': 'edit', 'title': '[hygiene] lint', 'number': 8}
    assert '"count": 0' in body and '"delta": -2' in body
    assert h.decide('lint', report, api.snapshot(), RUN, lambda body: False) == ({'action': 'skip'}, '')

def test_scanner_and_cli(tmp_path):
    h = load('hygiene_issue')
    scanner, deny = ROOT / 'scripts/publication-check.py', tmp_path / 'deny'
    # fixture-write: synthetic publication denial term.
    deny.write_text('synthetic-denied-term')
    assert not h.publication_ok('synthetic-denied-term', scanner, deny)
    assert h.publication_ok('Synthetic count: 0', scanner, deny)
    inputs, issues, body, decision, summary = [tmp_path / n for n in ('report', 'issues', 'body', 'decision', 'summary')]
    # fixture-write: synthetic local report and API snapshot.
    inputs.write_text(json.dumps({'findings': {'lint': {'synthetic-denied-term': 1}}}))
    # fixture-write: synthetic empty API issue snapshot.
    issues.write_text('[]')
    command = [sys.executable, str(ROOT / 'scripts/hygiene_issue.py'), '--report', str(inputs), '--issues', str(issues), '--kind', 'lint', '--run', RUN, '--scanner', str(scanner), '--deny-file', str(deny), '--body', str(body), '--decision', str(decision), '--summary', str(summary)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert json.loads(decision.read_text()) == {'action': 'skip'} and body.read_text() == ''
    assert summary.read_text().strip() == 'filing skipped: publication check'
    with pytest.raises(ValueError):
        h.render('lint', {'arbitrary prose': 1}, RUN, None)

def test_measurement_errors_and_reruns(tmp_path):
    r = load('hygiene_report')
    report = {'findings': {}, 'metrics': {}, 'errors': []}
    artifacts = []
    for attempt in (1, 2):
        import zipfile
        path = tmp_path / f'{attempt}.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            # fixture-write: synthetic JUnit artifact.
            archive.writestr('application-results.xml', '<testsuite><testcase classname="tests.test_sample.TestGroup" name="test_item">' + ('<failure/>' if attempt == 1 else '') + '</testcase></testsuite>')
        artifacts.append({'name': f'application-results-0-11-{attempt}', 'path': str(path), 'expired': False})
    runs = [{'id': 11, 'event': 'push', 'head_branch': 'main', 'jobs': [{'conclusion': 'failure', 'run_attempt': 1, 'name': 'application-shards (0)'}], 'artifacts': artifacts}]
    r.flakes(report, runs, 'application-results.xml')
    assert report['findings']['flaky-tests'] == {'tests/test_sample.py::TestGroup::test_item': 1, 'tests/test_sample.py::TestGroup::test_item::rerun-pass': 1}
    artifacts[0]['expired'] = True
    missing = {'findings': {}, 'metrics': {}, 'errors': []}
    r.flakes(missing, runs, 'application-results.xml')
    assert missing['errors'] and not missing['findings']
    output, summary = tmp_path / 'report', tmp_path / 'summary'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/hygiene_report.py'), 'flake-ledger', '--runs', str(tmp_path / 'absent'), '--junit-name', 'application-results.xml', '--output', str(output), '--summary', str(summary), '--install', 'failure', '--collection', 'failure'], capture_output=True)
    assert result.returncode == 0 and len(json.loads(output.read_text())['errors']) >= 2
