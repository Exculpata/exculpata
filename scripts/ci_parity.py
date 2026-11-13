#!/usr/bin/env python3
"""Offline workflow parity: ci_parity.py <worktree> <out-file> <venv>.

Exit 0: executed checks passed (expected NOT RUN steps permit GREEN OVERALL).
Exit 1: check failed; 2: refused/preflight; 3: unexpected NOT RUN;
4: no steps/request not found; 5: UNMAPPED workflow behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ci_parity_expr as expr
from scripts.ci_parity_environment import Refused, describe, preflight
from scripts.ci_parity_plan import SelectionError, build, load_skips
from scripts.ci_parity_runner import Runner, base_context, git


def scenario_plan(plan, context, supplied):
    """Preview hypothetical outputs explicitly; never classify changes here."""
    rows = []
    for workflow in plan.workflows:
        if not workflow.triggered:
            continue
        results = {}
        for job in workflow.jobs:
            needs = {name: results[name] for name in job.needs}
            status = 'success' if all(item['result'] == 'success' for item in needs.values()) else 'failure'
            job_context = {**context, 'needs': needs, 'env': {}, 'steps': {}}
            applicable = expr.condition(job.raw.get('if'), job_context, status)
            outputs = {}
            for matrix in job.cells:
                cell = {**job_context, 'matrix': matrix, 'steps': {}}
                steps = []
                for step in job.steps:
                    active = applicable and expr.condition(step.raw.get('if'), cell)
                    steps.append({'step': step.name, 'state': 'SKIP' if not active else 'NOT RUN' if step.unavailable else 'RUN' if 'run' in step.raw else 'HARNESS'})
                    if 'id' in step.raw:
                        identity = ':'.join((workflow.name, job.name, step.raw['id']))
                        if active and identity not in supplied:
                            raise expr.Unmapped(f'{identity}: scenario needs explicit step outputs; regenerate scenario fixtures from the real command')
                        cell['steps'][step.raw['id']] = {'outputs': supplied.get(identity, {}) if active else {},
                                                        'outcome': 'success' if active else 'skipped',
                                                        'conclusion': 'success' if active else 'skipped'}
                if applicable:
                    outputs = {name: expr.render(value, cell) for name, value in job.raw.get('outputs', {}).items()}
                rows.append({'workflow': workflow.name, 'job': job.name, 'matrix': matrix,
                             'state': 'RUN' if applicable else 'SKIP', 'outputs': outputs, 'steps': steps})
            results[job.name] = {'result': 'success' if applicable else 'skipped', 'outputs': outputs}
    return rows


def changed_inputs(files):
    return [name for name in files if name.startswith(('.github/workflows/', '.github/actions/', 'scripts/ci_parity'))
            or name == 'scripts/ci-parity-expected-skips.toml']


def validate_bindings(plan, context):
    """Reject unsupported checkout targets and literal paths before any step runs."""
    for workflow in plan.workflows:
        for job in workflow.jobs:
            defaults = {**workflow.raw.get('defaults', {}).get('run', {}),
                        **job.raw.get('defaults', {}).get('run', {})}
            for matrix in job.cells:
                bound = {**context, 'matrix': matrix}
                for step in job.steps:
                    location = ':'.join((workflow.name, job.name, step.name))
                    if step.raw.get('uses', '').split('@')[0] == 'actions/checkout':
                        ref = expr.render(step.raw.get('with', {}).get('ref', context['github']['sha']), bound)
                        if ref != context['github']['sha']:
                            raise expr.Unmapped(f'{location}: checkout ref differs from HEAD; extend ci_parity checkout mapping')
                    directory = step.raw.get('working-directory', defaults.get('working-directory', '.'))
                    if '${{' not in directory and (Path(directory).is_absolute() or '..' in Path(directory).parts):
                        raise expr.Unmapped(f'{location}: working-directory outside checkout; extend ci_parity runner mapping')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('worktree', type=Path)
    parser.add_argument('out_file', type=Path)
    parser.add_argument('venv', type=Path)
    parser.add_argument('--event', default='pull_request')
    parser.add_argument('--base-ref', default='origin/main')
    parser.add_argument('--workflows', help='comma-separated stems or filenames')
    parser.add_argument('--jobs', help='comma-separated job IDs; dependencies included')
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--scenario', type=Path, help='with --plan only: JSON of explicitly hypothetical workflow:job:step-id outputs')
    args = parser.parse_args(argv)
    source, out, venv = args.worktree.resolve(), args.out_file.resolve(), args.venv.resolve()
    runner, code, message, unexpected = None, 2, 'REFUSED', 0
    out.parent.mkdir(parents=True, exist_ok=True)
    # Check before opening the report so a report inside the source does not
    # disguise preexisting changes. Execution is always from committed HEAD.
    status_error = None
    try:
        dirty = git(source, 'status', '--porcelain', '--untracked-files=all')
    except (Refused, OSError) as error:
        dirty, status_error = '', error
    with out.open('w') as report:
        lines = []

        def emit(line):
            print(line, flush=True)
            lines.append(line)
            report.write(line + '\n')
            report.flush()

        try:
            emit('LOCAL CI PARITY')
            if status_error:
                raise status_error
            if dirty and not args.plan:
                raise Refused('worktree not clean: ' + dirty)
            if dirty:
                emit('PLAN ONLY: source has uncommitted changes; execution requires a clean tree')
            head = git(source, 'rev-parse', 'HEAD')
            base = git(source, 'rev-parse', args.base_ref)
            ancestor = git(source, 'merge-base', base, head)
            branch = git(source, 'branch', '--show-current') or 'detached'
            base_branch = args.base_ref.removeprefix('refs/heads/').removeprefix('refs/remotes/').removeprefix('origin/')
            files = subprocess.check_output(['git', 'diff', '--name-only', '--no-renames', '-z', ancestor, head, '--'], cwd=source, timeout=60).decode().rstrip('\0').split('\0')
            files = [name for name in files if name]
            emit(f'event={args.event} action=synchronize head={head} base={base} merge_base={ancestor} branch={branch}')
            emit(f'selection: workflows={args.workflows or "all"} jobs={args.jobs or "all"}; required dependencies included')
            emit('computed outputs: pending real workflow steps; each OUTPUTS/JOB line records the values used')
            for delta in describe():
                emit('ENVIRONMENT DELTA: ' + delta)
            changed = changed_inputs(files)
            if changed:
                emit('PARITY INPUTS CHANGED: ' + ', '.join(changed))
            for skip in load_skips(source):
                emit('EXPECTED NOT RUN: {workflow}:{job}:{step} category={category} reason={reason}'.format(**skip))
            if args.event != 'pull_request':
                raise expr.Unmapped(f'unsupported event {args.event}; add event mapping in ci_parity_plan')
            installed = preflight(source, venv, set())
            plan = build(source, args.event, files, base_branch,
                         workflows=args.workflows.split(',') if args.workflows else None,
                         jobs=args.jobs.split(',') if args.jobs else None)
            context = base_context(head, base, branch, base_branch)
            validate_bindings(plan, context)
            for note in plan.notes:
                emit('NOTED: ' + note)
            for workflow in plan.workflows:
                emit(f'WORKFLOW {workflow.name}: {workflow.reason}; triggers={",".join(workflow.triggers)}')
            if plan.unexpected:
                unexpected = len(plan.unexpected)
                for item in plan.unexpected:
                    emit('NOT RUN (UNEXPECTED): ' + item)
                if plan.stale:
                    emit('STALE EXPECTED SKIPS: ' + ', '.join(plan.stale))
                code, message = 3, 'NOT GREEN, UNEXPECTED NOT RUN STEPS'
            else:
                if plan.stale:
                    raise expr.Unmapped('stale expected skips: ' + ', '.join(plan.stale) + '; update the reviewed skip data and drift test')
                installed = preflight(source, venv, plan.python_versions, installed)
                emit('PREFLIGHT PASS: Python=' + '.'.join(map(str, installed['python'])) + '; dev extras satisfied')
                if args.plan:
                    emit('PLAN: ' + json.dumps(plan.snapshot(), sort_keys=True))
                    if args.scenario:
                        supplied = json.loads(args.scenario.read_text())
                        emit('SCENARIO (hypothetical supplied outputs): ' + str(args.scenario))
                        emit(json.dumps(scenario_plan(plan, context, supplied), indent=2, sort_keys=True))
                    code, message = 0, 'PLAN ONLY; NO CHECKS EXECUTED'
                else:
                    if args.scenario:
                        raise Refused('--scenario is only valid with --plan')
                    runner = Runner(source, venv, head, context, emit, out.parent)
                    runner.run(plan)
                    if runner.failed:
                        code, message = 1, 'NOT GREEN, FAILURES'
                    elif not runner.ran:
                        code, message = 4, 'NO STEPS RAN'
                    elif runner.not_run:
                        code, message = 0, 'ran steps passed; GREEN OVERALL: every NOT RUN step is on the expected-skip list above'
                    elif args.jobs or args.workflows:
                        code, message = 0, 'selected checks passed; NOT GREEN OVERALL: scoped run; environment deltas apply'
                    else:
                        code, message = 0, 'all applicable steps ran and passed; environment deltas still apply'
        except expr.Unmapped as error:
            code, message = 5, 'UNMAPPED: ' + str(error)
            emit(message)
        except SelectionError as error:
            code, message = 4, 'REQUESTED ITEM NOT FOUND: ' + str(error)
            emit(message)
        except (Refused, OSError, subprocess.SubprocessError, ValueError) as error:
            code, message = 2, 'REFUSED: ' + str(error)
            emit(message)
        except KeyboardInterrupt:
            code, message = 1, 'NOT GREEN, CANCELLED'
            emit(message)
        if runner:
            computed = {workflow: {job: value['outputs'] for job, value in jobs.items()}
                        for workflow, jobs in runner.results.items()}
            output_line = 'computed outputs (completed jobs): ' + json.dumps(computed, sort_keys=True)
            lines = [output_line if line.startswith('computed outputs:') else line for line in lines]
            report.seek(0)
            report.truncate()
            report.write('\n'.join(lines) + '\n')
            report.flush()
            emit(output_line)
        ran, failed, not_run = (runner.ran, runner.failed, runner.not_run) if runner else (0, 0, unexpected)
        emit(f'SUMMARY ran={ran} failed={failed} not_run={not_run} not_run_unexpected={unexpected} RESULT: {message}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
