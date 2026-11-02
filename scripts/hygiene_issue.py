"""Render local hygiene reports into publication-checked filing decisions."""
import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
LABEL = 'automated-hygiene'
TYPES = {'lint', 'dead-code', 'undeclared-deps', 'db-ratchet', 'swallowed-errors', 'import-cycles', 'flaky-tests',
         'vulnerable-deps', 'stale-branches', 'stale-issues', 'ghost-workflows'}
LABEL_DESCRIPTION = 'Automated repository hygiene reports'

def publication_ok(body, scanner_path, deny_file):
    try:
        spec = importlib.util.spec_from_file_location('hygiene_publication', scanner_path)
        scanner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = scanner
        spec.loader.exec_module(scanner)
        return not scanner._scan_bytes(body.encode(), location='hygiene-body', deny=scanner._deny_terms(deny_file))
    except Exception:
        return False

def render(kind, measurements, run, previous, partial=False):
    if not isinstance(measurements, dict):
        raise ValueError('expected measurement count map')
    url = urlsplit(run)
    if kind not in TYPES or url.scheme != 'https' or not url.netloc or url.query or url.fragment or not re.fullmatch(r'/[\w.-]+/[\w.-]+/actions/runs/[0-9]+', url.path):
        raise ValueError('unsupported report identity')
    if any(not re.fullmatch(r'[\w./:\[\](),=+%? -]{1,500}', key, flags=re.ASCII) or (' ' in key and not (key.startswith('tests/') and ':' * 2 in key)) or type(value) is not int or value < 0 for key, value in measurements.items()):
        raise ValueError('unsupported measurement')
    count = sum(measurements.values())
    payload = {'coverage': 'partial' if partial else 'complete', 'count': count, 'delta': None if previous is None else count - previous, 'measurements': measurements}
    body = f'Automated hygiene report: {kind}\n\nRun: {run}\n\nZero findings means no automatic closure.\n\n```json\n' + json.dumps(payload, sort_keys=True, indent=2) + '\n```\n'
    if len(body.encode()) > 60_000:
        raise ValueError('body limit')
    return body

def decide(kind, report, issues, run, scan):
    if len(issues) >= 1000:
        raise ValueError('issue snapshot limit')
    title = '[hygiene] ' + kind
    matches = [i for i in issues if i.get('state', 'OPEN') == 'OPEN' and not i.get('pull_request') and i['title'] == title and LABEL in [label['name'] for label in i['labels']]]
    issue = min(matches, key=lambda i: i['number']) if matches else None
    previous = None
    try:
        previous = json.loads((issue.get('body') or '').split('```json\n')[1].split('\n```')[0])['count'] if issue else None
        if type(previous) is not int or previous < 0:
            previous = None
    except (KeyError, IndexError, ValueError, TypeError):
        previous = None
    body = render(kind, report['findings'][kind], run, previous, bool(report.get('errors')))
    if not scan(title + '\n' + body + LABEL + '\n' + LABEL_DESCRIPTION):
        return {'action': 'skip'}, ''
    return {'action': 'edit' if issue else 'create', 'title': title, 'number': issue['number'] if issue else None}, body

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('report', 'issues', 'scanner', 'deny-file', 'body', 'decision', 'summary'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('kind', 'run'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    decision, body = {'action': 'skip'}, ''
    try:
        decision, body = decide(args.kind, json.loads(args.report.read_text()), json.loads(args.issues.read_text()), args.run, lambda text: publication_ok(text, args.scanner, args.deny_file))
        message = 'filing skipped: publication check' if decision['action'] == 'skip' else 'prepared: ' + args.kind
    except Exception:
        decision = {'action': 'error'}
        message = 'filing failed: unavailable or unsupported report; check --kind and findings count-map format'
        print(message, file=sys.stderr)
    args.body.write_text(body, encoding='utf-8')
    args.decision.write_text(json.dumps(decision), encoding='utf-8')
    with args.summary.open('a') as summary:
        summary.write(message + '\n')
    return 1 if decision['action'] == 'error' else 0
if __name__ == '__main__':
    sys.exit(main())
