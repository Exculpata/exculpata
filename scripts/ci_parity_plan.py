"""Discover and validate the entire bounded workflow model before execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import tomllib

from scripts import ci_parity_expr as expr
from scripts.ci_parity_environment import Refused


HONORED = {
    'workflow': {'name', 'on', 'jobs', 'env', 'defaults'},
    'job': {'name', 'needs', 'if', 'steps', 'strategy', 'env', 'outputs', 'timeout-minutes', 'defaults'},
    'step': {'name', 'id', 'if', 'run', 'uses', 'with', 'env', 'shell',
             'working-directory', 'continue-on-error', 'timeout-minutes'},
    'action': {'actions/checkout', 'actions/setup-python'},
}
NOTED = {
    'workflow': {'permissions': 'local execution has no hosted token permissions',
                 'concurrency': 'one local invocation runs serially; hosted cancellation is not reproduced'},
    'job': {'runs-on': 'local machine, not the hosted runner image',
            'permissions': 'local execution has no hosted token permissions',
            'services': 'services are not started; dependent checks must be explicit NOT RUN entries'},
    'step': {},
}
NOT_RUN = {
    'actions/upload-artifact': ('github-only', 'hosted artifact upload unavailable offline'),
    'actions/download-artifact': ('download', 'hosted artifact download unavailable offline'),
    'actions/cache': ('download', 'hosted cache unavailable offline'),
    'actions/cache/restore': ('download', 'hosted cache unavailable offline'),
    'actions/cache/save': ('github-only', 'hosted cache publication unavailable offline'),
}
ACTION_INPUTS = {
    'actions/checkout': {'ref', 'fetch-depth', 'persist-credentials'},
    'actions/setup-python': {'python-version', 'cache', 'cache-dependency-path'},
    'actions/upload-artifact': {'name', 'path', 'if-no-files-found', 'retention-days'},
    'actions/download-artifact': {'name', 'path', 'pattern', 'merge-multiple'},
    'actions/cache': {'path', 'key', 'restore-keys'},
    'actions/cache/restore': {'path', 'key', 'restore-keys'},
    'actions/cache/save': {'path', 'key'},
}
CATEGORIES = {'install', 'download', 'docker', 'service', 'browser', 'github-only', 'binary-missing'}
# These identify unavailable capabilities, not an approval to skip a check. The
# exact data entry is required independently. Arbitrary shell is reviewed code,
# not sandboxed by this finite detector.
CAPABILITIES = (
    ('install', r'\bpip\s+install\b|\bmake\s+bootstrap\b|\bapt-get\b|\bsudo\b|\bnpm\s+(?:ci|install)\b|--locked-environments'),
    ('download', r'\bcurl\b|\bwget\b|urlretrieve\s*\(|--stage-models'),
    ('docker', r'\bdocker\s+(?:run|pull|build|exec|compose)\b|(?:^|\n)\s*(?:\./scripts/compose-check|[^\n]*python[^\n]*(?:-m pytest|-m unittest)[^\n]*(?:test_egress_proxy_live|test_relay_build_context_live|test_gateway_nginx))'),
    ('browser', r'run-browser-acceptance|corpus-check-full'),
    ('service', r'test_[\w-]*postgres|EXCULPATA_BACKUP_INTEGRATION'),
    ('binary-missing', r'/tmp/gitleaks|\$GITLEAKS|test_gitleaks_exception'),
)


class SelectionError(ValueError):
    """Requested workflow/job is absent (exit 4)."""


def fail(location, detail):
    raise expr.Unmapped(f'{location}: {detail}; add support in scripts/ci_parity_plan.py (HONORED/NOTED/NOT_RUN) and tests/test_ci_parity.py')


def unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            fail('YAML', f'duplicate key {key!r} at line {key_node.start_mark.line + 1}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


def read_yaml(path):
    try:
        import yaml
    except ImportError as error:
        raise Refused('PyYAML import unavailable; invoke ci_parity with the selected venv Python') from error

    class UniqueLoader(yaml.SafeLoader):
        pass

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    try:
        value = yaml.load(path.read_text(), Loader=UniqueLoader)
    except (yaml.YAMLError, OSError) as error:
        fail(path.stem, f'YAML {error}')
    if not isinstance(value, dict):
        fail(path.stem, 'expected YAML mapping')
    if True in value:
        if 'on' in value:
            fail(path.stem, 'ambiguous on key')
        value['on'] = value.pop(True)
    return value


def keys(value, allowed, location):
    if not isinstance(value, dict):
        fail(location, 'expected mapping')
    unknown = set(value) - set(allowed)
    if unknown:
        fail(location, f'unknown keys {sorted(map(str, unknown))}')


def mapping_keys(value, location):
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        fail(location, 'expected string-keyed mapping')


def scalar_values(value, location):
    mapping_keys(value, location)
    if any(not isinstance(item, (str, bool, int, float)) for item in value.values()):
        fail(location, 'expected scalar mapping values')


def validate_expressions(value, location):
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and '${{' in key:
                    fail(location, 'expressions in keys')
                if key == 'if':
                    expr.parse(item)
                else:
                    validate_expressions(item, location)
        elif isinstance(value, list):
            for item in value:
                validate_expressions(item, location)
        elif isinstance(value, str):
            for expression, part in expr.segments(value):
                if expression:
                    expr.parse(part)
    except expr.Unmapped as error:
        raise expr.Unmapped(f'{location}: {error}') from error


def validate_level(value, level, location, notes):
    keys(value, HONORED[level] | set(NOTED[level]), location)
    for key in NOTED[level]:
        if key in value:
            notes.add(f'{level}.{key}: {NOTED[level][key]}')
    validate_expressions({key: item for key, item in value.items() if key not in ('jobs', 'steps')}, location)
    for key in ('env', 'outputs'):
        if key in value:
            scalar_values(value[key], location + ':' + key)
    if 'timeout-minutes' in value:
        timeout = value['timeout-minutes']
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            fail(location, 'timeout-minutes must be a positive literal number')
    if 'defaults' in value:
        keys(value['defaults'], {'run'}, location + ':defaults')
        keys(value['defaults'].get('run', {}), {'shell', 'working-directory'}, location + ':defaults:run')
        validate_shell(value['defaults'].get('run', {}), location)
    validate_shell(value, location)


def validate_shell(value, location):
    if value.get('shell', 'bash') != 'bash':
        fail(location, 'shell other than bash')
    if 'working-directory' in value and not isinstance(value['working-directory'], str):
        fail(location, 'working-directory must be a string')


def step_name(step):
    return step.get('name') or step.get('run', '').strip().split('\n')[0] or action_name(step['uses'])


def action_name(value):
    return value.split('@', 1)[0]


def service_context(value, condition=False):
    if isinstance(value, dict):
        return any(service_context(item, key == 'if') for key, item in value.items())
    if isinstance(value, list):
        return any(service_context(item) for item in value)
    if isinstance(value, str):
        expressions = [value] if condition else [part for active, part in expr.segments(value) if active]
        return any(node[0] == 'get' and node[1] == ('context', 'job') and node[2] == ('literal', 'services')
                   for source in expressions for node in expr.walk(expr.parse(source)))
    return False


@dataclass
class Step:
    name: str
    raw: dict
    unavailable: tuple[str, str] | None = None
    expected: dict | None = None


@dataclass
class Job:
    name: str
    raw: dict
    needs: list[str]
    cells: list[dict]
    steps: list[Step]


@dataclass
class Workflow:
    name: str
    filename: str
    raw: dict
    triggers: list[str]
    triggered: bool
    reason: str
    jobs: list[Job] = field(default_factory=list)


@dataclass
class Plan:
    workflows: list[Workflow]
    notes: list[str]
    skips: list[dict]
    python_versions: set[str]
    stale: list[str]
    unexpected: list[str]

    def snapshot(self):
        return {
            'workflows': [{
                'workflow': workflow.name, 'triggers': workflow.triggers,
                'triggered': workflow.triggered, 'reason': workflow.reason,
                'definition': normalized(workflow.raw) if workflow.jobs else None,
                'jobs': [{'job': job.name, 'needs': job.needs, 'cells': job.cells,
                          'steps': [{'name': step.name, 'unavailable': step.unavailable,
                                     'expected': step.expected} for step in job.steps]}
                         for job in workflow.jobs],
            } for workflow in self.workflows],
            'notes': self.notes, 'python_versions': sorted(self.python_versions),
            'skips': self.skips, 'stale': self.stale, 'unexpected': self.unexpected,
        }


def normalized(value):
    if isinstance(value, dict):
        return {key: action_name(item) if key == 'uses' else
                hashlib.sha256(item.encode()).hexdigest() if key == 'run' and isinstance(item, str) else
                normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def load_skips(root):
    path = root / 'scripts' / 'ci-parity-expected-skips.toml'
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        fail('skips', str(error))
    keys(data, {'skip'}, 'skips')
    result, seen = [], set()
    for entry in data.get('skip', []):
        keys(entry, {'workflow', 'job', 'step', 'category', 'reason'}, 'skips')
        if set(entry) != {'workflow', 'job', 'step', 'category', 'reason'} or not all(isinstance(value, str) and value.strip() for value in entry.values()):
            fail('skips', 'every entry requires workflow, job, step, category and reason strings')
        if entry['category'] not in CATEGORIES:
            fail('skips', f'unknown category {entry["category"]}')
        identity = tuple(entry[key] for key in ('workflow', 'job', 'step'))
        if identity in seen:
            fail(':'.join(identity), 'duplicate expected skip')
        seen.add(identity)
        result.append(entry)
    return result


def glob_regex(pattern, location):
    if not isinstance(pattern, str) or not pattern or re.search(r'[\[\]{}+\\]', pattern):
        fail(location, f'unsupported trigger glob {pattern!r}')
    result, index = '', 0
    while index < len(pattern):
        if pattern[index:index + 3] == '**/':
            result += '(?:.*/)?'
            index += 3
        elif pattern[index:index + 2] == '**':
            result += '.*'
            index += 2
        else:
            character = pattern[index]
            result += '[^/]*' if character == '*' else '[^/]' if character == '?' else re.escape(character)
            index += 1
    return re.compile('^' + result + '$')


def matches_patterns(value, patterns, location):
    matched = False
    for pattern in patterns:
        negative = pattern.startswith('!')
        if glob_regex(pattern[1:] if negative else pattern, location).fullmatch(value):
            matched = not negative
    return matched


def trigger(data, event, files, branch, action, location):
    events = data.get('on')
    if isinstance(events, str):
        events = {events: None}
    elif isinstance(events, list) and all(isinstance(item, str) for item in events):
        events = dict.fromkeys(events)
    mapping_keys(events, location + ':on')
    if event not in events:
        return sorted(events), False, f'not triggered by {event}'
    filters = events[event] or {}
    keys(filters, {'types', 'branches', 'branches-ignore', 'paths', 'paths-ignore'}, location + ':on')
    for key, patterns in filters.items():
        if not isinstance(patterns, list) or not patterns or not all(isinstance(item, str) for item in patterns):
            fail(location, f'trigger {key} requires a nonempty list of strings')
        if key != 'types':
            if key.endswith('-ignore') and any(item.startswith('!') for item in patterns):
                fail(location, f'negation in {key}')
            for pattern in patterns:
                glob_regex(pattern.removeprefix('!'), location)
            if all(item.startswith('!') for item in patterns):
                fail(location, f'{key} requires a positive pattern')
    for kind in ('branches', 'paths'):
        if kind in filters and kind + '-ignore' in filters:
            fail(location, f'both {kind} and {kind}-ignore')
    accepted = action in filters.get('types', ['opened', 'synchronize', 'reopened'])
    if 'branches' in filters:
        accepted &= matches_patterns(branch, filters['branches'], location)
    if 'branches-ignore' in filters:
        accepted &= not matches_patterns(branch, filters['branches-ignore'], location)
    if any(key.startswith('paths') for key in filters) and files is None:
        fail(location, 'path filters require the complete changed-file list')
    if 'paths' in filters:
        accepted &= any(matches_patterns(path, filters['paths'], location) for path in files)
    if 'paths-ignore' in filters:
        accepted &= any(not matches_patterns(path, filters['paths-ignore'], location) for path in files)
    return sorted(events), bool(accepted), 'event filters match' if accepted else 'event filters do not match'


def matrix_cells(raw, location):
    strategy = raw.get('strategy', {})
    keys(strategy, {'matrix', 'fail-fast', 'max-parallel'}, location + ':strategy')
    if 'fail-fast' in strategy and type(strategy['fail-fast']) is not bool:
        fail(location, 'strategy fail-fast must be boolean')
    if 'max-parallel' in strategy and (type(strategy['max-parallel']) is not int or strategy['max-parallel'] <= 0):
        fail(location, 'strategy max-parallel must be a positive integer')
    matrix = strategy.get('matrix', {})
    mapping_keys(matrix, location + ':matrix')
    for key, values in matrix.items():
        if key in ('include', 'exclude') or not isinstance(values, list) or not values:
            fail(location, f'matrix {key}: only nonempty literal axes are supported')
        if any(not literal_matrix_value(item) for item in values):
            fail(location, f'matrix {key}: requires literal JSON-compatible values')
    return [dict(zip(matrix, values)) for values in itertools.product(*matrix.values())]


def literal_matrix_value(value):
    if isinstance(value, dict):
        return all(isinstance(key, str) and literal_matrix_value(item) for key, item in value.items())
    if isinstance(value, list):
        return all(literal_matrix_value(item) for item in value)
    if isinstance(value, str):
        return '${{' not in value
    return value is None or type(value) in (bool, int, float) and math.isfinite(value)


def validate_services(services, location):
    mapping_keys(services, location)
    for name, value in services.items():
        keys(value, {'image', 'env', 'ports', 'options'}, location + ':' + name)
        if not isinstance(value.get('image'), str):
            fail(location, 'service requires image string')
        scalar_values(value.get('env', {}), location)
        if not isinstance(value.get('ports', []), list) or not isinstance(value.get('options', ''), str):
            fail(location, 'service ports/options shape')


def build(root: Path, event='pull_request', changed_files=None, branch='main', action='synchronize', workflows=None, jobs=None):
    if event != 'pull_request':
        fail('event', f'unsupported event {event}')
    skips = load_skips(root)
    indexed = {(row['workflow'], row['job'], row['step']): row for row in skips}
    found, notes, versions, unexpected, matched = [], set(), set(), [], set()
    for path in sorted((root / '.github' / 'workflows').glob('*')):
        if path.suffix not in ('.yml', '.yaml'):
            continue
        raw = read_yaml(path)
        # Non-PR jobs may use different grammars; their event exclusion is explicit.
        keys(raw, HONORED['workflow'] | set(NOTED['workflow']), path.stem)
        triggers, active, reason = trigger(raw, event, changed_files, branch, action, path.stem)
        workflow = Workflow(path.stem, path.name, raw, triggers, active, reason)
        found.append(workflow)
        if event not in triggers:
            continue
        validate_level(raw, 'workflow', path.stem, notes)
        raw_jobs = raw.get('jobs')
        mapping_keys(raw_jobs, path.stem + ':jobs')
        pending = dict(raw_jobs)
        while pending:
            progress = False
            for name, job_raw in list(pending.items()):
                location = f'{path.stem}:{name}'
                validate_level(job_raw, 'job', location, notes)
                needs = job_raw.get('needs', [])
                needs = [needs] if isinstance(needs, str) else needs
                if not isinstance(needs, list) or any(not isinstance(item, str) or item not in raw_jobs for item in needs):
                    fail(location, 'needs must name existing jobs')
                if any(item in pending for item in needs):
                    continue
                if 'services' in job_raw:
                    validate_services(job_raw['services'], location + ':services')
                if service_context(job_raw.get('env', {})) or service_context(raw.get('env', {})):
                    fail(location, 'service context in shared env cannot be supplied without provisioning services')
                cells = matrix_cells(job_raw, location)
                steps = []
                ids = set()
                raw_steps = job_raw.get('steps')
                if not isinstance(raw_steps, list) or not raw_steps:
                    fail(location, 'job requires nonempty steps')
                for step_raw in raw_steps:
                    validate_level(step_raw, 'step', location, notes)
                    if ('run' in step_raw) == ('uses' in step_raw):
                        fail(location, 'step requires exactly one of run or uses')
                    if any(key in step_raw and not isinstance(step_raw[key], str) for key in ('run', 'uses', 'name', 'id')):
                        fail(location, 'run/uses/name/id must be strings')
                    name_text = step_name(step_raw)
                    step_location = location + ':' + name_text
                    if 'id' in step_raw:
                        if step_raw['id'] in ids:
                            fail(step_location, 'duplicate step id')
                        ids.add(step_raw['id'])
                    if 'continue-on-error' in step_raw and not isinstance(step_raw['continue-on-error'], bool):
                        fail(step_location, 'continue-on-error must be boolean')
                    unavailable = None
                    expected = indexed.get((path.stem, name, name_text))
                    if expected:
                        if (path.stem, name, name_text) in matched:
                            fail(step_location, 'expected skip matches more than one step; use unique step names')
                        matched.add((path.stem, name, name_text))
                    if 'uses' in step_raw:
                        action_id = action_name(step_raw['uses'])
                        if action_id.startswith('./'):
                            local = (root / action_id).resolve()
                            if not local.is_relative_to(root.resolve()):
                                fail(step_location, 'local action outside repository')
                            definitions = [item for item in local.glob('action.*') if item.suffix in ('.yml', '.yaml')]
                            if len(definitions) != 1:
                                fail(step_location, 'local action definition absent or ambiguous')
                            definition = read_yaml(definitions[0])
                            if definition.get('runs', {}).get('using') != 'composite' or expected is None:
                                fail(step_location, 'local composite action needs expansion or an exact NOT RUN entry')
                            keys(step_raw.get('with', {}), definition.get('inputs', {}), step_location + ':with')
                            unavailable = (expected['category'], expected['reason'])
                            notes.add('local composite action: explicitly NOT RUN, including nested actions')
                        elif action_id not in HONORED['action'] and action_id not in NOT_RUN:
                            fail(step_location, f'unknown action {action_id}')
                        else:
                            keys(step_raw.get('with', {}), ACTION_INPUTS[action_id], step_location + ':with')
                            unavailable = NOT_RUN.get(action_id)
                            if action_id == 'actions/setup-python':
                                version = step_raw.get('with', {}).get('python-version')
                                if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', version):
                                    fail(step_location, 'setup-python requires literal python-version')
                                versions.add(version)
                                if 'cache' in step_raw.get('with', {}):
                                    notes.add('setup-python cache: NOT RUN; existing venv only, no cache network access')
                            if action_id == 'actions/checkout':
                                depth = step_raw.get('with', {}).get('fetch-depth', 1)
                                if depth not in (0, 1):
                                    fail(step_location, 'checkout fetch-depth other than 0/1')
                                notes.add('checkout: detached HEAD with existing complete local history; no credential persistence or fetching')
                    else:
                        if 'with' in step_raw:
                            fail(step_location, 'with on run step')
                        searchable = step_raw['run'] + '\n' + json.dumps(step_raw.get('env', {}))
                        for category, pattern in CAPABILITIES:
                            if re.search(pattern, searchable):
                                unavailable = (category, 'command requires ' + category + ' capability')
                                break
                    if expected:
                        unavailable = (expected['category'], expected['reason'])
                    elif service_context(step_raw):
                        unavailable = ('service', 'service context requires a provisioned job service')
                    if unavailable and expected is None:
                        unexpected.append(step_location + ': ' + unavailable[1])
                    steps.append(Step(name_text, step_raw, unavailable, expected))
                workflow.jobs.append(Job(name, job_raw, needs, cells, steps))
                del pending[name]
                progress = True
            if not progress:
                fail(path.stem, 'cyclic job needs')
    stale = [':'.join(identity) for identity in indexed if identity not in matched]
    if workflows:
        missing = set(workflows) - {item for wf in found for item in (wf.name, wf.filename)}
        if missing:
            raise SelectionError(f'workflow not found: {sorted(missing)}')
        found = [wf for wf in found if wf.name in workflows or wf.filename in workflows]
    if jobs:
        missing = set(jobs) - {job.name for wf in found for job in wf.jobs}
        if missing:
            raise SelectionError(f'job not found: {sorted(missing)}')
        for wf in found:
            selected = {job.name for job in wf.jobs if job.name in jobs}
            for job in reversed(wf.jobs):
                if job.name in selected:
                    selected.update(job.needs)
            wf.jobs = [job for job in wf.jobs if job.name in selected]
    return Plan(found, sorted(notes), skips, versions, stale, unexpected)
