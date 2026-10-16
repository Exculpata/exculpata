#!/usr/bin/env python3
"""Exercise Activity's real modal lifecycle with delayed synthetic responses."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from starlette.responses import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.managed_storage import StoragePolicy  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402
from synthetic_browser_environment import isolate_environment  # noqa: E402

ACTOR = 'development-taylor-morgan'


class ActivityResponses:
    """Control only Activity transport; successful bodies come from the real app."""

    def __init__(self, app):
        self.app = app
        self.started = 0
        self.plan(status=503, hold=True)

    def plan(self, *, status=200, hold=False):
        self.status = status
        self.gate = threading.Event()
        if not hold:
            self.gate.set()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope['path'] == '/activity':
            gate, status = self.gate, self.status
            self.started += 1
            released = await asyncio.to_thread(gate.wait, 20)
            if not released or status != 200:
                await Response('Synthetic activity service unavailable', status_code=503)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    isolate_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'synthetic_only': True, 'checks': []}
    checks = report['checks']
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-activity-") as temporary:
        app = create_workbench_app(Path(temporary).resolve() / 'runtime', auth_mode='test',
            generator=UnavailableGenerator(), learned_retrieval=False, background_ingestion=False,
            storage_policy=StoragePolicy(reserve_bytes=0))
        bench = app.state.workbench
        # Keep queued synthetic jobs stable; no model or external service runs.
        assert bench.research is not None
        bench.research.close()
        assert bench.answers is not None
        bench.answers.close()
        for number in range(6):
            matter = bench.create_matter(f'Synthetic activity matter {number}', 'Generated modal checks', ACTOR)
            for job in range(3):
                bench.workspace.queue_research_job(matter.matter_id, ACTOR,
                    'Synthetic review question', f'Generated review {number}-{job}',
                    f'research-request-{number * 3 + job:032x}')
        conversation = bench.workspace.get_conversation(matter.matter_id)
        for number in range(3):
            job, _ = bench.workspace.queue_answer_job(matter.matter_id, conversation.conversation_id,
                ACTOR, 'Generated duplicate-destination question', f'answer-request-{number:032x}')
            claimed = bench.workspace.claim_answer_job('synthetic-activity-worker')
            assert claimed.job_id == job.job_id
            bench.workspace.fail_answer_job(job.job_id, f'Generated failure {number}')
        transport = ActivityResponses(app)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(transport, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        # Match the existing disposable journeys on hosted Linux runners.
        if sys.platform == "linux":
            options.add_argument("--no-sandbox")
        for flag in ('--headless=new', '--disable-dev-shm-usage', '--no-proxy-server', '--window-size=1024,768'):
            options.add_argument(flag)
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 15)
            wait.until(lambda _: server.started)
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                'width': 1024, 'height': 768, 'deviceScaleFactor': 1, 'mobile': False})
            driver.get(base + f'/matters/{matter.slug}/notebook')
            find = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)
            js = driver.execute_script
            active = lambda: driver.switch_to.active_element
            drawer, toggle = find('[data-activity-drawer]'), find('[data-activity-toggle]')
            close = find('[data-activity-close]')
            rail_scrim = find('[data-rail-scrim]')
            js('arguments[0].setAttribute("inert", "preserved-state")', rail_scrim)
            toggle.click()
            wait.until(lambda _: js('return arguments[0].getBoundingClientRect().right <= innerWidth', close))
            wait.until(lambda _: active() == close)
            wait.until(lambda _: transport.started > 0)
            wait.until(lambda _: find('#activity-drawer-heading').text == 'Activity')
            assert js('return document.querySelector(".topbar").inert && document.querySelector("#main-content").inert')
            assert not js('return arguments[0].closest("[inert]")', drawer)
            close.send_keys(Keys.TAB)
            assert active() == close
            ActionChains(driver).key_down(Keys.SHIFT).send_keys(Keys.TAB).key_up(Keys.SHIFT).perform()
            assert active() == close
            js('document.activeElement.blur()')
            ActionChains(driver).send_keys(Keys.TAB).perform()
            assert active() == close
            checks.append('Delayed first load has a stable labelled heading and Close; background is inert; Tab, Shift+Tab and unfocused recovery stay inside')

            transport.gate.set()
            wait.until(lambda _: find('[data-activity-retry]').is_displayed())
            assert 'couldn’t be updated' in find('[data-activity-loading]').text
            assert active() == close
            assert close.is_displayed()
            close.send_keys(Keys.TAB)
            assert active() == find('[data-activity-retry]')
            active().send_keys(Keys.TAB)
            assert active() == close
            checks.append('Failed first load retains its heading and Close, offers retry, and keeps keyboard focus contained')

            transport.plan(hold=True)
            before = transport.started
            find('[data-activity-retry]').click()
            wait.until(lambda _: transport.started > before)
            close.click()
            wait.until(lambda _: active() == toggle)
            assert js('return !document.querySelector(".topbar").inert && !document.querySelector("#main-content").inert')
            assert rail_scrim.get_attribute('inert') is not None
            assert js('return arguments[0].getAttribute("inert")', rail_scrim) == 'preserved-state'
            transport.gate.set()
            wait.until(lambda _: len(driver.find_elements(By.CSS_SELECTOR, '[data-activity-content]')) == 1)
            assert active() == toggle
            assert drawer.get_attribute('aria-hidden') == 'true'
            assert js('return arguments[0].inert', drawer)
            checks.append('Close during a delayed retry restores the opener and exact prior inert states; its later success never steals focus')

            transport.plan()
            old_content = find('[data-activity-content]')
            toggle.click()
            wait.until(lambda _: js('return arguments[0].getBoundingClientRect().right <= innerWidth', close))
            wait.until(EC.staleness_of(old_content))
            assert active() == close
            assert js('return arguments[0].scrollHeight > arguments[0].clientHeight', find('.activity-list'))
            transport.plan(hold=True)
            before = transport.started
            focused = js('''const list = document.querySelector('.activity-list');
                list.scrollTop = 360;
                const box = list.getBoundingClientRect();
                const item = [...list.querySelectorAll('[data-activity-action]')].find(el => {
                  const r = el.getBoundingClientRect(); return r.top > box.top + 10 && r.bottom < box.bottom - 10;
                }); item.focus({preventScroll:true}); return item;''')
            action = focused.get_attribute('data-activity-action')
            scroll_top = js('return arguments[0].scrollTop', find('.activity-list'))
            assert scroll_top > 0
            wait.until(lambda _: transport.started > before)
            transport.gate.set()
            wait.until(EC.staleness_of(focused))
            wait.until(lambda _: active().get_attribute('data-activity-action') == action)
            assert abs(js('return arguments[0].scrollTop', find('.activity-list')) - scroll_top) <= 1
            checks.append('An actual automatic poll replaces data while preserving the focused surviving record action and list scroll')

            transport.plan(hold=True)
            before = transport.started
            answers = [row for row in driver.find_elements(By.CSS_SELECTOR, '.activity-item')
                if 'Focused answer' in row.text]
            assert len(answers) == 3
            assert len({row.get_attribute('href') for row in answers}) == 1
            assert len({row.get_attribute('data-activity-action') for row in answers}) == 3
            focused = answers[1]
            detail = focused.text
            js('arguments[0].scrollIntoView({block:"center",behavior:"instant"}); arguments[0].focus({preventScroll:true})', focused)
            action = focused.get_attribute('data-activity-action')
            scroll_top = js('return arguments[0].scrollTop', find('.activity-list'))
            wait.until(lambda _: transport.started > before)
            transport.gate.set()
            wait.until(EC.staleness_of(focused))
            wait.until(lambda _: active().get_attribute('data-activity-action') == action)
            assert active().text == detail
            assert abs(js('return arguments[0].scrollTop', find('.activity-list')) - scroll_top) <= 1
            checks.append('Three answer jobs share a destination; polling preserves focus on the second distinct record and its scroll position')

            transport.plan(status=503)
            focused = active()
            wait.until(lambda _: 'Updates paused' in find('[data-activity-updated]').text)
            assert active() == focused
            assert abs(js('return arguments[0].scrollTop', find('.activity-list')) - scroll_top) <= 1
            active().send_keys(Keys.ESCAPE)
            assert active() == toggle
            assert drawer.get_attribute('aria-hidden') == 'true'
            checks.append('A failed automatic poll keeps existing actions, focus and scroll; Escape restores the opener')

            transport.plan(hold=True)
            before = transport.started
            toggle.click()
            wait.until(lambda _: js('return arguments[0].getBoundingClientRect().right <= innerWidth', close))
            wait.until(lambda _: transport.started > before)
            scrim = find('[data-activity-scrim]')
            ActionChains(driver).move_to_element_with_offset(scrim, -250, 0).click().perform()
            wait.until(lambda _: active() == toggle)
            old_content = find('[data-activity-content]')
            transport.gate.set()
            wait.until(EC.staleness_of(old_content))
            assert active() == toggle
            checks.append('Clicking the scrim closes a pending refresh and the eventual response leaves focus on the opener')

            # Pointer activation need not focus buttons in every browser.
            transport.plan()
            js('document.activeElement.blur(); arguments[0].click()', toggle)
            wait.until(lambda _: active() == close)
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            assert active() == toggle
            checks.append('Opening with no focused control still restores the Activity trigger when closed')

            # A replaced opener must never strand focus in the hidden drawer.
            transport.plan()
            toggle.click()
            wait.until(lambda _: js('return arguments[0].getBoundingClientRect().right <= innerWidth', close))
            js('arguments[0].remove()', toggle)
            close.click()
            assert active() == find('#main-content')
            checks.append('If the opener disappears, closing returns focus to main content instead of a detached control')
            driver.execute_cdp_cmd('Emulation.setScriptExecutionDisabled', {'value': True})
            driver.get(base + f'/matters/{matter.slug}/notebook')
            assert not driver.find_elements(By.CSS_SELECTOR, '.matter-section-disclosure')
            ActionChains(driver).send_keys(Keys.TAB).perform()
            assert active() == find('.skip-link')
            active().send_keys(Keys.ENTER)
            assert active() == find('#main-content')
            checks.append('With page JavaScript disabled, the first Tab reaches Skip to main content and Enter focuses its rendered main target')
            report.update(passed=True, browser='Chrome for Testing', viewport='1024x768',
                commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                unrun_checks=['Safari/WebKit', 'screen reader announcements'])
        except Exception as error:
            report['error'] = type(error).__name__ + ': ' + (str(error).splitlines() or ['No further detail'])[0]
            if driver:
                driver.save_screenshot(str(args.output / 'failure.png'))
            raise
        finally:
            transport.gate.set()
            (args.output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
            if driver:
                driver.quit()
            server.should_exit = True
            thread.join(10)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
