"""Scheduled measurements; errors are data, never test or merge gates."""
import ast
import argparse
import json
import re
import subprocess
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
def measure(report, tool, action):
    try:
        return action()
    except Exception as error:
        report['errors'].append({'tool': tool, 'error': type(error).__name__})

def command(args, accepted=(0, 1)):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if result.returncode not in accepted:
        raise RuntimeError('tool failed')
    return result.stdout

def static(report, args):
    trees = {p: ast.parse(p.read_text()) for base in args.sources for p in Path(base).rglob('*.py')}
    production = {p: tree for p, tree in trees.items() if not p.is_relative_to(args.tests)}
    baseline = json.loads(args.baseline.read_text())
    raw = {str(p): sum(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'fetchall' for n in ast.walk(t)) for p, t in production.items()}
    report['metrics']['fetchall'] = raw
    report['findings']['db-ratchet'] = {p: max(0, n - baseline.get(p, 0)) for p, n in raw.items() if n > baseline.get(p, 0)}
    report['findings']['db-ratchet'].update({p + '::stale-baseline': 1 for p in baseline.keys() - raw.keys()})
    swallowed = {str(p): sum(isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name) and n.type.id == 'Exception' and len(n.body) == 1 and isinstance(n.body[0], ast.Pass) for n in ast.walk(t)) for p, t in production.items()}
    report['findings']['swallowed-errors'] = {p: n for p, n in swallowed.items() if n}
    report['metrics']['god-files'] = {str(p): len(p.read_text().splitlines()) for p in production if len(p.read_text().splitlines()) > 1500}
    report['metrics']['test-markers'] = dict(Counter(n.attr for t in trees.values() for n in ast.walk(t) if isinstance(n, ast.Attribute) and ast.unparse(n) in {'.'.join(parts) for parts in [('pytest', 'skip'), ('pytest', 'xfail'), *[('pytest', 'mark', mark) for mark in ('skip', 'skipif', 'xfail')]]}))
    modules = {str(p).split('/src/')[-1].removeprefix('src/').removesuffix('.py').replace('/', '.').removesuffix('.__init__'): t for p, t in production.items()}
    graph = {name: set() for name in modules}
    for name, tree in modules.items():
        for node in ast.walk(tree):
            imports = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom):
                package = name if any(p.stem == '__init__' and t is tree for p, t in production.items()) else name.rpartition('.')[0]
                prefix = '.'.join(package.split('.')[:len(package.split('.')) - node.level + 1]) if node.level else ''
                base = '.'.join(filter(None, (prefix, node.module)))
                imports = [base] + [base + '.' + a.name for a in node.names]
            graph[name].update(i for i in imports if i in modules and i != name)
    def reachable(start):
        seen, todo = set(), [start]
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(graph[node] - seen)
        return seen
    reach = {name: reachable(name) for name in graph}
    remaining, cycles = set(graph), {}
    while remaining:
        node = min(remaining)
        group = {other for other in reach[node] if node in reach[other]}
        remaining -= group
        if len(group) > 1:
            cycles[node] = len(group)
    report['findings']['import-cycles'] = cycles
    broken = Counter()
    for path in [*args.docs.rglob('*.md'), *args.root.glob('*.md')]:
        for target in re.findall(r'\]\(([^\s)]+)', path.read_text()):
            url = urlsplit(target.strip('<>'))
            if url.path and not url.scheme and not url.netloc and not (path.parent / unquote(url.path)).exists():
                broken[str(path)] += 1
    report['metrics']['broken-doc-links'] = dict(broken)

def hygiene(report, args):
    measure(report, 'static', lambda: static(report, args))
    if args.install != 'success':
        return
    lint = measure(report, 'ruff', lambda: {row['code']: row['count'] for row in json.loads(command(['ruff', 'check', '--no-cache', *map(str, args.sources), str(args.scripts), '--select', 'F401,F841,B023,B905', '--statistics', '--output-format', 'json']))})
    dead = measure(report, 'vulture', lambda: {'unused': len(command(['vulture', *[str(p) for p in args.sources if p != args.tests], '--min-confidence', '80'], (0, 3)).splitlines())})
    def dependencies():
        command(['deptry', str(args.root), '--json-output', str(args.deptry_output)])
        return dict(Counter(row['error']['code'] for row in json.loads(args.deptry_output.read_text())))
    deps = measure(report, 'deptry', dependencies)
    report['findings'].update({key: value for key, value in [('lint', lint), ('dead-code', dead), ('undeclared-deps', deps)] if value is not None})

def junit(data):
    text = data.decode('utf-8')
    if '<!DOCTYPE' in text or '<!ENTITY' in text:
        raise ValueError('unsupported XML')
    rows = {}
    root = ET.fromstring(text)
    if root.tag not in {'testsuite', 'testsuites'}:
        raise ValueError('unsupported JUnit root')
    for case in root.iter('testcase'):
        parts = case.attrib['classname'].split('.')
        end = next((i for i in range(len(parts), 0, -1) if Path('/'.join(parts[:i]) + '.py').is_file()), next((i for i, part in enumerate(parts) if part[:1].isupper()), len(parts)))
        node = '/'.join(parts[:end]) + '.py::' + (':' * 2).join(parts[end:] + [case.attrib['name']]) if parts[0] else case.attrib['name'].replace('.', '/') + '.py'
        if not node.startswith('tests/'): raise ValueError('unsupported test node')
        state = 'failed' if case.find('failure') is not None or case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'
        rows[node] = state if rows.get(node) != 'failed' else 'failed'
    return rows

def flakes(report, runs, junit_name):
    observations, failures = defaultdict(dict), Counter()
    for run in runs:
        if run['event'] in {'pull_request', 'pull_request_target'} or (run['head_branch'] != 'main' and run['event'] != 'merge_group'):
            continue
        def collect(run=run):
            jobs = run['jobs']
            failed = [j for j in jobs if j['conclusion'] == 'failure']
            if not failed:
                return
            failures[str(run['id'])] = len(failed)
            artifacts = run['artifacts']
            covered = set()
            for artifact in artifacts:
                match = re.fullmatch(r"application-results-(\d+)-" + str(run['id']) + r"-(\d+)", artifact['name'])
                if not match:
                    continue
                if artifact['expired']:
                    raise ValueError('expired artifact')
                with zipfile.ZipFile(artifact['path']) as archive:
                    entry = next(e for e in archive.infolist() if Path(e.filename).name == junit_name)
                    if entry.file_size > 16_000_000:
                        raise ValueError('XML limit')
                    rows = junit(archive.read(entry))
                attempt = int(match[2])
                covered.add((int(match[1]), attempt))
                for node, state in rows.items():
                    states = observations[(run['id'], node)]
                    states[attempt] = state if states.get(attempt) != 'failed' else 'failed'
            expected = {(int(m[1]), j['run_attempt']) for j in failed if (m := re.fullmatch(r'application-shards \((\d+)\)', j['name']))}
            if not covered or not expected.issubset(covered) or any(not re.fullmatch(r'application(?:-shards \([0-9]+\))?', j['name']) for j in failed):
                raise ValueError('missing test artifacts')
        measure(report, 'actions-artifacts', collect)
    counts = Counter()
    for (_, node), attempts in observations.items():
        bad = [a for a, state in attempts.items() if state == 'failed']
        if bad:
            counts[node] += len(bad)
            counts[node + '::rerun-pass'] += int(any(a > min(bad) and state == 'passed' for a, state in attempts.items()))
    report['metrics'].update({'failed-jobs': dict(failures), 'flaky-tests': dict(counts)})
    if counts or not report['errors']:
        report['findings']['flaky-tests'] = dict(counts)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=('hygiene-report', 'flake-ledger'))
    for name in ('root', 'tests', 'scripts', 'docs', 'baseline', 'deptry-output', 'runs', 'output', 'summary'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--sources', type=Path, nargs='+')
    for name in ('install', 'collection', 'junit-name'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    report = {'findings': {}, 'metrics': {}, 'errors': []}
    for tool, state in [('install', args.install), ('collection', args.collection)]:
        if state != 'success':
            report['errors'].append({'tool': tool, 'error': 'input unavailable'})
    measure(report, args.job, lambda: hygiene(report, args) if args.job == 'hygiene-report' else flakes(report, json.loads(args.runs.read_text()), args.junit_name))
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    with args.summary.open('a') as summary:
        summary.write(f"{args.job}: {len(report['findings'])} measured finding types; {len(report['errors'])} tool errors. See report artifact.\n")
if __name__ == '__main__':
    main()
