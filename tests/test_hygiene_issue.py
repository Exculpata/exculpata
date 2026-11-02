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

WEEKLY_KINDS = ('vulnerable-deps', 'stale-branches', 'stale-issues', 'ghost-workflows')

@pytest.mark.parametrize('kind', WEEKLY_KINDS)
def test_weekly_kind_creates_and_updates(kind):
    h = load('hygiene_issue')
    report = {'findings': {kind: {'count': 2}}, 'details': 'DO NOT COPY'}
    decision, body = h.decide(kind, report, [], RUN, lambda text: True)
    assert decision == {'action': 'create', 'title': '[hygiene] ' + kind, 'number': None}
    issue = dict(decision, body=body, labels=[{'name': h.LABEL}], number=9)
    decision, body = h.decide(kind, {'findings': {kind: {'count': 0}}}, [issue], RUN, lambda text: True)
    assert decision['action'] == 'edit' and decision['number'] == 9
    assert '"delta": -2' in body and 'DO NOT COPY' not in body

@pytest.mark.parametrize(('kind', 'report', 'valid'), [
    *[(kind, {'findings': {kind: {'count': 1}}}, True) for kind in WEEKLY_KINDS],
    ('unsupported', {'findings': {'unsupported': {}}}, False),
    ('lint', {'findings': {'lint': []}}, False),
    ('stale-branches', {'findings': {'stale-branches': []}}, False),
    ('lint', {'findings': {'lint': {'count': -1}}}, False),
    ('lint', {}, False),
])
def test_cli_cannot_silently_skip_report(tmp_path, kind, report, valid):
    inputs, issues, deny, body, decision, summary = [tmp_path / name for name in
                                                  ('report', 'issues', 'deny', 'body', 'decision', 'summary')]
    # fixture-write: synthetic report, empty issue snapshot and empty deny terms.
    inputs.write_text(json.dumps(report))
    # fixture-write: synthetic issue snapshot.
    issues.write_text('[]')
    # fixture-write: synthetic empty deny terms.
    deny.write_text('')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/hygiene_issue.py'),
                            '--report', str(inputs), '--issues', str(issues), '--kind', kind,
                            '--run', RUN, '--scanner', str(ROOT / 'scripts/publication-check.py'),
                            '--deny-file', str(deny), '--body', str(body), '--decision', str(decision),
                            '--summary', str(summary)], capture_output=True, text=True)
    if valid:
        assert result.returncode == 0
        assert json.loads(decision.read_text())['action'] == 'create'
    else:
        assert result.returncode != 0
        assert 'filing failed:' in result.stderr and 'filing failed:' in summary.read_text()
        assert json.loads(decision.read_text())['action'] == 'error'

def test_dependency_emitter_count_map(tmp_path, monkeypatch):
    h = load('hygiene_deps')
    lock = tmp_path / 'synthetic.lock'
    # fixture-write: synthetic lock filename; auditor process is substituted below.
    lock.write_text('')
    monkeypatch.setattr(h, 'ROOTS', (tmp_path,))
    audit = {'dependencies': [{'name': 'sample', 'vulns': [{'id': 'SYNTH-1'}, {'id': 'SYNTH-2'}]}]}
    monkeypatch.setattr(h.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, json.dumps(audit), ''))
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', ['hygiene_deps', '--output', str(output)])
    assert h.main() == 0
    report = json.loads(output.read_text())
    assert report['findings'] == {'vulnerable-deps': {'advisories': 2}}
    assert report['audits'][0]['result'] == audit
    assert not report['errors']
    monkeypatch.setattr(h.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 2, '', 'DO NOT COPY'))
    assert h.main() == 0
    report = json.loads(output.read_text())
    assert report['errors']
    _, body = load('hygiene_issue').decide('vulnerable-deps', report, [], RUN, lambda text: True)
    assert '"coverage": "partial"' in body and 'DO NOT COPY' not in body

def test_stale_emitter_count_map():
    h = load('hygiene_stale')
    data = {'branches': [{'name': 'synthetic', 'updated_at': '2026-01-01T00:00:00Z'}],
            'issues': [{'number': 1, 'updated_at': '2026-01-01T00:00:00Z'}],
            'workflows': [{'path': '.github/workflows/retired.yml'}], 'workflow_files': []}
    report = h.build_report(data, h.parse_time('2026-02-01T00:00:00Z'))
    assert report['findings'] == {kind: {'count': 1} for kind in WEEKLY_KINDS[1:]}
    assert report['details']['stale-branches'][0]['name'] == 'synthetic'
    assert report['details']['stale-issues'][0]['number'] == 1
    assert report['details']['ghost-workflows'] == ['retired.yml']
    for kind in WEEKLY_KINDS[1:]:
        _, body = load('hygiene_issue').decide(kind, report, [], RUN, lambda text: True)
        assert 'synthetic' not in body and 'retired.yml' not in body
