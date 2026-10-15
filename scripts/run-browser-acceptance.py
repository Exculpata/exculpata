#!/usr/bin/env python3
"""Run pinned, synthetic browser journeys and retain bounded diagnostics only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_LOG_BYTES = 256 * 1024
MAX_SUMMARY_LINES = 12
# ChromeDriver stack frames: Linux "#0 0x55..." and macOS "0   chromedriver   0x0001...".
NATIVE_FRAME = re.compile(r'\s*(?:#\d+ 0x[0-9a-f]+\b|\d+\s+\S+\s+0x[0-9a-f]+\b)')
JOURNEYS = (
    ('intake', 'browser-accept-intake-receipts.py', 'receipt-browser-result.json', 11, ()),
    ('dusk', 'browser-accept-dusk.py', 'receipt.json', 12, ()),
    ('reports', 'browser-accept-reports-bundle.py', 'receipt.json', 16, ('--verify-readiness',)),
    ('workspace-layout', 'browser-accept-workspace-layout.py', 'receipt.json', 19, ()),
    ('activity', 'browser-accept-activity.py', 'receipt.json', 10, ()),
    ('zoom', 'browser-accept-zoom.py', 'receipt.json', 12, ()),
    ('account-controls', 'browser-accept-account-controls.py', 'receipt.json', 6, ()),
    ('matter-knowledge', 'browser-accept-matter-knowledge.py', 'receipt.json', 7, ()),
    ('matter-context', 'browser-accept-matter-context.py', 'receipt.json', 7, ()),
    ('cited-context', 'browser-accept-cited-context.py', 'receipt.json', 11, ()),
    ('assertions', 'browser-accept-assertions.py', 'receipt.json', 14, ()),
    ('assistant-dock', 'browser-accept-assistant-dock.py', 'receipt.json', 13, ()),
    ('one-box', 'browser-accept-one-box.py', 'receipt.json', 15, ()),
    ('briefing', 'browser-accept-briefing.py', 'receipt.json', 15, ()),
    ('matter-purpose', 'browser-accept-matter-purpose.py', 'receipt.json', 6, ()),
    ('ask-heading', 'browser-accept-ask-heading.py', 'receipt.json', 20, ()),
    ('source-roles', 'browser-accept-source-roles.py', 'receipt.json', 6, ()),
)
ARTIFACT_NAMES = (
    'source-role-1440.png', 'source-role-390.png', 'source-role-320.png', 'source-role-conflict-320.png',
    'receipt-browser-result.json', 'receipt.json', 'failure.png', 'failure.html',
    'purpose-empty-1440.png', 'purpose-edit-1440.png', 'purpose-edit-390.png',
    'purpose-edit-320.png', 'purpose-conflict-320.png',
    'dusk-status.png', 'dusk-filters.png', 'dusk-bulk.png', 'dusk-focus.png',
    'dusk-bulk-active.png', 'dusk-mobile.png',
    'ask-heading-dark-1440.png', 'ask-heading-dark-390.png',
    'ask-heading-light-1440.png', 'ask-heading-light-390.png',
    'ask-heading-dusk-1440.png', 'ask-heading-dusk-390.png',
    'ask-heading-review-dark-1920.png', 'ask-heading-review-light-1920.png',
    'ask-heading-conversation-dark-1920.png', 'ask-heading-conversation-light-1920.png',
    'browser-errors.json', 'reports-desktop.png', 'reports-mobile.png',
    'export-ready-1440.png', 'export-ready-430.png', 'export-needs-attention.png',
    'readiness-failed-close-1440.png', 'readiness-failed-close-430.png',
    'closure-cancellation-window-1440.png', 'closure-cancellation-window-430.png',
    'closure-final-confirmation-1440.png', 'closure-final-confirmation-430.png',
    'legacy-check-in-invitation-1440.png', 'legacy-check-in-invitation-430.png',
    'closure-member-notice-1440.png', 'closure-member-notice-430.png',
    'delete-without-export-window-1440.png', 'delete-without-export-window-430.png',
    'delete-without-export-final-1440.png', 'delete-without-export-final-430.png',
    'notes-constrained-with-assistant.png', 'sources-mobile-actions-390.png',
    'one-box-home-430.png', 'one-box-answer-430.png',
    'deeper-investigation-offer-430.png',
    'briefing-sections-430.png', 'briefing-question-430.png', 'briefing-ask-fallback-430.png',
    'sources-mobile-actions-320.png',
    'notes-suggestion-original-support.png', 'root-font-200-percent.png',
    'text-spacing.png', 'notes-balanced-4k.png',
    'zoom-125-notes.png', 'zoom-125-sources.png',
    'zoom-150-notes.png', 'zoom-150-sources.png',
    'zoom-200-notes.png', 'zoom-200-sources.png',
    'zoom-400-notes.png', 'zoom-400-sources.png',
    'context-supplied-mobile.png', 'context-dark-desktop.png', 'context-light-desktop.png', 'context-dusk-desktop.png', 'context-dusk-mobile.png',
    'knowledge-dark-desktop.png', 'knowledge-light-desktop.png', 'knowledge-dusk-desktop.png', 'knowledge-dusk-mobile.png',
    'cited-context-desktop.png', 'cited-context-390.png', 'cited-context-320.png',
    'cited-context-assistant.png', 'cited-context-assistant-390.png', 'cited-context-assistant-320.png',
    'cited-context-changed.png',
    'cited-context-transcript-moment.png', 'cited-context-answer-return.png',
    'assertion-source-toolbar-1440.png', 'assertion-source-toolbar-1200.png',
    'assertion-desktop.png', 'assertion-accounts.png', 'assertion-source-return-mobile.png',
    'assertion-conflict.png', 'assertion-mobile.png', 'assertion-mobile-recovery.png',
    'assertion-chronology.png',
    'dock-source-1440.png', 'dock-source-390.png', 'dock-source-320.png',
    'dock-suggest-preview-1440.png', 'dock-suggested-1440.png', 'dock-suggest-390.png', 'dock-suggest-320.png', 'dock-suggest-preview-320.png',
    'dock-draft-1440.png', 'dock-draft-390.png', 'dock-draft-320.png',
)


def host_platform() -> str:
    pair = sys.platform, platform.machine().lower()
    if pair == ('linux', 'x86_64'):
        return 'linux64'
    if pair == ('darwin', 'arm64'):
        return 'mac-arm64'
    raise ValueError('Pinned browser acceptance supports Linux x86_64 and Apple Silicon macOS.')


def browser_pins(platform_name: str) -> tuple[str, dict]:
    manifest = json.loads((ROOT / 'config/browser-testing.json').read_text())
    version = manifest['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version):
        raise ValueError('Invalid browser version pin.')
    pins = manifest['platforms'][platform_name]
    for tool in ('chrome', 'chromedriver'):
        expected = f'https://storage.googleapis.com/chrome-for-testing-public/{version}/{platform_name}/{tool}-{platform_name}.zip'
        if pins[tool]['url'] != expected or not re.fullmatch(r'[0-9a-f]{64}', pins[tool]['sha256']):
            raise ValueError('Browser and driver must have matching immutable URL and SHA-256 pins.')
    return version, pins


def verify_archive(path: Path, expected_digest: str) -> None:
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('Browser archive must be a bounded regular file.')
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected_digest:
        raise ValueError('Browser archive SHA-256 mismatch; nothing was extracted or executed.')


def download_archive(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, destination.open('xb') as output:
        if response.geturl() != url:
            raise ValueError('Unexpected browser archive redirect.')
        received = 0
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > MAX_ARCHIVE_BYTES:
                raise ValueError('Browser archive exceeds the download limit.')
            output.write(chunk)


def extract_archive(archive: Path, destination: Path) -> None:
    """Extract verified Chrome archives, including bounded macOS framework links."""
    destination.mkdir()
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 10000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ValueError('Browser archive exceeds extraction limits.')
        links = []
        for entry in entries:
            relative = PurePosixPath(entry.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename:
                raise ValueError('Unsafe browser archive path.')
            target = destination / relative
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                links.append((target, bundle.read(entry).decode('utf-8')))
            elif entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError('Unsupported browser archive entry.')
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if mode & 0o111 else 0o644)
        for target, link in links:
            if Path(link).is_absolute() or not (target.parent / link).resolve().is_relative_to(destination):
                raise ValueError('Unsafe browser archive link.')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        if any(not target.resolve().is_relative_to(destination) for target, _ in links):
            raise ValueError('Browser archive link leaves the extraction root.')


def install_browser(scratch: Path, platform_name: str, archives: Path | None = None) -> tuple[Path, Path, str]:
    version, pins = browser_pins(platform_name)
    extracted = {}
    for tool in ('chrome', 'chromedriver'):
        pin = pins[tool]
        archive = (archives or scratch) / f'{tool}-{platform_name}.zip'
        if archives is None:
            download_archive(pin['url'], archive)
        verify_archive(archive, pin['sha256'])
        extracted[tool] = scratch / tool
        extract_archive(archive, extracted[tool])
    chrome_relative = ('chrome-linux64/chrome' if platform_name == 'linux64' else
        'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing')
    chrome = extracted['chrome'] / chrome_relative
    driver = extracted['chromedriver'] / f'chromedriver-{platform_name}/chromedriver'
    if not chrome.is_file() or not driver.is_file():
        raise ValueError('Verified archives did not contain the expected browser executables.')
    return chrome, driver, version


def child_environment(scratch: Path) -> dict[str, str]:
    # Never inherit deployment settings, proxy credentials, model endpoints or
    # Python import overrides from the contributor's shell.
    for name in ('home', 'tmp', 'config', 'cache'):
        (scratch / name).mkdir()
    return {
        'PATH': os.environ.get('PATH', os.defpath),
        'HOME': str(scratch / 'home'), 'TMPDIR': str(scratch / 'tmp'),
        'XDG_CONFIG_HOME': str(scratch / 'config'), 'XDG_CACHE_HOME': str(scratch / 'cache'),
        'LANG': 'C.UTF-8', 'TZ': 'UTC', 'PYTHONPATH': str(ROOT / 'src'),
        'PYTHONNOUSERSITE': '1', 'PYTHONUNBUFFERED': '1',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'CASE_INTELLIGENCE_STORAGE_RESERVE_GIB': '0',
        'NO_PROXY': 'localhost,127.0.0.1,::1',
    }


def terminate_group(process: subprocess.Popen) -> None:
    # Clean descendants even after their leader exits successfully. Drivers and
    # browsers inherit the journey's session/process group.
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            break
        if signum == signal.SIGTERM:
            time.sleep(0.2)
            # Reap a terminated leader before signaling its remaining group.
            # macOS can return EPERM for a group containing only its zombie.
            process.poll()
    process.wait(timeout=5)


def run_process(command: list[str], log: Path, environment: dict[str, str], timeout: float) -> int:
    with log.open('xb') as output:
        process = subprocess.Popen(command, cwd=ROOT, env=environment,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        finally:
            terminate_group(process)


def check_receipt(path: Path, minimum_checks: int) -> None:
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ValueError('Missing or oversized regular receipt.')
    receipt = json.loads(path.read_text())
    if not isinstance(receipt, dict) or receipt.get('passed') is not True or receipt.get('synthetic_only') is not True:
        raise ValueError('Journey did not report a successful synthetic receipt.')
    checks = receipt.get('checks')
    if not isinstance(checks, list) or len(checks) < minimum_checks or not all(isinstance(c, str) and c.strip() for c in checks):
        raise ValueError('Journey receipt is missing required checks.')


def collect_artifacts(raw: Path, destination: Path, log: Path, remaining: int) -> tuple[int, list[str]]:
    destination.mkdir()
    omitted = []
    for name in ARTIFACT_NAMES:
        source = raw / name
        if not source.exists() and not source.is_symlink():
            continue
        metadata = source.lstat()
        limit = MAX_RECEIPT_BYTES if name.endswith('.json') else MAX_FILE_BYTES
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > min(limit, remaining):
            omitted.append(name)
            continue
        shutil.copyfile(source, destination / name)
        remaining -= metadata.st_size
    if log.exists() and stat.S_ISREG(log.lstat().st_mode):
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - min(MAX_LOG_BYTES, remaining)))
            tail = stream.read(min(MAX_LOG_BYTES, remaining))
        (destination / 'journey.log').write_bytes(tail)
        remaining -= len(tail)
    return remaining, omitted


def failure_summary(log: Path) -> list[str]:
    """Return the end of a failed journey's traceback for the CI log.

    Hosted artifacts can be unavailable to the person diagnosing a failure, so
    the job log names where the synthetic journey stopped. Native driver frames
    are omitted and every line is bounded.
    """
    if not log.exists() or not stat.S_ISREG(log.lstat().st_mode):
        return []
    with log.open('rb') as stream:
        stream.seek(max(0, log.stat().st_size - 64 * 1024))
        text = stream.read().decode('utf-8', 'replace')
    lines = [line.rstrip() for line in text.splitlines()
        if line.strip() and not NATIVE_FRAME.match(line)]
    return [line[:300] for line in lines[-MAX_SUMMARY_LINES:]]


def run_journeys(chrome: Path, driver: Path, scratch: Path, output: Path, timeout: float) -> list[dict]:
    results = []
    remaining = MAX_ARTIFACT_BYTES
    for name, script, receipt, minimum_checks, extra in JOURNEYS:
        root = scratch / name
        root.mkdir()
        raw = root / 'raw'
        raw.mkdir()
        log = root / 'journey.log'
        result = {'journey': name, 'passed': False}
        try:
            command = [sys.executable, str(ROOT / 'scripts' / script),
                '--chrome-binary', str(chrome), '--chromedriver', str(driver),
                '--output', str(raw), *extra]
            code = run_process(command, log, child_environment(root), timeout)
            if code != 0:
                raise ValueError(f'Journey exited with status {code}.')
            check_receipt(raw / receipt, minimum_checks)
            result['passed'] = True
        except InterruptedError:
            result['error'] = 'Journey interrupted.'
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result['error'] = str(exc)[:500]
        finally:
            remaining, omitted = collect_artifacts(raw, output / name, log, remaining)
            result['omitted_artifacts'] = omitted
            results.append(result)
            (output / 'journeys.json').write_text(json.dumps(results, indent=2) + '\n')
        print(f"{name}: {'passed' if result['passed'] else 'FAILED'}", flush=True)
        if not result['passed']:
            if result.get('error'):
                print(f"  {result['error'][:300]}", flush=True)
            for line in failure_summary(log):
                print(f'  {line}', flush=True)
    return results


def interrupted(signum, _frame):
    raise InterruptedError(f'Browser acceptance interrupted by signal {signum}.')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='A new directory for bounded diagnostics.')
    parser.add_argument('--archives', type=Path, help='Optional offline archive directory; pins are still verified.')
    parser.add_argument('--timeout-seconds', type=int, default=300, choices=range(1, 601), metavar='1..600')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print('Choose a fresh output directory; existing results were preserved.', file=sys.stderr)
        return 1
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    summary = {'synthetic_only': True, 'passed': False}
    try:
        with tempfile.TemporaryDirectory(prefix='recordbench-browser-ci-') as temporary:
            scratch = Path(temporary).resolve()
            name = host_platform()
            chrome, driver, version = install_browser(scratch, name, args.archives)
            summary.update(platform=name, browser_version=version)
            results = run_journeys(chrome, driver, scratch, output, args.timeout_seconds)
            summary['passed'] = len(results) == len(JOURNEYS) and all(result['passed'] for result in results)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        summary['error'] = str(exc)[:500]
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
