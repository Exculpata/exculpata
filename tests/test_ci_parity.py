"""Offline contracts for the workflow-derived local runner."""

from pathlib import Path

import pytest

from scripts import ci_parity_expr as expr
from scripts import ci_parity_plan as plan


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('source', 'expected'), [
    ("'TRUE' == 'true'", True),
    ("'2' == 2", True),
    ("null == false", True),
    ("'no-number' < 2", False),
    ("false || 'fallback'", 'fallback'),
    ("true && 'value'", 'value'),
    ("!false && (2 >= 1) && (1 <= 2)", True),
    ("contains(fromJSON('[\"A\",\"B\"]'), 'a')", True),
    ("startsWith('Synthetic', 'SYN') && endsWith('Synthetic', 'TIC')", True),
    ("format('{{{0}}}', 'value')", '{value}'),
    ("join(fromJSON('[1,2]'), ':')", '1:2'),
    ("contains(github.event.pull_request.labels.*.name, 'CHECK')", True),
])
def test_expression_semantics(source, expected):
    context = {'github': {'event': {'pull_request': {'labels': [{'name': 'check'}]}}}}
    assert expr.evaluate(source, context) == expected


@pytest.mark.parametrize('source', [
    'secrets.TOKEN', 'unknown()', 'github.foo + 1', '__import__(\'os\')',
    'true || unknown()', 'false && secrets.TOKEN', 'format(\'{0.__class__}\', 1)',
])
def test_expression_fails_closed(source):
    with pytest.raises(expr.Unmapped):
        expr.evaluate(source, {})


def test_f2_default_success_and_conditions():
    context = {'needs': {'scope': {'result': 'failure', 'outputs': {'docs_only': 'true'}}}}
    assert not expr.condition("needs.scope.outputs.docs_only != 'true'", context, 'success')
    assert not expr.condition('true', context, 'failure')
    assert expr.condition('always()', context, 'failure')
    assert expr.condition('failure() && !cancelled()', context, 'failure')


def synthetic_workflow(tmp_path, document):
    import yaml

    directory = tmp_path / '.github' / 'workflows'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'synthetic.yml'
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def document(steps=None, **job_values):
    return {'on': {'pull_request': None}, 'jobs': {
        'build': {'runs-on': 'ubuntu-24.04', 'steps': steps or [{'run': 'true'}], **job_values},
    }}


def test_f9_unknown_key_prevents_whole_plan(tmp_path):
    synthetic_workflow(tmp_path, document(container='synthetic'))
    with pytest.raises(expr.Unmapped, match='synthetic:build.*container'):
        plan.build(tmp_path)


def test_matrix_product_and_needs_order(tmp_path):
    data = document(strategy={'matrix': {'axis': [0, 1, 2], 'other': ['a', 'b']}})
    data['jobs'] = {'after': {'needs': 'build', 'steps': [{'run': 'true'}]}, **data['jobs']}
    synthetic_workflow(tmp_path, data)
    workflow = plan.build(tmp_path).workflows[0]
    assert [job.name for job in workflow.jobs] == ['build', 'after']
    assert len(workflow.jobs[0].cells) == 6


@pytest.mark.parametrize('matrix', [{'include': [{}]}, {'exclude': [{}]}, {'axis': 'value'}])
def test_matrix_unknown_forms(tmp_path, matrix):
    synthetic_workflow(tmp_path, document(strategy={'matrix': matrix}))
    with pytest.raises(expr.Unmapped, match='matrix'):
        plan.build(tmp_path)


def test_new_action_fails_but_sha_change_is_free(tmp_path):
    synthetic_workflow(tmp_path, document([{'uses': 'actions/checkout@new-ref'}]))
    plan.build(tmp_path)
    synthetic_workflow(tmp_path, document([{'uses': 'synthetic/new-action@pin'}]))
    with pytest.raises(expr.Unmapped, match='action'):
        plan.build(tmp_path)


@pytest.mark.parametrize(('level', 'key'), [('workflow', 'unknown'), ('job', 'container'), ('step', 'unknown')])
def test_all_key_levels_fail_closed(tmp_path, level, key):
    data = document()
    target = data if level == 'workflow' else data['jobs']['build'] if level == 'job' else data['jobs']['build']['steps'][0]
    target[key] = 'synthetic'
    synthetic_workflow(tmp_path, data)
    with pytest.raises(expr.Unmapped, match=key):
        plan.build(tmp_path)


def test_entire_plan_is_validated_even_unselected_and_false_steps(tmp_path):
    data = document([{'run': 'true'}, {'if': False, 'uses': 'synthetic/unknown@v1'}])
    synthetic_workflow(tmp_path, data)
    with pytest.raises(expr.Unmapped, match='unknown action'):
        plan.build(tmp_path, jobs=['absent'])


@pytest.mark.parametrize(('filters', 'files', 'branch', 'expected'), [
    ({'paths': ['docs/**', '!docs/private/**']}, ['docs/a.md'], 'main', True),
    ({'paths': ['docs/**', '!docs/private/**']}, ['docs/private/a.md'], 'main', False),
    ({'paths': ['*.md']}, ['docs/a.md'], 'main', False),
    ({'paths': ['**/*.md']}, ['a.md'], 'main', True),
    ({'branches': ['release/*']}, ['a'], 'release/one', True),
    ({'branches-ignore': ['main']}, ['a'], 'main', False),
    ({'types': ['opened']}, ['a'], 'main', False),
    ({'paths-ignore': ['docs/**']}, ['docs/a.md', 'src/a.py'], 'main', True),
])
def test_trigger_filters(tmp_path, filters, files, branch, expected):
    data = document()
    data['on']['pull_request'] = filters
    synthetic_workflow(tmp_path, data)
    assert plan.build(tmp_path, changed_files=files, branch=branch).workflows[0].triggered is expected


@pytest.mark.parametrize('filters', [{'paths': ['[ab]']}, {'unknown': []}, {'paths': ['a'], 'paths-ignore': ['b']}])
def test_unknown_filters_fail(tmp_path, filters):
    data = document()
    data['on']['pull_request'] = filters
    synthetic_workflow(tmp_path, data)
    with pytest.raises(expr.Unmapped):
        plan.build(tmp_path, changed_files=['a'])


def test_discovery_and_event_exclusion(tmp_path):
    synthetic_workflow(tmp_path, document())
    path = tmp_path / '.github/workflows/nightly.yaml'
    path.write_text('on: [schedule]\njobs: {}\n')
    found = plan.build(tmp_path)
    assert len(found.workflows) == 2
    assert not found.workflows[0].triggered
    assert found.workflows[0].triggers == ['schedule']
    with pytest.raises(expr.Unmapped, match='unsupported event'):
        plan.build(tmp_path, event='push')


def test_duplicate_yaml_and_needs_cycles(tmp_path):
    path = synthetic_workflow(tmp_path, document())
    path.write_text('on: pull_request\njobs: {}\njobs: {}\n')
    with pytest.raises(expr.Unmapped, match='duplicate key'):
        plan.build(tmp_path)
    synthetic_workflow(tmp_path, document(needs='build'))
    with pytest.raises(expr.Unmapped, match='cyclic'):
        plan.build(tmp_path)


def test_action_inputs_and_matrix_values_fail_closed(tmp_path):
    synthetic_workflow(tmp_path, document([{'uses': 'actions/checkout@pin', 'with': {'sparse-checkout': 'src'}}]))
    with pytest.raises(expr.Unmapped, match='sparse-checkout'):
        plan.build(tmp_path)


def git_commit(root, message='synthetic'):
    import subprocess

    for args in (['add', '.'], ['-c', 'user.name=Synthetic Contributor', '-c',
                               'user.email=synthetic@example.invalid', 'commit', '-qm', message]):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, timeout=30)
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()


def repository(tmp_path, data):
    import subprocess

    root = tmp_path / 'repo'
    synthetic_workflow(root, data)
    (root / 'pyproject.toml').write_text('[project.optional-dependencies]\ndev = []\n')
    subprocess.run(['git', 'init', '-q', str(root)], check=True, timeout=30)
    head = git_commit(root)
    return root, head


def run_repository(tmp_path, data):
    import sys
    from scripts.ci_parity_runner import Runner, base_context

    root, head = repository(tmp_path, data)
    messages = []
    runner = Runner(root, Path(sys.executable).parent.parent, head,
                    base_context(head, head, 'synthetic', 'main'), messages.append, tmp_path)
    runner.run(plan.build(root))
    return runner, messages, root


def test_f4_cells_have_fresh_checkout_temp_and_file_commands(tmp_path):
    command = '''test ! -e state
printf owned > state
mkdir "$RUNNER_TEMP/application-workers"
test "$RUNNER_TEMP" = "$TMPDIR"
test ! -e .venv
printf '%s\\n' "$GITHUB_OUTPUT" "$GITHUB_ENV" "$GITHUB_PATH" "$GITHUB_STEP_SUMMARY"
printf 'value=cell\\n' >> "$GITHUB_OUTPUT"
'''
    data = document([{'id': 'proof', 'run': command}], strategy={'matrix': {'cell': [0, 1]}})
    runner, messages, root = run_repository(tmp_path, data)
    assert runner.failed == 0
    assert runner.ran == 2
    cells = [line for line in messages if line.startswith('CELL ')]
    assert len(cells) == 2 and cells[0].split('checkout=')[1] != cells[1].split('checkout=')[1]
    paths = [log.read_text().splitlines() for log in sorted(tmp_path.glob('parity-*.log'))]
    assert len(set(paths[0] + paths[1])) == 8
    assert not (root / 'state').exists()
    for line in cells:
        assert not Path(line.split('checkout=')[1].split(' RUNNER_TEMP=')[0]).exists()


@pytest.mark.parametrize('exit_code', [0, 1])
def test_cell_storage_belongs_to_report_directory_and_is_reclaimed(tmp_path, exit_code):
    import subprocess

    command = '''printf '%s\\n' "$GITHUB_WORKSPACE" "$RUNNER_TEMP" "$TMPDIR" \
"$GITHUB_EVENT_PATH" "$GITHUB_OUTPUT" "$GITHUB_ENV" "$GITHUB_PATH" "$GITHUB_STEP_SUMMARY"
mkdir "$RUNNER_TEMP/application-workers"
exit ''' + str(exit_code)
    data = document([{'run': command}],
                    strategy={'fail-fast': False, 'matrix': {'cell': [0, 1]}})
    root, _ = repository(tmp_path, data)
    output = tmp_path / 'reports'
    output.mkdir()
    result, report = cli(root, output)
    assert result.returncode == exit_code, report
    assert f'ran=2 failed={2 * exit_code}' in report
    logs = sorted(output.glob('parity-*.log'))
    assert len(logs) == 2
    for log in logs:
        paths = [Path(line) for line in log.read_text().splitlines()]
        assert len(paths) == 8
        assert all(path.is_relative_to(output) for path in paths)
        assert paths[1] == paths[2]
        assert all(not path.exists() for path in paths)
        assert not paths[0].parent.exists()
    assert not list(output.glob('ci-parity-*'))
    registered = subprocess.check_output(['git', 'worktree', 'list', '--porcelain'], cwd=root, text=True)
    assert registered.count('worktree ') == 1


def test_outputs_env_path_and_real_needs_plumbing(tmp_path):
    data = document([
        {'id': 'first', 'run': '''printf 'value<<END\\nfirst\\nsecond\\nEND\\n' >> "$GITHUB_OUTPUT"
printf 'EXAMPLE=synthetic\\n' >> "$GITHUB_ENV"
mkdir "$RUNNER_TEMP/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$RUNNER_TEMP/bin/synthetic-command"
chmod +x "$RUNNER_TEMP/bin/synthetic-command"
printf '%s\\n' "$RUNNER_TEMP/bin" >> "$GITHUB_PATH"
'''},
        {'run': 'test "$EXAMPLE" = synthetic\nsynthetic-command\ntest "${{ steps.first.outputs.value }}" = "first\nsecond"'},
    ], outputs={'value': '${{ steps.first.outputs.value }}'})
    data['jobs']['aggregate'] = {'needs': 'build', 'steps': [
        {'env': {'UPSTREAM': '${{ needs.build.result }}', 'VALUE': '${{ needs.build.outputs.value }}'},
         'run': 'test "$UPSTREAM" = success\ntest "$VALUE" = "first\nsecond"'},
    ]}
    runner, _, _ = run_repository(tmp_path, data)
    assert runner.failed == 0 and runner.ran == 3
    assert runner.results['synthetic']['build']['outputs'] == {'value': 'first\nsecond'}


def test_failure_default_success_always_and_continue_on_error(tmp_path):
    data = document([
        {'id': 'bad', 'run': 'false'},
        {'run': 'exit 7'},
        {'if': 'failure()', 'run': 'test "${{ steps.bad.outcome }}" = failure'},
        {'if': 'always()', 'run': 'true'},
    ])
    data['jobs']['ordinary'] = {'needs': 'build', 'steps': [{'run': 'exit 9'}]}
    data['jobs']['aggregate'] = {'needs': 'build', 'if': 'always()', 'steps': [
        {'env': {'RESULT': '${{ needs.build.result }}'}, 'run': 'test "$RESULT" = failure'},
        {'id': 'tolerated', 'continue-on-error': True, 'run': 'false'},
        {'run': 'test "${{ steps.tolerated.outcome }}" = failure\ntest "${{ steps.tolerated.conclusion }}" = success'},
    ]}
    runner, messages, _ = run_repository(tmp_path, data)
    assert runner.failed == 1 and runner.ran == 6
    assert runner.results['synthetic']['ordinary']['result'] == 'skipped'
    assert runner.results['synthetic']['aggregate']['result'] == 'success'
    assert any('SKIP (condition)' in message for message in messages)


@pytest.mark.parametrize(('shell', 'failed'), [(None, 0), ('bash', 1)])
def test_shell_semantics(tmp_path, shell, failed):
    step = {'run': 'false | cat'}
    if shell:
        step['shell'] = shell
    runner, _, _ = run_repository(tmp_path, document([step]))
    assert runner.failed == failed


def test_default_shell_exits_on_first_failure(tmp_path):
    runner, _, _ = run_repository(tmp_path, document([{'run': 'false\necho incorrect'}]))
    assert runner.failed == 1
    assert 'incorrect' not in next(tmp_path.glob('parity-*.log')).read_text()


@pytest.mark.parametrize('scope', ['step', 'job'])
def test_timeouts_cleanup_and_descendant_termination(tmp_path, scope):
    import os
    import time

    data = document([{'run': 'sleep 20 &\necho $!\nwait'}])
    target = data['jobs']['build']['steps'][0] if scope == 'step' else data['jobs']['build']
    target['timeout-minutes'] = 0.003
    start = time.monotonic()
    runner, messages, _ = run_repository(tmp_path, data)
    assert time.monotonic() - start < 10
    assert runner.failed == 1
    log = next(tmp_path.glob('parity-*.log')).read_text()
    assert 'TIMEOUT' in log
    pid = int(log.splitlines()[0])
    status_path = Path('/proc') / str(pid) / 'stat'
    assert not status_path.exists() or status_path.read_text().split()[2] == 'Z'
    assert any('timeout=True' in message for message in messages)
    assert os.getpid() != pid


def test_file_commands_fail_on_unknown_or_incomplete(tmp_path):
    from scripts.ci_parity_runner import file_commands

    path = tmp_path / 'output'
    for text in ('value<<END\nmissing end\n', 'invalid\n'):
        path.write_text(text)
        with pytest.raises(expr.Unmapped, match='file command|multiline'):
            file_commands(path)


def test_environment_caps_workers_without_rewriting_command(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTEST_ADDOPTS', 'unexpected')
    monkeypatch.setenv('CASE_REVIEW_POSTGRES_TEST_DSN', 'synthetic-unavailable')
    data = document([{'env': {'PYTEST_XDIST_AUTO_NUM_WORKERS': '6'}, 'run': '''test "$PYTEST_XDIST_AUTO_NUM_WORKERS" = 4
test -z "${PYTEST_ADDOPTS+x}"
test -z "${CASE_REVIEW_POSTGRES_TEST_DSN+x}"
.venv/bin/python -c 'import sys; print(sys.version_info[:2])'
printf '%s\\n' 'pytest -n auto'
'''}])
    runner, _, _ = run_repository(tmp_path, data)
    assert runner.failed == 0
    assert 'pytest -n auto' in next(tmp_path.glob('parity-*.log')).read_text()


def write_skips(root, entries):
    import json

    path = root / 'scripts/ci-parity-expected-skips.toml'
    path.parent.mkdir(exist_ok=True)
    path.write_text('\n'.join('\n[[skip]]\n' + '\n'.join(f'{key} = {json.dumps(value)}' for key, value in row.items()) for row in entries))


def skip_entry(step='Install synthetic'):
    return {'workflow': 'synthetic', 'job': 'build', 'step': step,
            'category': 'install', 'reason': 'Synthetic unavailable installation.'}


def test_skips_are_exact_duplicate_stale_and_unlisted(tmp_path):
    synthetic_workflow(tmp_path, document([{'name': 'Install synthetic', 'run': 'python -m pip install synthetic'}]))
    assert len(plan.build(tmp_path).unexpected) == 1
    write_skips(tmp_path, [skip_entry()])
    assert not plan.build(tmp_path).unexpected
    write_skips(tmp_path, [skip_entry(), skip_entry()])
    with pytest.raises(expr.Unmapped, match='duplicate expected skip'):
        plan.build(tmp_path)
    write_skips(tmp_path, [skip_entry('Old name')])
    found = plan.build(tmp_path)
    assert found.stale == ['synthetic:build:Old name']
    assert len(found.unexpected) == 1


def test_local_composite_is_loud_until_explicitly_unavailable(tmp_path):
    synthetic_workflow(tmp_path, document([{'name': 'Packages', 'uses': './.github/actions/synthetic'}]))
    action_dir = tmp_path / '.github/actions/synthetic'
    action_dir.mkdir(parents=True)
    (action_dir / 'action.yml').write_text('runs:\n  using: composite\n  steps:\n    - run: true\n')
    with pytest.raises(expr.Unmapped, match='local composite'):
        plan.build(tmp_path)
    write_skips(tmp_path, [skip_entry('Packages')])
    assert not plan.build(tmp_path).unexpected


def test_preflight_names_exact_missing_pin_and_python(tmp_path):
    import sys
    from scripts.ci_parity_environment import Refused, preflight

    (tmp_path / 'pyproject.toml').write_text('[project.optional-dependencies]\ndev = ["synthetic-unavailable-package==12.34.56"]\n')
    with pytest.raises(Refused) as failure:
        preflight(tmp_path, Path(sys.executable).parent.parent, {'1.2'})
    assert 'synthetic-unavailable-package==12.34.56' in str(failure.value)
    assert 'Python==1.2' in str(failure.value)


@pytest.mark.parametrize(('actual', 'constraint', 'expected'), [
    ('3.8.0', '>=3.8,<4', True), ('4.0', '>=3.8,<4', False),
    ('0.17.3', '==0.17.3', True), ('0.17.4', '==0.17.3', False),
    ('6.0.3', '>=6,<7', True),
])
def test_preflight_dependency_constraints(actual, constraint, expected):
    from scripts.ci_parity_environment import satisfies

    assert satisfies(actual, constraint) is expected


def cli(root, tmp_path, *args):
    import subprocess
    import sys

    out = tmp_path / 'report.txt'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci_parity.py'), str(root), str(out),
                             str(Path(sys.executable).parent.parent), '--base-ref', 'HEAD', *args],
                            capture_output=True, text=True, timeout=30)
    report = out.read_text()
    assert result.stdout.splitlines()[-1].startswith('SUMMARY '), result.stderr
    assert result.stdout.splitlines()[-1] == report.splitlines()[-1]
    return result, report


@pytest.mark.parametrize(('data', 'arguments', 'exit_code', 'needle'), [
    (document(), [], 0, 'ran=1 failed=0'),
    (document([{'run': 'false'}]), [], 1, 'NOT GREEN, FAILURES'),
    (document([{'run': 'pip install synthetic'}]), [], 3, 'UNEXPECTED'),
    (document(), ['--jobs', 'absent'], 4, 'REQUESTED ITEM NOT FOUND'),
    (document(), ['--workflows', 'absent'], 4, 'REQUESTED ITEM NOT FOUND'),
    (document([{'run': 'true', 'unknown': 1}]), [], 5, 'unknown'),
    (document([{'run': 'true', 'if': False}]), [], 4, 'NO STEPS RAN'),
    (document(), ['--event', 'push'], 5, 'unsupported event'),
    (document(), ['--plan'], 0, 'PLAN ONLY'),
])
def test_cli_contract(tmp_path, data, arguments, exit_code, needle):
    root, _ = repository(tmp_path, data)
    result, report = cli(root, tmp_path, *arguments)
    assert result.returncode == exit_code, report
    assert needle in report


def test_dirty_tree_refusal_and_preflight_precede_commands(tmp_path):
    root, _ = repository(tmp_path, document([{'run': 'echo should-not-run'}]))
    (root / 'dirty').touch()
    result, _ = cli(root, tmp_path)
    assert result.returncode == 2
    (root / 'dirty').unlink()
    (root / 'pyproject.toml').write_text('[project.optional-dependencies]\ndev = ["synthetic-missing==123.0"]\n')
    git_commit(root)
    result, report = cli(root, tmp_path)
    assert result.returncode == 2
    assert 'synthetic-missing==123.0' in report
    assert not list(tmp_path.glob('parity-*.log'))


def test_unmapped_late_step_executes_nothing(tmp_path):
    root, _ = repository(tmp_path, document([{'run': 'echo should-not-run'}, {'run': 'true', 'if': 'unsupported()'}]))
    result, report = cli(root, tmp_path)
    assert result.returncode == 5
    assert 'synthetic:build' in report
    assert not list(tmp_path.glob('parity-*.log'))


def test_expected_skip_is_visible_and_never_called_green(tmp_path):
    root, _ = repository(tmp_path, document([{'run': 'true'}, {'name': 'Install synthetic', 'run': 'pip install synthetic'}]))
    write_skips(root, [skip_entry()])
    git_commit(root)
    result, report = cli(root, tmp_path)
    assert result.returncode == 0, report
    assert 'NOT GREEN OVERALL' in report and 'not_run=1' in report
    assert 'EXPECTED NOT RUN: synthetic:build:Install synthetic' in report


def test_job_selection_includes_needs(tmp_path):
    data = document()
    data['jobs']['after'] = {'needs': 'build', 'steps': [{'run': 'true'}]}
    synthetic_workflow(tmp_path, data)
    assert [job.name for job in plan.build(tmp_path, jobs=['after']).workflows[0].jobs] == ['build', 'after']


def test_repository_drift_and_hosted_discovery():
    import subprocess
    import sys
    from scripts.ci_parity import validate_bindings
    from scripts.ci_parity_runner import base_context, git

    found = plan.build(ROOT)
    head = git(ROOT, 'rev-parse', 'HEAD')
    validate_bindings(found, base_context(head, head, 'synthetic', 'main'))
    assert not found.stale, 'Stale exact expected skips: ' + ', '.join(found.stale)
    assert not found.unexpected, 'Unmapped local capability: ' + ', '.join(found.unexpected)
    files = []
    for shard in ('0', '1'):
        files.extend(subprocess.check_output([sys.executable, str(ROOT / 'scripts/application-test-shard.py'), shard], text=True).splitlines())
    assert files.count('tests/test_ci_parity.py') == 1


def real_scope_outputs(tmp_path, found, changed_path):
    import json
    import shutil
    import subprocess
    import sys
    from scripts.ci_parity_environment import local_environment
    from scripts.ci_parity_runner import base_context, file_commands

    root, base = repository(tmp_path, document())
    (root / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/classify-quality-change.py', root / 'scripts/classify-quality-change.py')
    base = git_commit(root)
    changed = root / changed_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text('Synthetic changed content\n')
    head = git_commit(root)
    context = base_context(head, base, 'synthetic', 'main')
    event_path = tmp_path / 'event.json'
    event_path.write_text(json.dumps(context['github']['event']))
    env = local_environment(root, tmp_path, Path(sys.executable).parent.parent, head, 'synthetic', event_path)
    outputs = {}
    for workflow in found.workflows:
        for job in workflow.jobs:
            for step in job.steps:
                if 'id' not in step.raw:
                    continue
                assert step.unavailable is None, 'A scenario cannot invent outputs from an unavailable step'
                identity = ':'.join((workflow.name, job.name, step.raw['id']))
                output_path = tmp_path / (identity.replace(':', '-') + '.output')
                output_path.touch()
                step_env = {**env, 'GITHUB_OUTPUT': str(output_path)}
                step_env.update({name: expr.render(value, context) for name, value in step.raw.get('env', {}).items()})
                subprocess.run(['bash', '-e', '-c', expr.render(step.raw['run'], context)], cwd=root,
                               env=step_env, check=True, capture_output=True, text=True, timeout=30)
                outputs[identity] = file_commands(output_path)
    return outputs, context


def test_golden_plan(tmp_path):
    import json
    import os
    from scripts.ci_parity import scenario_plan

    found = plan.build(ROOT)
    scenarios = {}
    for name, changed_path in [('docs', 'docs/synthetic.md'), ('code', 'src/synthetic.py'), ('browser', 'src/synthetic/static/synthetic.js')]:
        directory = tmp_path / name
        directory.mkdir()
        supplied, context = real_scope_outputs(directory, found, changed_path)
        scenarios[name] = {'supplied': supplied, 'plan': scenario_plan(found, context, supplied)}
    value = json.loads(json.dumps({'definition': found.snapshot(), 'scenarios': scenarios}))
    path = ROOT / 'tests/snapshots/ci_parity_plan.json'
    command = 'CI_PARITY_UPDATE_SNAPSHOT=1 python -m pytest -q tests/test_ci_parity.py::test_golden_plan'
    if os.environ.get('CI_PARITY_UPDATE_SNAPSHOT') == '1':
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    assert path.exists(), 'Regenerate reviewed workflow plan: ' + command
    assert value == json.loads(path.read_text()), 'Workflow plan drift; inspect mappings and regenerate: ' + command
    docs = scenarios['docs']['plan']
    code = scenarios['code']['plan']
    # This asserts the reported Pilot failure directly, using real classifier outputs.
    for rows, expected in ((docs, 'SKIP'), (code, 'RUN')):
        application = [step['state'] for row in rows for step in row['steps'] if step['step'] == 'Run application suite']
        assert application == [expected, expected]
    assert all('scope' not in step['step'].lower() or step['state'] != 'NOT RUN'
               for row in docs for step in row['steps'])


def test_command_change_alters_snapshot_but_action_pin_does_not(tmp_path):
    path = synthetic_workflow(tmp_path, document([{'uses': 'actions/checkout@old'}, {'run': 'echo one'}]))
    before = plan.build(tmp_path).snapshot()
    path.write_text(path.read_text().replace('@old', '@new'))
    assert plan.build(tmp_path).snapshot() == before
    path.write_text(path.read_text().replace('echo one', 'echo two'))
    assert plan.build(tmp_path).snapshot() != before


def test_real_classifier_controls_runner_docs_steps(tmp_path):
    import shutil
    import sys
    from scripts.ci_parity_runner import Runner, base_context

    data = document([{'id': 'scope', 'run': 'python3 scripts/classify-quality-change.py'}],
                    outputs={'docs_only': '${{ steps.scope.outputs.docs_only }}'})
    data['jobs']['suite'] = {'needs': 'build', 'strategy': {'matrix': {'part': [0, 1]}}, 'steps': [
        {'if': "needs.build.outputs.docs_only != 'true'", 'run': 'exit 42'},
        {'if': "needs.build.outputs.docs_only == 'true'", 'run': 'true'},
    ]}
    root, _ = repository(tmp_path, data)
    (root / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/classify-quality-change.py', root / 'scripts/classify-quality-change.py')
    base = git_commit(root)
    (root / 'docs').mkdir()
    (root / 'docs/synthetic.md').write_text('Synthetic documentation\n')
    head = git_commit(root)
    messages = []
    runner = Runner(root, Path(sys.executable).parent.parent, head,
                    base_context(head, base, 'synthetic', 'main'), messages.append, tmp_path)
    runner.run(plan.build(root))
    assert runner.ran == 3 and runner.failed == 0
    assert runner.results['synthetic']['build']['outputs']['docs_only'] == 'true'


def test_checkout_mapping_rejected_before_any_command(tmp_path):
    root, _ = repository(tmp_path, document([{'run': 'echo wrong'}, {'uses': 'actions/checkout@pin', 'with': {'ref': 'unsupported-ref'}}]))
    result, report = cli(root, tmp_path)
    assert result.returncode == 5
    assert 'checkout ref' in report
    assert not list(tmp_path.glob('parity-*.log'))


def test_context_fields_are_closed_even_in_false_branch(tmp_path):
    synthetic_workflow(tmp_path, document([{'run': 'true', 'if': "false && runner.unsupported == 'value'"}]))
    with pytest.raises(expr.Unmapped, match='context field'):
        plan.build(tmp_path)


def test_isolated_cell_cleans_after_exception(tmp_path):
    import subprocess
    from scripts.ci_parity_runner import isolated_cell

    root, head = repository(tmp_path, document())
    with pytest.raises(RuntimeError, match='synthetic'):
        with isolated_cell(root, head, tmp_path) as (tree, temporary):
            raise RuntimeError('synthetic interruption')
    assert not tree.exists() and not temporary.exists()
    registered = subprocess.check_output(['git', 'worktree', 'list', '--porcelain'], cwd=root, text=True)
    assert registered.count('worktree ') == 1


def test_matrix_fail_fast_is_honored(tmp_path):
    data = document([{'run': 'false'}], strategy={'matrix': {'part': [0, 1]}})
    runner, messages, _ = run_repository(tmp_path, data)
    assert runner.ran == 1 and runner.failed == 1
    assert any('matrix fail-fast' in line for line in messages)


def test_non_fail_fast_runs_every_cell(tmp_path):
    data = document([{'run': 'false'}], strategy={'fail-fast': False, 'matrix': {'part': [0, 1]}})
    runner, _, _ = run_repository(tmp_path, data)
    assert runner.ran == 2 and runner.failed == 2


def test_renamed_skip_reports_both_unlisted_and_stale(tmp_path):
    root, _ = repository(tmp_path, document([{'name': 'New name', 'run': 'pip install synthetic'}]))
    write_skips(root, [skip_entry('Old name')])
    git_commit(root)
    result, report = cli(root, tmp_path)
    assert result.returncode == 3
    assert 'NOT RUN (UNEXPECTED): synthetic:build:New name' in report
    assert 'STALE EXPECTED SKIPS: synthetic:build:Old name' in report


def test_duplicate_step_names_cannot_share_one_skip(tmp_path):
    step = {'name': 'Install synthetic', 'run': 'pip install synthetic'}
    synthetic_workflow(tmp_path, document([step, step.copy()]))
    write_skips(tmp_path, [skip_entry()])
    with pytest.raises(expr.Unmapped, match='more than one step'):
        plan.build(tmp_path)


def test_invalid_repository_still_finishes_with_summary(tmp_path):
    root = tmp_path / 'absent'
    result, report = cli(root, tmp_path)
    assert result.returncode == 2
    assert 'REFUSED' in report


def test_report_header_records_real_completed_outputs(tmp_path):
    root, _ = repository(tmp_path, document([{'id': 's', 'run': 'echo value=synthetic >> "$GITHUB_OUTPUT"'}],
                                          outputs={'value': '${{ steps.s.outputs.value }}'}))
    result, report = cli(root, tmp_path)
    assert result.returncode == 0, report
    header = report.split('ENVIRONMENT DELTA:')[0]
    assert '"value": "synthetic"' in header


def test_workflow_env_is_honored_after_inherited_env_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTEST_ADDOPTS', 'inherited-must-not-leak')
    data = document([{'env': {'PYTEST_ADDOPTS': 'explicit-synthetic'},
                      'run': 'test "$PYTEST_ADDOPTS" = explicit-synthetic'}])
    runner, _, _ = run_repository(tmp_path, data)
    assert runner.failed == 0


def test_service_context_cannot_silently_be_empty(tmp_path):
    synthetic_workflow(tmp_path, document([{'run': 'echo "${{ job.services.synthetic.id }}"'}]))
    found = plan.build(tmp_path)
    assert len(found.unexpected) == 1
    assert 'service context' in found.unexpected[0]


def test_matrix_outputs_follow_actual_local_completion_order(tmp_path):
    data = document([{'id': 's', 'run': 'echo part=${{ matrix.part }} >> "$GITHUB_OUTPUT"'}],
                    strategy={'matrix': {'part': [0, 1]}}, outputs={'part': '${{ steps.s.outputs.part }}'})
    data['jobs']['after'] = {'needs': 'build', 'steps': [
        {'run': 'test "${{ needs.build.outputs.part }}" = 1'},
    ]}
    runner, _, _ = run_repository(tmp_path, data)
    assert runner.failed == 0 and runner.ran == 3


@pytest.mark.parametrize('value', [0, -1, float('inf'), float('nan'), '${{ matrix.timeout }}'])
def test_timeout_unknown_values_fail_closed(tmp_path, value):
    synthetic_workflow(tmp_path, document(**{'timeout-minutes': value}))
    with pytest.raises(expr.Unmapped, match='timeout-minutes'):
        plan.build(tmp_path)


def test_missing_parser_dependency_still_reports_preflight_failure(tmp_path):
    import subprocess
    import sys

    root, _ = repository(tmp_path, document())
    out = tmp_path / 'report.txt'
    result = subprocess.run([sys.executable, '-S', str(ROOT / 'scripts/ci_parity.py'),
                             str(root), str(out), str(Path(sys.executable).parent.parent),
                             '--base-ref', 'HEAD'], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stderr
    assert result.stdout.splitlines()[-1].startswith('SUMMARY ')
    assert 'PyYAML' in out.read_text()


def test_object_matrix_axes_flow_to_step_expressions(tmp_path):
    data = document([{'run': 'test "${{ matrix.config.version }}" = synthetic'}],
                    strategy={'matrix': {'config': [{'version': 'synthetic'}], 'repeat': [0, 1]}})
    runner, _, _ = run_repository(tmp_path, data)
    assert runner.ran == 2 and runner.failed == 0


def test_preflight_rejects_unknown_later_constraint():
    from scripts.ci_parity_environment import satisfies

    with pytest.raises(expr.Unmapped, match='unsupported dependency constraint'):
        satisfies('0', '>=1,~=2')
