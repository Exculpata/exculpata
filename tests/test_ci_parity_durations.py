"""Synthetic duration reports and the runner's failure propagation."""

import pytest

from scripts.ci_parity_durations import NEAR_LIMIT_FRACTION, near_limit
from tests.test_ci_parity import document, run_repository


HEADER = '============================= slowest 25 durations ============================='


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
    assert near_limit('pytest --timeout-method=signal', HEADER + '\n999s call tests/x.py::t',
                      emit=messages.append) == []
    assert messages == []


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
