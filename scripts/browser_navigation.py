"""Bounded navigation for synthetic browser acceptance journeys."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

DIAGNOSTIC_NAME = 'navigation-timeouts.json'
MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_EVENTS = 64


def _record_timeout(output, url, attempt, phase, page_load_timeout, readiness_timeout):
    """Keep bounded, content-free evidence before attempting recovery."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / DIAGNOSTIC_NAME
    events = []
    if path.exists():
        if path.stat().st_size > MAX_DIAGNOSTIC_BYTES:
            raise ValueError('Navigation diagnostic exceeds its byte limit')
        events = json.loads(path.read_text())['events']
    if len(events) >= MAX_EVENTS:
        raise ValueError('Navigation diagnostic exceeds its event limit')
    events.append(dict(attempt=attempt, phase=phase, retry=attempt == 1,
                       reason='renderer-timeout', url_sha256=hashlib.sha256(url.encode()).hexdigest(),
                       page_load_timeout=page_load_timeout, readiness_timeout=readiness_timeout))
    payload = json.dumps(dict(synthetic_only=True, events=events), indent=2) + '\n'
    if len(payload.encode()) > MAX_DIAGNOSTIC_BYTES:
        raise ValueError('Navigation diagnostic exceeds its byte limit')
    path.write_text(payload)
    print(f'Renderer navigation timeout: attempt {attempt}/2; diagnostic {DIAGNOSTIC_NAME}', flush=True)


def navigate_to(driver, url, *, diagnostics, page_load_timeout=30, readiness_timeout=30):
    """Navigate and await readiness; retry one renderer timeout, recording both attempts."""
    if page_load_timeout <= 0 or readiness_timeout <= 0:
        raise ValueError('Navigation timeouts must be positive')
    driver.set_page_load_timeout(page_load_timeout)
    for attempt in (1, 2):
        phase = 'navigation'
        try:
            driver.get(url)
            phase = 'readiness'
            WebDriverWait(driver, readiness_timeout).until(
                lambda current: current.execute_script('return document.readyState') == 'complete')
            return
        except TimeoutException as exc:
            if 'timed out receiving message from renderer' not in (exc.msg or '').casefold():
                raise
            _record_timeout(diagnostics, url, attempt, phase, page_load_timeout, readiness_timeout)
            if attempt == 2:
                raise
