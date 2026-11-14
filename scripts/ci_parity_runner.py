"""Serial job execution with per-cell Git and filesystem ownership."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from scripts import ci_parity_expr as expr
from scripts.ci_parity_environment import Refused, local_environment, overrides
from scripts.ci_parity_plan import action_name, extract_pytest, service_context


def git(root, *args):
    try:
        return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE,
                                       text=True, timeout=60).strip()
    except subprocess.SubprocessError as error:
        raise Refused(f'git {args[0]} failed: {error}') from error


@contextmanager
def isolated_cell(source, head, output_directory):
    with tempfile.TemporaryDirectory(prefix='ci-parity-', dir=output_directory) as directory:
        home = Path(directory)
        tree, temporary = home / 'checkout', home / 'temp'
        temporary.mkdir()
        created = False
        try:
            git(source, 'worktree', 'add', '--detach', str(tree), head)
            created = True
            yield tree, temporary
        finally:
            if created:
                # The corpus guard intentionally removes write permission. Only
                # this cell's owned scratch checkout is made removable again.
                for path in tree.rglob('*'):
                    if not path.is_symlink():
                        path.chmod(path.stat().st_mode | 0o200)
                git(source, 'worktree', 'remove', '--force', str(tree))
                git(source, 'worktree', 'prune')


def file_commands(path):
    result = {}
    lines = path.read_text().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line:
            continue
        if '<<' in line and ('=' not in line or line.index('<<') < line.index('=')):
            name, delimiter = line.split('<<', 1)
            if not name or not delimiter:
                raise expr.Unmapped(f'invalid file command in {path.name}; see ci_parity_runner.file_commands')
            values = []
            while index < len(lines) and lines[index] != delimiter:
                values.append(lines[index])
                index += 1
            if index == len(lines):
                raise expr.Unmapped(f'unterminated multiline command in {path.name}; see ci_parity_runner.file_commands')
            index += 1
            result[name] = '\n'.join(values)
        elif '=' in line:
            name, value = line.split('=', 1)
            if not name:
                raise expr.Unmapped('empty file-command key; see ci_parity_runner.file_commands')
            result[name] = value
        else:
            raise expr.Unmapped(f'unknown file command {line!r}; see ci_parity_runner.file_commands')
    return result


def base_context(head, base, branch, base_branch, event=None):
    event = event or {'action': 'synchronize', 'number': 0, 'pull_request': {
        'number': 0, 'base': {'sha': base, 'ref': base_branch},
        'head': {'sha': head, 'ref': branch}, 'labels': []}, 'deleted': False}
    return {'github': {'event': event, 'event_name': 'pull_request', 'sha': head,
                       'ref': 'refs/pull/0/merge', 'head_ref': branch, 'base_ref': base_branch,
                       'run_id': '0', 'run_attempt': '1'},
            'inputs': {}, 'runner': {'os': 'Linux', 'arch': 'X64'},
            'needs': {}, 'steps': {}, 'matrix': {}, 'job': {'status': 'success'}, 'env': {}}


def run_shell(command, cwd, env, explicit_bash, timeout, log):
    flags = ['--noprofile', '--norc', '-e', '-o', 'pipefail'] if explicit_bash else ['-e']
    with log.open('w') as output:
        process = subprocess.Popen(['bash', *flags, '-c', command], cwd=cwd, env=env,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=max(0.001, timeout)), False
        except subprocess.TimeoutExpired:
            output.write('\nTIMEOUT: terminating the complete step process group\n')
            return 124, True
        finally:
            # Includes descendants left behind by a shell that exited first.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def result_status(results):
    for status in ('failure', 'cancelled'):
        if status in results:
            return status
    if results and all(item == 'skipped' for item in results):
        return 'skipped'
    return 'success'


class Runner:
    def __init__(self, source, venv, head, context, emit, log_directory):
        self.source, self.venv, self.head = source, venv, head
        self.context, self.emit, self.log_directory = context, emit, log_directory
        self.ran = self.failed = self.not_run = 0
        self.results = {}
        self.serial = 0

    def run(self, plan):
        for workflow in plan.workflows:
            if not workflow.triggered:
                continue
            results, failures = {}, {}
            self.results[workflow.name] = results
            for job in workflow.jobs:
                needs = {name: results[name] for name in job.needs}
                dependency_status = 'success' if all(item['result'] == 'success' for item in needs.values()) else 'failure' if any(item['result'] == 'failure' for item in needs.values()) else 'skipped'
                ancestor_failure = any(failures[name] for name in job.needs)
                context = {**self.context, 'needs': needs,
                           'github': {**self.context['github'], 'workflow': workflow.raw.get('name', workflow.name)}}
                tag = f'{workflow.name}:{job.name}'
                if not expr.condition(job.raw.get('if'), context, dependency_status, ancestor_failure):
                    self.emit(f'SKIP (condition) {tag}')
                    results[job.name] = {'result': 'skipped', 'outputs': {}}
                    failures[job.name] = ancestor_failure
                    continue
                cell_results, outputs = [], {}
                for matrix in job.cells:
                    if cell_results and job.raw.get('strategy', {}).get('fail-fast', True) and 'failure' in cell_results:
                        self.emit(f'SKIP (matrix fail-fast) {tag} {matrix}')
                        cell_results.append('cancelled')
                        continue
                    result, cell_outputs = self.cell(workflow, job, matrix, context, ancestor_failure)
                    cell_results.append(result)
                    outputs.update(cell_outputs)
                results[job.name] = {'result': result_status(cell_results), 'outputs': outputs}
                failures[job.name] = ancestor_failure or results[job.name]['result'] == 'failure'
                self.emit(f'JOB {tag} result={results[job.name]["result"]} outputs={json.dumps(outputs, sort_keys=True)}')
        return self.results

    def replay(self, workflow, job, step, tag, tree, temporary, env, context, defaults, deadline):
        commands, omitted = extract_pytest(step.raw['run'], tag)
        layers = (workflow.raw.get('env', {}), job.raw.get('env', {}), step.raw.get('env', {}))
        declared = {name for layer in layers for name in layer}
        replay_env = dict(env)
        dsn = 'CASE_REVIEW_POSTGRES_TEST_DSN'
        reserve = 'CASE_INTELLIGENCE_STORAGE_RESERVE_GIB'
        replay_env[dsn] = 'ci-parity-replay-stub-dsn'
        for name, value in step.raw.get('env', {}).items():
            if service_context(value):
                replay_env.pop(name, None)
                self.emit(f'REPLAY OMITTED ENV {name}: job.services unavailable')
            elif name != dsn:
                replay_env[name] = expr.render(value, {**context, 'env': replay_env})
        if reserve not in declared:
            replay_env[reserve] = '16384'
        if 'CASE_INTELLIGENCE_TEST_MODE' not in declared:
            replay_env.pop('CASE_INTELLIGENCE_TEST_MODE', None)
        plugins = replay_env.get('PYTEST_PLUGINS', '') if 'PYTEST_PLUGINS' in declared else ''
        replay_env['PYTEST_PLUGINS'] = ','.join(filter(None, ['ci_parity_service_replay', plugins]))
        replay_env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent), replay_env.get('PYTHONPATH', '')]))
        replay_env['PYTHONSAFEPATH'] = '1'
        replay_env = overrides(replay_env)
        run_context = {**context, 'env': replay_env}
        cwd = (tree / expr.render(step.raw.get('working-directory', defaults.get('working-directory', '.')), run_context)).resolve()
        if not cwd.is_relative_to(tree):
            raise expr.Unmapped(f'{tag}: working-directory escapes checkout; extend ci_parity_runner explicitly')
        self.emit(f'REPLAY {tag} reserve={replay_env[reserve]} ({"declared" if reserve in declared else "hostile"}) dsn=stub')
        for command in omitted:
            self.emit(f'REPLAY NOT REPLAYED {command[:80]}')
        totals = dict(stubbed_setup_ok=0, body_skipped=0, ran=0, passed=0,
                      skipped=0, unreplayable=0, failed=0)
        failed = False
        deadline = min(deadline, time.monotonic() + step.raw.get('timeout-minutes', 360) * 60)
        for command in commands:
            self.serial += 1
            report_directory = temporary / str(self.serial)
            report_directory.mkdir()
            report_path = report_directory / 'replay.json'
            replay_env['CI_PARITY_REPLAY_REPORT'] = str(report_path)
            log = self.log_directory / f'parity-{self.serial:04d}-replay.log'
            rendered = expr.render(command, run_context)
            code, timed_out = run_shell(rendered, cwd, replay_env, step.raw.get('shell', defaults.get('shell')) == 'bash', deadline - time.monotonic(), log)
            self.emit(f'REPLAY PYTEST exit={code} timeout={timed_out} log={log.name}')
            try:
                report = json.loads(report_path.read_text())
                counts, events = report['counts'], report['events']
                if set(counts) != set(totals) or any(type(value) is not int or value < 0 for value in counts.values()):
                    raise ValueError('invalid counters')
                for event in events:
                    kind, test, phase = event['kind'], event['test'], event['phase']
                    if kind == 'body_skipped':
                        self.emit(f'REPLAY BODY SKIPPED test={test} phase={phase}')
                    elif kind == 'unreplayable':
                        self.emit(f'REPLAY UNREPLAYABLE test={test} phase={phase} reason={event["reason"]}')
                    elif kind == 'failed':
                        self.emit(f'REPLAY FAIL test={test} phase={phase} {event["reason"]}: {event["message"]}')
                    else:
                        raise ValueError('invalid event kind')
                for key, value in counts.items():
                    totals[key] += value
                failed |= bool(counts['failed']) or code not in (0, 1) or (code == 1 and not (counts['failed'] or counts['unreplayable']))
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.emit(f'REPLAY REPORT FAILED {type(error).__name__}: missing or invalid pytest report')
                failed = True
        self.emit('REPLAY RESULT ' + ' '.join(f'{key}={value}' for key, value in totals.items()))
        self.ran += 1
        self.failed += int(failed)
        return 'failure' if failed else 'success'

    def cell(self, workflow, job, matrix, context, ancestor_failure=False):
        tag = f'{workflow.name}:{job.name}' + (f'[{json.dumps(matrix, sort_keys=True)}]' if matrix else '')
        with isolated_cell(self.source, self.head, self.log_directory) as (tree, temporary):
            self.emit(f'CELL {tag} checkout={tree} RUNNER_TEMP={temporary}')
            event_path = temporary / 'event.json'
            event_path.write_text(json.dumps(context['github']['event']))
            env = local_environment(tree, temporary, self.venv, self.head, context['github']['head_ref'], event_path)
            context = {**context, 'matrix': matrix, 'steps': {},
                       'runner': {**context['runner'], 'temp': str(temporary)},
                       'github': {**context['github'], 'workspace': str(tree)},
                       'job': {'status': 'success', 'services': {}}}
            for layer in (workflow.raw.get('env', {}), job.raw.get('env', {})):
                context['env'] = env
                env.update({name: expr.render(value, context) for name, value in layer.items()})
            env = overrides(env)
            status = 'success'
            deadline = time.monotonic() + job.raw.get('timeout-minutes', 360) * 60
            defaults = {**workflow.raw.get('defaults', {}).get('run', {}), **job.raw.get('defaults', {}).get('run', {})}
            for step in job.steps:
                context['job']['status'] = status
                context['env'] = env
                step_tag = tag + ':' + step.name
                step_state = {'outputs': {}, 'outcome': 'skipped', 'conclusion': 'skipped'}
                if not expr.condition(step.raw.get('if'), context, status, ancestor_failure):
                    self.emit(f'SKIP (condition) {step_tag}')
                elif time.monotonic() >= deadline:
                    self.emit(f'FAIL {step_tag}: job timeout-minutes exhausted')
                    self.failed += 1
                    status = 'failure'
                    step_state.update(outcome='failure', conclusion='failure')
                    break
                elif step.unavailable:
                    self.not_run += 1
                    self.emit(f'NOT RUN (expected-skip) {step_tag} category={step.unavailable[0]} reason={step.unavailable[1]}')
                    if step.expected and step.expected.get('replay'):
                        outcome = self.replay(workflow, job, step, step_tag, tree, temporary, env, context, defaults, deadline)
                        step_state.update(outcome=outcome, conclusion=outcome)
                        if outcome == 'failure':
                            status = 'failure'
                elif 'uses' in step.raw:
                    action = action_name(step.raw['uses'])
                    if action == 'actions/checkout':
                        requested = expr.render(step.raw.get('with', {}).get('ref', self.head), context)
                        if requested != self.head:
                            raise expr.Unmapped(f'{step_tag}: checkout ref {requested!r} differs from HEAD; add checkout support')
                    self.emit(f'HARNESS {step_tag}: {action}')
                    step_state.update(outcome='success', conclusion='success')
                else:
                    self.serial += 1
                    files = {name: temporary / f'{self.serial}-{name.lower()}' for name in
                             ('GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STEP_SUMMARY')}
                    for path in files.values():
                        path.touch()
                    step_env = {**env, **{name: str(path) for name, path in files.items()}}
                    step_env.update({name: expr.render(value, context, status) for name, value in step.raw.get('env', {}).items()})
                    step_env = overrides(step_env)
                    # env in run expressions includes this step's declared env.
                    run_context = {**context, 'env': step_env}
                    command = expr.render(step.raw['run'], run_context, status).replace('.venv/bin/', str(self.venv / 'bin') + '/')
                    cwd = (tree / expr.render(step.raw.get('working-directory', defaults.get('working-directory', '.')), run_context)).resolve()
                    if not cwd.is_relative_to(tree):
                        raise expr.Unmapped(f'{step_tag}: working-directory escapes checkout; extend ci_parity_runner explicitly')
                    slug = re.sub(r'[^A-Za-z0-9]+', '-', step_tag).strip('-')[:100]
                    log = self.log_directory / f'parity-{self.serial:04d}-{slug}.log'
                    budget = min(deadline - time.monotonic(), step.raw.get('timeout-minutes', 360) * 60)
                    started = time.monotonic()
                    self.emit(f'RUN {step_tag} log={log.name}')
                    code, timed_out = run_shell(command, cwd, step_env, step.raw.get('shell', defaults.get('shell')) == 'bash', budget, log)
                    self.ran += 1
                    outcome = 'failure' if code else 'success'
                    tolerated = step.raw.get('continue-on-error', False)
                    conclusion = 'success' if tolerated else outcome
                    if conclusion == 'failure':
                        self.failed += 1
                        status = 'failure'
                    step_state.update(outcome=outcome, conclusion=conclusion, outputs=file_commands(files['GITHUB_OUTPUT']))
                    updates = file_commands(files['GITHUB_ENV'])
                    if any(key.startswith(('GITHUB_', 'RUNNER_')) or key == 'NODE_OPTIONS' for key in updates):
                        raise expr.Unmapped(f'{step_tag}: reserved environment file command; see ci_parity_runner')
                    env.update(updates)
                    paths = files['GITHUB_PATH'].read_text().splitlines()
                    if any(not Path(path).is_absolute() for path in paths):
                        raise expr.Unmapped(f'{step_tag}: GITHUB_PATH requires absolute paths; see ci_parity_runner')
                    if paths:
                        env['PATH'] = os.pathsep.join([*reversed(paths), env['PATH']])
                    self.emit(f'{"FAIL" if code else "PASS"} {step_tag} exit={code} timeout={timed_out} continued={tolerated} seconds={time.monotonic() - started:.1f} log={log.name}')
                    if step_state['outputs']:
                        self.emit(f'OUTPUTS {step_tag}: {json.dumps(step_state["outputs"], sort_keys=True)}')
                    summary = files['GITHUB_STEP_SUMMARY'].read_text()
                    if summary:
                        self.emit(f'STEP SUMMARY {step_tag}:\n{summary}')
                if 'id' in step.raw:
                    context['steps'][step.raw['id']] = step_state
            context['job']['status'] = status
            context['env'] = env
            outputs = {name: expr.render(value, context, status) for name, value in job.raw.get('outputs', {}).items()}
            return status, outputs
