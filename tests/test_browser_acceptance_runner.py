"""Synthetic runner failures must not produce a green browser CI result."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import zipfile
from unittest.mock import Mock, PropertyMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location('browser_acceptance_runner', ROOT / 'scripts/run-browser-acceptance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def intake_script():
    spec = importlib.util.spec_from_file_location('browser_intake', ROOT / 'scripts/browser-accept-intake-receipts.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('detachment', ['stale', 'inspector'])
def test_refresh_waits_for_replacement_document_and_complete_load(intake_script, detachment):
    from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    driver = Mock()
    previous = driver.find_element.return_value
    error = (StaleElementReferenceException() if detachment == 'stale' else
             WebDriverException('Node with given id does not belong to the document'))
    previous.is_enabled.side_effect = [True, error]
    driver.execute_script.side_effect = ['loading', 'complete']
    intake_script.refresh_page(driver, WebDriverWait(driver, 1, poll_frequency=.001))
    driver.refresh.assert_called_once_with()
    assert previous.is_enabled.call_count == 2
    assert driver.execute_script.call_count == 2


def test_refresh_preserves_unexpected_driver_failures(intake_script):
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    driver = Mock()
    driver.find_element.return_value.is_enabled.side_effect = WebDriverException('Unexpected inspector failure')
    with pytest.raises(WebDriverException, match='Unexpected inspector failure'):
        intake_script.refresh_page(driver, WebDriverWait(driver, 1, poll_frequency=.001))
    driver.execute_script.assert_not_called()


@pytest.mark.parametrize('stalled_document', ['old', 'new'])
def test_refresh_fails_when_navigation_does_not_finish(intake_script, stalled_document):
    from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    driver = Mock()
    if stalled_document == 'new':
        driver.find_element.return_value.is_enabled.side_effect = StaleElementReferenceException()
    driver.execute_script.return_value = 'loading'
    with pytest.raises(TimeoutException):
        intake_script.refresh_page(driver, WebDriverWait(driver, .01, poll_frequency=.001))
    if stalled_document == 'old':
        driver.execute_script.assert_not_called()


def receipt(path, **values):
    path.write_text(json.dumps({'passed': True, 'synthetic_only': True,
        'checks': ['Generated check'] * 20, **values}))


@pytest.fixture(params=['context', 'source_roles'])
def context_script(monkeypatch, request):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    filename, action = ('browser-accept-matter-context.py', 'activate_context_action') if request.param == 'context' else (
        'browser-accept-source-roles.py', 'activate_role_action')
    spec = importlib.util.spec_from_file_location('browser_' + request.param, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.activate = getattr(module, action)
    return module


@pytest.mark.parametrize('detachment', ['stale', 'inspector'])
def test_context_activation_waits_for_detachment_and_complete_load(context_script, detachment):
    from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait

    driver, element = Mock(), Mock()
    error = (StaleElementReferenceException() if detachment == 'stale' else WebDriverException(
        'unknown error: unhandled inspector error: {"code":-32000,"message":"Node with given id does not belong to the document"}'))
    element.is_enabled.side_effect = [True, error]
    driver.execute_script.side_effect = ['loading', 'complete']
    context_script.activate(driver, WebDriverWait(driver, 1, poll_frequency=.001), element)
    element.send_keys.assert_called_once_with(Keys.ENTER)
    assert element.is_enabled.call_count == 2
    assert driver.execute_script.call_count == 2


@pytest.mark.parametrize('phase', ['activation', 'detachment', 'readiness'])
def test_context_activation_propagates_unexpected_driver_errors(context_script, phase):
    from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    driver, element = Mock(), Mock()
    element.is_enabled.side_effect = StaleElementReferenceException()
    target = {'activation': element.send_keys, 'detachment': element.is_enabled,
              'readiness': driver.execute_script}[phase]
    target.side_effect = WebDriverException('Unexpected inspector failure')
    with pytest.raises(WebDriverException, match='Unexpected inspector failure'):
        context_script.activate(driver, WebDriverWait(driver, 1, poll_frequency=.001), element)
    assert element.send_keys.call_count == 1
    if phase == 'activation':
        element.is_enabled.assert_not_called()
    if phase != 'readiness':
        driver.execute_script.assert_not_called()


@pytest.mark.parametrize('stalled_document', ['old', 'new'])
def test_context_activation_fails_when_navigation_does_not_finish(context_script, stalled_document):
    from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    driver, element = Mock(), Mock()
    if stalled_document == 'new':
        element.is_enabled.side_effect = StaleElementReferenceException()
    driver.execute_script.return_value = 'loading'
    with pytest.raises(TimeoutException):
        context_script.activate(driver, WebDriverWait(driver, .01, poll_frequency=.001), element)
    assert element.send_keys.call_count == 1
    if stalled_document == 'old':
        driver.execute_script.assert_not_called()


@pytest.fixture
def one_box_script(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('browser_one_box', ROOT / 'scripts/browser-accept-one-box.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('phase', ['lookup', 'visibility', 'text'])
def test_one_box_destination_refinds_reason_after_replacement(one_box_script, phase):
    from selenium.common.exceptions import StaleElementReferenceException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    driver, retiring, current = Mock(), Mock(), Mock(text='Synthetic route reason')
    # The retiring page looks ready, then completion reloads it during the read.
    driver.execute_script.side_effect = [True, False, True]
    driver.find_element.side_effect = [retiring, current]
    if phase == 'lookup':
        driver.find_element.side_effect = [StaleElementReferenceException(), current]
    elif phase == 'visibility':
        retiring.is_displayed.side_effect = StaleElementReferenceException()
    else:
        type(retiring).text = PropertyMock(side_effect=StaleElementReferenceException())
    one_box_script.wait_for_destination(WebDriverWait(driver, 1, poll_frequency=.001),
        '/matters/synthetic', 'Synthetic route reason')
    assert driver.execute_script.call_count == 3
    assert driver.find_element.call_count == 2
    assert all(call.args == (By.CSS_SELECTOR, '.one-box-route-reason')
        for call in driver.find_element.call_args_list)
    current.is_displayed.assert_called_once_with()


@pytest.mark.parametrize('failure', ['hidden', 'wrong text', 'driver error'])
def test_one_box_destination_preserves_failures(one_box_script, failure):
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    driver = Mock()
    driver.execute_script.return_value = True
    element = driver.find_element.return_value
    element.is_displayed.return_value = failure != 'hidden'
    element.text = 'Synthetic route reason' if failure == 'hidden' else 'Wrong route reason'
    error = AssertionError
    if failure == 'driver error':
        type(element).text = PropertyMock(side_effect=WebDriverException('invalid session id'))
        error = WebDriverException
    with pytest.raises(error):
        one_box_script.wait_for_destination(WebDriverWait(driver, 1, poll_frequency=.001),
            '/matters/synthetic', 'Synthetic route reason')
    assert driver.find_element.call_count == 1


@pytest.mark.parametrize('failure', ['not ready', 'always stale'])
def test_one_box_destination_retains_wait_deadline(one_box_script, failure):
    from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    driver = Mock()
    driver.execute_script.return_value = failure == 'always stale'
    type(driver.find_element.return_value).text = PropertyMock(side_effect=StaleElementReferenceException())
    with pytest.raises(TimeoutException):
        one_box_script.wait_for_destination(WebDriverWait(driver, .01, poll_frequency=.001),
            '/matters/synthetic', 'Synthetic route reason')
    if failure == 'not ready':
        driver.find_element.assert_not_called()
    else:
        assert driver.find_element.call_count > 1


def test_pins_match_browser_driver_version_and_official_archives(runner):
    for name in ('linux64', 'mac-arm64'):
        version, pins = runner.browser_pins(name)
        assert set(pins) == {'chrome', 'chromedriver'}
        assert all(f'/{version}/{name}/' in pin['url'] for pin in pins.values())


def test_mismatched_download_is_rejected_before_extraction(runner, tmp_path, monkeypatch):
    archive = tmp_path / 'chrome-linux64.zip'
    archive.write_bytes(b'Generated corrupt browser download')
    monkeypatch.setattr(runner, 'browser_pins', lambda _: ('1.2.3.4', {
        'chrome': {'sha256': '0' * 64}, 'chromedriver': {'sha256': '0' * 64}}))
    monkeypatch.setattr(runner, 'extract_archive', lambda *_: pytest.fail('Unverified archive was extracted'))
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        runner.install_browser(tmp_path, 'linux64', tmp_path)


def test_archive_verification_also_applies_to_offline_copies(runner, tmp_path):
    archive = tmp_path / 'generated.zip'
    archive.write_bytes(b'Generated fixed archive bytes')
    runner.verify_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    link = tmp_path / 'linked.zip'
    link.symlink_to(archive)
    with pytest.raises(ValueError, match='regular file'):
        runner.verify_archive(link, hashlib.sha256(archive.read_bytes()).hexdigest())


@pytest.mark.parametrize('name,target', [('../escaped', None), ('/escaped', None), ('link', '../../escaped')])
def test_archive_paths_cannot_escape_scratch(runner, tmp_path, name, target):
    archive = tmp_path / 'generated.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        entry = zipfile.ZipInfo(name)
        entry.external_attr = ((stat.S_IFLNK | 0o777) if target else (stat.S_IFREG | 0o644)) << 16
        bundle.writestr(entry, target or 'Generated bytes')
    with pytest.raises(ValueError, match='Unsafe browser archive'):
        runner.extract_archive(archive, tmp_path / 'extracted')
    assert not (tmp_path / 'escaped').exists()


def test_safe_framework_links_and_executable_modes_are_preserved(runner, tmp_path):
    archive = tmp_path / 'generated.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        entry = zipfile.ZipInfo('Versions/A/generated')
        entry.external_attr = (stat.S_IFREG | 0o755) << 16
        bundle.writestr(entry, 'Generated executable bytes')
        entry = zipfile.ZipInfo('Versions/Current')
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(entry, 'A')
    root = tmp_path / 'extracted'
    runner.extract_archive(archive, root)
    assert (root / 'Versions/Current/generated').read_text() == 'Generated executable bytes'
    assert (root / 'Versions/A/generated').stat().st_mode & 0o111


@pytest.mark.parametrize('kind', ['missing', 'malformed', 'failed', 'not_synthetic', 'empty', 'truncated', 'symlink'])
def test_receipt_validation_fails_closed(runner, tmp_path, kind):
    path = tmp_path / 'receipt.json'
    if kind != 'missing':
        receipt(path)
    if kind == 'malformed':
        path.write_text('{')
    elif kind == 'failed':
        receipt(path, passed=False)
    elif kind == 'not_synthetic':
        receipt(path, synthetic_only=False)
    elif kind == 'empty':
        receipt(path, checks=[])
    elif kind == 'truncated':
        receipt(path, checks=['Generated incomplete check'])
    elif kind == 'symlink':
        source = tmp_path / 'real.json'
        path.rename(source)
        path.symlink_to(source)
    with pytest.raises((ValueError, OSError)):
        runner.check_receipt(path, 11)


def test_artifacts_are_bounded_allowlisted_and_exclude_runtime_and_downloads(runner, tmp_path, monkeypatch):
    raw = tmp_path / 'raw'
    raw.mkdir()
    receipt(raw / 'receipt.json')
    (raw / 'runtime.sqlite').write_text('Generated runtime')
    (raw / 'downloads').mkdir()
    (raw / 'downloads/final.zip').write_text('Generated bundle')
    (raw / 'failure.html').symlink_to(raw / 'runtime.sqlite')
    (raw / 'failure.png').write_bytes(b'x' * 1000)
    log = tmp_path / 'raw.log'
    log.write_bytes(b'x' * 2000)
    monkeypatch.setattr(runner, 'MAX_FILE_BYTES', 500)
    monkeypatch.setattr(runner, 'MAX_LOG_BYTES', 100)
    output = tmp_path / 'output'
    remaining, omitted = runner.collect_artifacts(raw, output, log, 900)
    assert set(path.name for path in output.iterdir()) == {'receipt.json', 'journey.log'}
    assert set(omitted) == {'failure.html', 'failure.png'}
    assert (output / 'journey.log').stat().st_size == 100
    assert sum(path.stat().st_size for path in output.iterdir()) == 900 - remaining



def test_zoom_evidence_is_retained_without_collecting_profile_data(runner, tmp_path):
    raw, output = tmp_path / 'raw', tmp_path / 'output'
    raw.mkdir()
    expected = {f'zoom-{percent}-{page}.png' for percent in (125, 150, 200, 400)
        for page in ('notes', 'sources')}
    for name in expected:
        (raw / name).write_bytes(b'Generated synthetic screenshot')
    (raw / 'zoom-profile.json').write_text('Generated profile state must not be collected')
    remaining, omitted = runner.collect_artifacts(raw, output, tmp_path / 'absent.log', 10000)
    assert {path.name for path in output.iterdir()} == expected
    assert omitted == []
    assert remaining == 10000 - sum(path.stat().st_size for path in output.iterdir())


def test_environment_excludes_deployment_and_proxy_settings(runner, tmp_path, monkeypatch):
    for key in ('CASE_INTELLIGENCE_POSTGRES_DSN', 'DATABASE_URL', 'HTTP_PROXY', 'HTTPS_PROXY', 'PYTHONHOME'):
        monkeypatch.setenv(key, 'generated-untrusted-value')
    env = runner.child_environment(tmp_path)
    assert all(value != 'generated-untrusted-value' for value in env.values())
    assert env['HF_HUB_OFFLINE'] == env['TRANSFORMERS_OFFLINE'] == '1'
    assert env['CASE_INTELLIGENCE_STORAGE_RESERVE_GIB'] == '0'
    assert Path(env['TMPDIR']).is_dir()


def test_existing_output_is_preserved_and_no_journey_runs(runner, tmp_path, monkeypatch):
    output = tmp_path / 'old'
    output.mkdir()
    marker = output / 'summary.json'
    marker.write_text('Generated old result')
    monkeypatch.setattr(runner, 'install_browser', lambda *_: pytest.fail('Browser ran with stale output'))
    assert runner.main(['--output', str(output)]) == 1
    assert marker.read_text() == 'Generated old result'


@pytest.mark.parametrize('failure_position', [1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17])
@pytest.mark.parametrize('failure', ['exit', 'timeout', 'missing', 'failed', 'truncated', 'none'])
def test_all_journeys_run_and_failure_cannot_be_hidden_by_receipt(runner, tmp_path, monkeypatch, failure, failure_position):
    scratch, output = tmp_path / 'scratch', tmp_path / 'output'
    scratch.mkdir()
    output.mkdir()
    commands = []

    def run(command, log, environment, timeout):
        commands.append(command)
        raw = Path(command[command.index('--output') + 1])
        log.write_text('Generated child output')
        name = 'receipt-browser-result.json' if len(commands) == 1 else 'receipt.json'
        failing_journey = len(commands) == failure_position
        if failure != 'missing' or not failing_journey:
            values = {'passed': failure != 'failed' or not failing_journey}
            if failure == 'truncated' and failing_journey:
                values['checks'] = ['Generated incomplete check'] * {1: 10, 4: 18, 5: 9, 6: 11, 7: 5, 8: 6, 9: 6, 10: 10, 11: 13, 12: 5, 13: 14, 14: 14, 15: 5, 16: 19, 17: 5}[failure_position]
            receipt(raw / name, **values)
        if failing_journey:
            if failure == 'exit':
                return 9  # A success receipt must not mask a process error.
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(command, timeout)
        return 0

    monkeypatch.setattr(runner, 'run_process', run)
    results = runner.run_journeys(Path('/generated/chrome'), Path('/generated/driver'), scratch, output, 1)
    assert len(commands) == 17
    assert '--verify-readiness' in commands[2]
    assert 'browser-accept-dusk.py' in commands[1][1]
    assert 'browser-accept-workspace-layout.py' in commands[3][1]
    assert 'browser-accept-activity.py' in commands[4][1]
    assert 'browser-accept-zoom.py' in commands[5][1]
    assert 'browser-accept-account-controls.py' in commands[6][1]
    assert 'browser-accept-matter-knowledge.py' in commands[7][1]
    assert 'browser-accept-matter-context.py' in commands[8][1]
    assert 'browser-accept-cited-context.py' in commands[9][1]
    assert 'browser-accept-assertions.py' in commands[10][1]
    assert 'browser-accept-assistant-dock.py' in commands[11][1]
    assert 'browser-accept-one-box.py' in commands[12][1]
    assert 'browser-accept-briefing.py' in commands[13][1]
    assert 'browser-accept-matter-purpose.py' in commands[14][1]
    assert 'browser-accept-ask-heading.py' in commands[15][1]
    assert 'browser-accept-source-roles.py' in commands[16][1]
    assert results[failure_position - 1]['passed'] is (failure == 'none')
    assert all(result['passed'] for index, result in enumerate(results) if index != failure_position - 1)
    assert json.loads((output / 'journeys.json').read_text()) == results


def alive(pid):
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith('Z')


@pytest.mark.parametrize('leader_exits', [True, False])
def test_process_group_cleanup_includes_orphan_descendants(runner, tmp_path, leader_exits):
    marker = tmp_path / 'pid'
    code = """
import os, pathlib, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
if sys.argv[2] == 'wait':
    time.sleep(60)
"""
    command = [sys.executable, '-c', code, str(marker), 'exit' if leader_exits else 'wait']
    if leader_exits:
        assert runner.run_process(command, tmp_path / 'log', dict(os.environ), 3) == 0
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            runner.run_process(command, tmp_path / 'log', dict(os.environ), 1)
    child = int(marker.read_text())
    deadline = time.monotonic() + 3
    while alive(child) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not alive(child), 'Generated descendant survived process-group cleanup'


def test_sigterm_interrupt_cleans_active_child_and_returns_failure(runner, tmp_path):
    marker = tmp_path / 'started'
    code = """
import importlib.util, os, pathlib, signal, sys
spec=importlib.util.spec_from_file_location('runner', sys.argv[1]);r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
signal.signal(signal.SIGTERM, r.interrupted)
try:
    r.run_process([sys.executable, '-c', 'import os,pathlib,sys,time;p=pathlib.Path(sys.argv[1]);t=p.with_name(p.name+".partial");t.write_text(str(os.getpid()));t.replace(p);time.sleep(60)', sys.argv[2]], pathlib.Path(sys.argv[3]), dict(os.environ), 30)
except InterruptedError:
    sys.exit(17)
"""
    parent = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts/run-browser-acceptance.py'),
        str(marker), str(tmp_path / 'log')])
    try:
        deadline = time.monotonic() + 5
        pid = ""
        while time.monotonic() < deadline:
            if marker.is_file():
                pid = marker.read_text().strip()
                if pid.isdigit():
                    break
            time.sleep(.02)
        assert pid.isdigit()
        parent.send_signal(signal.SIGTERM)
        assert parent.wait(timeout=5) == 17
        assert not alive(int(pid))
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait()


@pytest.mark.parametrize('kind', ['good', 'oversized', 'redirected'])
def test_download_is_bounded_and_stays_on_pinned_url(runner, tmp_path, monkeypatch, kind):
    import io
    url = 'https://storage.googleapis.com/chrome-for-testing-public/1.2.3.4/linux64/chrome-linux64.zip'
    class Response(io.BytesIO):
        def geturl(self):
            return url if kind != 'redirected' else 'https://example.test/unexpected'
    monkeypatch.setattr(runner.urllib.request, 'urlopen', lambda *a, **kw: Response(b'Generated archive'))
    monkeypatch.setattr(runner, 'MAX_ARCHIVE_BYTES', 2 if kind == 'oversized' else 100)
    if kind == 'good':
        runner.download_archive(url, tmp_path / 'archive.zip')
        assert (tmp_path / 'archive.zip').read_bytes() == b'Generated archive'
    else:
        with pytest.raises(ValueError, match='limit|redirect'):
            runner.download_archive(url, tmp_path / 'archive.zip')


def test_mismatched_driver_version_pin_is_rejected(runner, tmp_path, monkeypatch):
    manifest = json.loads((ROOT / 'config/browser-testing.json').read_text())
    manifest['platforms']['linux64']['chromedriver']['url'] = manifest['platforms']['linux64']['chromedriver']['url'].replace(manifest['version'], '1.2.3.4')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/browser-testing.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='matching immutable'):
        runner.browser_pins('linux64')


@pytest.mark.parametrize('outcomes,code', [
    ([index != failing for index in range(17)], 1) for failing in range(17)
] + [([True] * 17, 0), ([True] * 16, 1), ([], 1)])
def test_main_requires_all_journeys_to_pass(runner, tmp_path, monkeypatch, outcomes, code):
    monkeypatch.setattr(runner, 'install_browser', lambda *_: (Path('/generated/chrome'), Path('/generated/driver'), '1.2.3.4'))
    monkeypatch.setattr(runner, 'host_platform', lambda: 'linux64')
    monkeypatch.setattr(runner, 'run_journeys', lambda *_: [{'passed': passed} for passed in outcomes])
    output = tmp_path / 'output'
    assert runner.main(['--output', str(output)]) == code
    assert json.loads((output / 'summary.json').read_text())['passed'] is (code == 0)


@pytest.mark.parametrize("suffix", [".zip", ".docx"])
def test_report_download_waits_for_completed_archive(tmp_path, suffix):
    spec = importlib.util.spec_from_file_location("browser_reports", ROOT / "scripts/browser-accept-reports-bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    previous = tmp_path / ("previous" + suffix)
    with zipfile.ZipFile(previous, "w") as archive:
        archive.writestr("synthetic.txt", "Previous synthetic download")
    before = {previous}
    target = tmp_path / ("current" + suffix)
    target.touch()  # Chrome can reserve the final name before finishing transfer.
    assert module.completed_download(tmp_path, before, suffix) is None
    target.write_bytes(b"PK\x03\x04incomplete synthetic archive")
    assert module.completed_download(tmp_path, before, suffix) is None
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("synthetic.txt", "Complete synthetic download")
    pending = tmp_path / "current.crdownload"
    pending.touch()
    assert module.completed_download(tmp_path, before, suffix) is None
    pending.unlink()
    assert module.completed_download(tmp_path, before, suffix) == target


def test_standalone_dusk_clears_deployment_settings_before_app(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('standalone_dusk', ROOT / 'scripts/browser-accept-dusk.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ('CASE_INTELLIGENCE_POSTGRES_DSN', 'CASE_INTELLIGENCE_POSTGRES_DSN_FILE',
                 'CASE_REVIEW_RUNTIME_ROOT', 'RECORDBENCH_RUNTIME_ROOT'):
        monkeypatch.setenv(name, 'synthetic-must-not-be-used')
    monkeypatch.setattr(sys, 'argv', ['browser-accept-dusk.py', '--chrome-binary', '/synthetic/chrome',
        '--chromedriver', '/synthetic/driver', '--output', str(tmp_path / 'screens')])
    class AppBoundary(Exception):
        pass
    def check_app(*args, **kwargs):
        assert 'CASE_INTELLIGENCE_POSTGRES_DSN' not in os.environ
        assert 'CASE_INTELLIGENCE_POSTGRES_DSN_FILE' not in os.environ
        assert 'CASE_REVIEW_RUNTIME_ROOT' not in os.environ
        assert 'RECORDBENCH_RUNTIME_ROOT' not in os.environ
        assert os.environ['CASE_INTELLIGENCE_STORAGE_RESERVE_GIB'] == '0'
        raise AppBoundary
    monkeypatch.setattr(module, 'create_workbench_app', check_app)
    with pytest.raises(AppBoundary):
        module.main()


@pytest.fixture
def workspace_layout_script(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('browser_workspace_layout', ROOT / 'scripts/browser-accept-workspace-layout.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('existing', ['receipt', 'directory', 'symlink'])
def test_workspace_layout_preserves_existing_evidence(workspace_layout_script, tmp_path, existing):
    output = tmp_path / 'output'
    original = tmp_path / 'original'
    original.mkdir()
    if existing == 'symlink':
        output.symlink_to(original, target_is_directory=True)
    else:
        output.mkdir()
        if existing == 'receipt':
            (output / 'receipt.json').write_text('Generated previous receipt')
        else:
            (output / 'partial').mkdir()
    with pytest.raises(ValueError, match='existing results were preserved'):
        workspace_layout_script.prepare_output(output)
    if existing == 'receipt':
        assert (output / 'receipt.json').read_text() == 'Generated previous receipt'
    elif existing == 'directory':
        assert (output / 'partial').is_dir()
    else:
        assert output.is_symlink()
        assert list(original.iterdir()) == []


@pytest.mark.parametrize('runner_precreated', [False, True])
def test_workspace_layout_accepts_only_fresh_empty_output(workspace_layout_script, tmp_path, runner_precreated):
    output = tmp_path / 'output'
    if runner_precreated:
        output.mkdir()
    workspace_layout_script.prepare_output(output)
    assert output.is_dir() and list(output.iterdir()) == []


@pytest.mark.parametrize('capture_fails', [False, True])
def test_workspace_failure_receipt_cannot_remain_successful(workspace_layout_script, tmp_path, capture_fails):
    driver = Mock()
    if capture_fails:
        driver.save_screenshot.side_effect = RuntimeError('Synthetic disconnected driver')
    report = {'passed': True, 'synthetic_only': True, 'checks': ['Earlier passing check']}
    workspace_layout_script.failure_evidence(tmp_path, driver, report, AssertionError('Synthetic source filename is clipped'))
    saved = json.loads((tmp_path / 'receipt.json').read_text())
    assert saved['passed'] is False
    assert saved['checks'] == ['Earlier passing check']
    assert saved['error'] == {'type': 'AssertionError', 'message': 'Synthetic source filename is clipped'}
    driver.save_screenshot.assert_called_once_with(str(tmp_path / 'failure.png'))
    assert ('screenshot_error' in saved) is capture_fails


def test_standalone_workspace_layout_clears_deployment_settings_before_app(workspace_layout_script, tmp_path, monkeypatch):
    for name in ('CASE_INTELLIGENCE_POSTGRES_DSN', 'CASE_INTELLIGENCE_POSTGRES_DSN_FILE',
                 'CASE_REVIEW_RUNTIME_ROOT', 'RECORDBENCH_RUNTIME_ROOT'):
        monkeypatch.setenv(name, 'synthetic-must-not-be-used')

    class AppBoundary(Exception):
        pass

    def check_app(*args, **kwargs):
        assert all('synthetic-must-not-be-used' != value for value in os.environ.values())
        assert os.environ['CASE_INTELLIGENCE_STORAGE_RESERVE_GIB'] == '0'
        raise AppBoundary

    monkeypatch.setattr(workspace_layout_script, 'create_workbench_app', check_app)
    with pytest.raises(AppBoundary):
        workspace_layout_script.main(['--chrome-binary', '/synthetic/chrome', '--chromedriver', '/synthetic/driver',
            '--output', str(tmp_path / 'screens')])


@pytest.fixture
def zoom_script():
    spec = importlib.util.spec_from_file_location('browser_zoom', ROOT / 'scripts/browser-accept-zoom.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('detachment', ['stale', 'inspector'])
def test_zoom_treats_a_detached_handle_as_a_replaced_page(zoom_script, detachment):
    from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

    element = Mock()
    element.is_enabled.side_effect = (StaleElementReferenceException() if detachment == 'stale' else
                                      WebDriverException('Node with given id does not belong to the document'))
    assert zoom_script.replaced(element)(Mock()) is True


def test_zoom_preserves_unexpected_driver_failures(zoom_script):
    from selenium.common.exceptions import WebDriverException

    element = Mock()
    element.is_enabled.side_effect = WebDriverException('invalid session id')
    with pytest.raises(WebDriverException, match='invalid session id'):
        zoom_script.replaced(element)(Mock())
    element.is_enabled.side_effect = None
    element.is_enabled.return_value = True
    assert zoom_script.replaced(element)(Mock()) is False


@pytest.fixture
def entities_script():
    spec = importlib.util.spec_from_file_location('browser_entities', ROOT / 'scripts/browser-accept-entities.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('detachment', ['stale', 'inspector'])
def test_entities_rereads_text_when_the_page_is_replaced(entities_script, detachment):
    from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

    driver = Mock()
    replaced = Mock()
    type(replaced).text = property(lambda _self: (_ for _ in ()).throw(
        StaleElementReferenceException() if detachment == 'stale' else
        WebDriverException('Node with given id does not belong to the document')))
    current = Mock(text='Original-source mentions (0)')
    driver.find_element.side_effect = [replaced, current]
    assert entities_script.page_text(driver) == 'Original-source mentions (0)'
    assert driver.find_element.call_count == 2


def test_entities_text_preserves_unexpected_driver_failures(entities_script):
    from selenium.common.exceptions import TimeoutException, WebDriverException

    driver = Mock()
    driver.find_element.side_effect = WebDriverException('invalid session id')
    with pytest.raises(WebDriverException, match='invalid session id'):
        entities_script.page_text(driver)
    assert driver.find_element.call_count == 1
    from selenium.common.exceptions import StaleElementReferenceException
    driver = Mock()
    driver.find_element.side_effect = StaleElementReferenceException()
    with pytest.raises(TimeoutException):
        entities_script.page_text(driver, attempts=3)
    assert driver.find_element.call_count == 3


def test_failed_journey_names_where_it_stopped_in_the_job_log(runner, tmp_path, monkeypatch, capsys):
    scratch, output = tmp_path / 'scratch', tmp_path / 'output'
    scratch.mkdir()
    output.mkdir()

    def run(command, log, environment, timeout):
        failing = 'browser-accept-workspace-layout.py' in command[1]
        raw = Path(command[command.index('--output') + 1])
        if failing:
            frames = ''.join(f'#{index} 0x55aa{index:04x} <unknown>\n' for index in range(30))
            log.write_text('Generated progress\n' * 50 + 'Traceback (most recent call last):\n'
                           '  File "generated-journey.py", line 42, in main\n'
                           '    assert overflow == 0, ' + 'x' * 400 + '\n'
                           'AssertionError: generated layout overflow\nStacktrace:\n' + frames)
            return 1
        log.write_text('Generated child output')
        name = 'receipt-browser-result.json' if 'intake' in command[1] else 'receipt.json'
        receipt(raw / name, passed=True)
        return 0

    monkeypatch.setattr(runner, 'run_process', run)
    runner.run_journeys(Path('/generated/chrome'), Path('/generated/driver'), scratch, output, 1)
    printed = capsys.readouterr().out.splitlines()
    failed = printed.index('workspace-layout: FAILED')
    summary = printed[failed + 1:printed.index('activity: passed')]
    assert summary[-2:] == ['  AssertionError: generated layout overflow', '  Stacktrace:']
    assert '    File "generated-journey.py", line 42, in main' in summary
    assert summary[0] == '  Journey exited with status 1.'
    assert len(summary) == runner.MAX_SUMMARY_LINES + 1 and all(len(line) <= 302 for line in summary)
    assert not any('0x55aa' in line for line in summary)
    # Passing journeys stay one line each.
    assert printed[failed - 1] == 'reports: passed' and printed[failed + len(summary) + 1] == 'activity: passed'


def test_failure_summary_omits_macos_driver_frames_and_names_receipt_errors(runner, tmp_path, monkeypatch, capsys):
    scratch, output = tmp_path / 'scratch', tmp_path / 'output'
    scratch.mkdir()
    output.mkdir()

    def run(command, log, environment, timeout):
        raw = Path(command[command.index('--output') + 1])
        if 'browser-accept-zoom.py' in command[1]:
            frames = ''.join(f'{index}   chromedriver                        0x00000001{index:08x} cxxbridge1$str$ptr + 1\n'
                             for index in range(30))
            log.write_text('Traceback (most recent call last):\n  File "generated-journey.py", line 7, in main\n'
                           'selenium.common.exceptions.NoSuchElementException: generated\nStacktrace:\n' + frames)
            return 1
        log.write_text('Generated child output that looks successful')
        name = 'receipt-browser-result.json' if 'intake' in command[1] else 'receipt.json'
        # The activity journey exits cleanly but leaves an incomplete receipt.
        receipt(raw / name, passed='browser-accept-activity.py' not in command[1])
        return 0

    monkeypatch.setattr(runner, 'run_process', run)
    runner.run_journeys(Path('/generated/chrome'), Path('/generated/driver'), scratch, output, 1)
    printed = capsys.readouterr().out.splitlines()
    activity = printed[printed.index('activity: FAILED') + 1:printed.index('zoom: FAILED')]
    assert activity[0] == '  Journey did not report a successful synthetic receipt.'
    zoom = printed[printed.index('zoom: FAILED') + 1:printed.index('account-controls: passed')]
    assert zoom[0] == '  Journey exited with status 1.'
    assert '  selenium.common.exceptions.NoSuchElementException: generated' in zoom
    assert not any('chromedriver' in line for line in zoom)
