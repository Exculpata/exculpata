"""Synthetic duration reports and the runner's failure propagation."""

import pytest
import yaml

from scripts.ci_parity_durations import NEAR_LIMIT_FRACTION, near_limit
from tests.test_ci_parity import ROOT, document, run_repository


HEADER = '============================= slowest 25 durations ============================='
REBUILD_ID = 'tests/test_discovery_review_index.py::test_rebuild_step_row_budget[10000]'
RESUME_ID = ('tests/test_discovery_review_index_lifecycle.py::'
             'test_single_heavily_coded_item_stays_pending_with_complete_values_on_resume')


def test_accepted_exceptions_are_exactly_the_approved_literal_table():
    import ast

    from scripts import ci_parity_durations

    expected = {REBUILD_ID: (135, '2026-11-15T15:24'), RESUME_ID: (125, '2026-11-15T17:17')}
    assert ci_parity_durations.ACCEPTED_EXCEPTIONS == expected
    tree = ast.parse((ROOT / 'scripts/ci_parity_durations.py').read_text())
    assignment, = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'ACCEPTED_EXCEPTIONS'
                           for target in node.targets)]
    assert ast.literal_eval(assignment.value) == expected
    with pytest.raises(TypeError):
        near_limit('pytest --timeout=360', HEADER, exceptions={})


@pytest.mark.parametrize('test_id,ceiling,approval,observed', [
    (REBUILD_ID, 135, '2026-11-15T15:24', 125.77),
    (RESUME_ID, 125, '2026-11-15T17:17', 120.41),
])
def test_accepted_exception_ceiling_and_phase_totals(test_id, ceiling, approval, observed):
    for seconds in (1, observed, ceiling, ceiling + 0.01):
        messages = []
        log = f'{HEADER}\n0.25s setup {test_id}\n{seconds - 0.5:.2f}s call {test_id}\n' \
              f'0.25s teardown {test_id}\n'
        result = near_limit('pytest --timeout=360', log, step_tag='synthetic', emit=messages.append)
        if seconds <= ceiling:
            assert result == []
            assert messages == [f'ACCEPTED EXCEPTION synthetic: {test_id} took {seconds:.2f}s '
                                f'of 360s (ceiling {ceiling}s, approval {approval})']
        else:
            assert result == [(test_id, seconds)]
            assert messages == [f'NEAR TIMEOUT synthetic: {test_id} took {seconds:.2f}s '
                                f'of 360s (limit 0.30, ceiling {ceiling}s)']


@pytest.mark.parametrize('test_id', [
    REBUILD_ID.replace('[10000]', '[5000]'),
    REBUILD_ID.replace('[10000]', '[20000]'),
    REBUILD_ID.replace('test_discovery_review_index.py', 'other.py'),
    RESUME_ID.replace('test_discovery_review_index_lifecycle.py', 'other.py'),
    REBUILD_ID + '-x', RESUME_ID + '-x',
])
def test_accepted_exceptions_require_exact_full_id(test_id):
    messages = []
    assert near_limit('pytest --timeout=360', f'{HEADER}\n109s call {test_id}',
                      emit=messages.append) == [(test_id, 109.0)]
    assert messages == [f'NEAR TIMEOUT pytest: {test_id} took 109.00s of 360s (limit 0.30)']


@pytest.mark.parametrize('command', ['python -m pytest --timeout=360',
                                    'python -m pytest --timeout 360'])
def test_threshold_and_zero_setup(command):
    messages = []
    log = f'{HEADER}\n109.00s call     tests/x.py::test_slow[param]\n' \
          '0.00s setup    tests/x.py::test_slow[param]\n' \
          '107.00s call tests/x.py::test_fast\n108.00s call tests/x.py::test_boundary\n'
    assert NEAR_LIMIT_FRACTION == 0.30
    assert near_limit(command, log, emit=messages.append, step_tag='synthetic') == [
        ('tests/x.py::test_slow[param]', 109.0)]
    assert messages == ['NEAR TIMEOUT synthetic: tests/x.py::test_slow[param] took '
                        '109.00s of 360s (limit 0.30)']


def test_sums_all_phases_inside_duration_section():
    log = f'999.00s call tests/x.py::outside\n{HEADER}\n' \
          '50.00s setup tests/x.py::test_total\n50.00s call tests/x.py::test_total\n' \
          '10.00s teardown tests/x.py::test_total\n' \
          '=================== 1 passed ===================\n' \
          '999.00s call tests/x.py::outside\n'
    assert near_limit('pytest --timeout 360', log, emit=lambda message: None) == [
        ('tests/x.py::test_total', 110.0)]


def test_no_timeout_has_no_result_or_note():
    messages = []
    assert near_limit('pytest -q', HEADER + '\n999s call tests/x.py::t',
                      emit=messages.append) == []
    assert messages == []


@pytest.fixture
def application_command():
    workflow = yaml.safe_load((ROOT / '.github/workflows/quality-gates.yml').read_text())
    return next(step['run'] for step in workflow['jobs']['application-shards']['steps']
                if step.get('name') == 'Run application suite')


def test_application_command_timeout_is_found(application_command):
    messages = []
    assert near_limit(application_command, '1 passed', emit=messages.append) == []
    assert messages == ['DURATIONS UNCHECKED pytest: no durations section for pytest timeout 360s']


@pytest.mark.parametrize('seconds', [120, 107])
def test_application_command_duration_limit(application_command, seconds):
    messages = []
    result = near_limit(application_command, f'{HEADER}\n{seconds}s call tests/x.py::t',
                        emit=messages.append)
    assert result == ([('tests/x.py::t', 120.0)] if seconds == 120 else [])
    assert messages == (['NEAR TIMEOUT pytest: tests/x.py::t took 120.00s of 360s (limit 0.30)']
                        if seconds == 120 else [])


@pytest.mark.parametrize('command', [
    'pytest --timeout', 'pytest --timeout=', 'pytest --timeout unknown',
    'pytest --timeout=nan', 'pytest --timeout=inf', 'pytest --timeout=-1',
    'pytest --timeout-method=signal', 'pytest "--timeout=360',
])
def test_unparsable_timeout_is_explicitly_unchecked(command):
    messages = []
    assert near_limit(command, HEADER, emit=messages.append) == []
    assert len(messages) == 1 and messages[0].startswith('DURATIONS UNCHECKED pytest:')


def test_continuations_and_token_whitespace():
    command = 'pytest --time\\\nout " 360 "'
    assert near_limit(command, f'{HEADER}\n120s call tests/x.py::t', emit=lambda message: None) == [
        ('tests/x.py::t', 120.0)]


def test_zero_timeout_disables_limit():
    messages = []
    assert near_limit('pytest --timeout=0', f'{HEADER}\n999s call tests/x.py::t',
                      emit=messages.append) == []
    assert messages == []


def test_runner_rejects_real_application_command(tmp_path, monkeypatch, application_command):
    from scripts import ci_parity_runner

    def fake_shell(command, cwd, env, explicit_bash, timeout, log):
        log.write_text(f'{HEADER}\n120s call tests/x.py::t\n')
        return 0, False

    monkeypatch.setattr(ci_parity_runner, 'run_shell', fake_shell)
    # Resolve the workflow expressions without changing its shell command shape.
    command = application_command.replace('${{ runner.temp }}', 'synthetic-temp')
    runner, messages, _ = run_repository(tmp_path, document([{'run': command}]))
    assert runner.failed == 1
    assert runner.results['synthetic']['build']['result'] == 'failure'
    assert any(message.startswith('NEAR TIMEOUT ') and 'of 360s' in message for message in messages)

    for case, seconds, failed, result, note in [
        ('accepted', 120.41, 0, 'success', 'ACCEPTED EXCEPTION '),
        ('over-ceiling', 125.01, 1, 'failure', 'NEAR TIMEOUT '),
    ]:
        def exception_shell(command, cwd, env, explicit_bash, timeout, log):
            log.write_text(f'{HEADER}\n{seconds}s call {RESUME_ID}\n')
            return 0, False

        monkeypatch.setattr(ci_parity_runner, 'run_shell', exception_shell)
        case_path = tmp_path / case
        case_path.mkdir()
        runner, messages, _ = run_repository(case_path, document([{'run': command}]))
        assert runner.ran == 1 and runner.failed == failed
        assert runner.results['synthetic']['build']['result'] == result
        assert len([message for message in messages if message.startswith(note)]) == 1
        assert any(RESUME_ID in message and 'ceiling 125s' in message for message in messages)
        if not failed:
            assert not any(message.startswith('NEAR TIMEOUT ') for message in messages)


def test_timeout_without_section_is_explicitly_unchecked():
    messages = []
    assert near_limit('pytest --timeout=360', '1 passed', emit=messages.append,
                      step_tag='synthetic') == []
    assert len(messages) == 1 and messages[0].startswith('DURATIONS UNCHECKED synthetic:')


def test_quoted_timeout_color_and_parameter_spaces():
    log = f'\x1b[33m{HEADER}\x1b[0m\n\x1b[31m109.25s call tests/x.py::t[two words]\x1b[0m\n'
    assert near_limit('pytest --timeout "360";', log, emit=lambda message: None) == [
        ('tests/x.py::t[two words]', 109.25)]


def test_multiple_commands_use_the_strictest_timeout():
    assert near_limit('pytest --timeout=720; pytest --timeout 360',
                      HEADER + '\n109s call tests/x.py::t', emit=lambda message: None) == [
        ('tests/x.py::t', 109.0)]


def test_fraction_cannot_be_overridden():
    with pytest.raises(TypeError):
        near_limit('pytest --timeout=360', HEADER, fraction=1)


@pytest.mark.parametrize('exit_code,continued', [(0, False), (0, True), (1, False), (1, True)])
def test_runner_counts_offending_step_once_and_propagates_failure(tmp_path, monkeypatch, exit_code, continued):
    from scripts import ci_parity_runner

    def fake_shell(command, cwd, env, explicit_bash, timeout, log):
        log.write_text(f'{HEADER}\n109s call tests/x.py::one\n110s call tests/x.py::two\n')
        return (exit_code if '--timeout' in command else 0), False

    monkeypatch.setattr(ci_parity_runner, 'run_shell', fake_shell)
    runner, messages, _ = run_repository(tmp_path, document([
        {'id': 'suite', 'run': 'python -m pytest --timeout=360', 'continue-on-error': continued},
        {'run': 'echo only-on-success'},
        {'if': 'failure() && steps.suite.conclusion == \'failure\'', 'run': 'echo on-failure'},
    ]))
    assert runner.ran == 2 and runner.failed == 1
    assert runner.results['synthetic']['build']['result'] == 'failure'
    assert len([message for message in messages if message.startswith('NEAR TIMEOUT ')]) == 2


def test_runner_unchecked_step_does_not_fail(tmp_path, monkeypatch):
    from scripts import ci_parity_runner

    def fake_shell(command, cwd, env, explicit_bash, timeout, log):
        log.write_text('1 passed\n')
        return 0, False

    monkeypatch.setattr(ci_parity_runner, 'run_shell', fake_shell)
    runner, messages, _ = run_repository(tmp_path, document([{'run': 'pytest --timeout 360'}]))
    assert runner.ran == 1 and runner.failed == 0
    assert any(message.startswith('DURATIONS UNCHECKED ') for message in messages)
