#!/usr/bin/env python3
"""Synthetic C2 reader navigation, keyboard editing and conflict recovery."""
import argparse
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.source_roles import SourceRoleService
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'


def wait_for_replacement(driver, wait, element):
    """Observe the retiring document before checking its replacement's readiness."""
    def detached(current):
        try:
            return EC.staleness_of(element)(current)
        except WebDriverException as exc:
            if 'Node with given id does not belong to the document' not in exc.msg:
                raise
            return True
    wait.until(detached)
    wait.until(lambda _: driver.execute_script('return document.readyState') == 'complete')


def activate_role_action(driver, wait, element):
    """Finish the native navigation before a subsequent reload or request."""
    element.send_keys(Keys.ENTER)
    wait_for_replacement(driver, wait, element)


def refresh_page(driver, wait):
    # Chrome can acknowledge reload before the replacement document commits.
    previous_document = driver.find_element(By.TAG_NAME, 'html')
    driver.refresh()
    wait_for_replacement(driver, wait, previous_document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Choose a new or empty output directory.')
    args.output.mkdir(parents=True, exist_ok=True)
    isolate_environment()
    report = dict(synthetic_only=True, passed=False, checks=[])
    driver = None
    with tempfile.TemporaryDirectory(prefix='exculpata-source-roles-') as temporary:
        app = create_workbench_app(Path(temporary).resolve() / 'runtime', auth_mode='test',
            generator=UnavailableGenerator(), background_ingestion=False, learned_retrieval=False)
        bench = app.state.workbench
        matter = bench.create_matter('Synthetic charging paper roles', '', ACTOR)
        store = bench.source_store(matter)
        document, _ = store.store_stream('synthetic-charge.txt', 'text/plain', io.BytesIO(b'Synthetic charging paper: copper arrived at noon.'))
        bench._ensure_source_organizations(matter, store)
        bench.index_document(matter, document.document_id)
        token = store.action_token(document)
        service = SourceRoleService(bench.workspace)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        for flag in ('--headless=new', '--no-sandbox', '--no-proxy-server', '--window-size=1440,1100'):
            options.add_argument(flag)
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            wait = WebDriverWait(driver, 30)
            wait.until(lambda _: server.started)
            reader = base + f'/matters/{matter.slug}/sources/{token}'
            def find(selector):
                return driver.find_element(By.CSS_SELECTOR, selector)
            def focus(element):
                for _ in range(100):
                    if driver.switch_to.active_element == element:
                        assert element.is_displayed() and element.is_enabled()
                        assert driver.execute_script('return arguments[0].matches(":focus-visible")', element)
                        return
                    ActionChains(driver).send_keys(Keys.TAB).perform()
                raise AssertionError('Control is not reachable with native Tab')
            def submit():
                button = find('#source-role-form button')
                focus(button)
                activate_role_action(driver, wait, button)
            def check(message):
                report['checks'].append(message)
                print(message, flush=True)
            driver.get(reader)
            details = find('.review-source-details summary')
            focus(details)
            details.send_keys(Keys.ENTER)
            assert 'Role: Unclassified' in find('[data-source-role]').text
            link = find('[data-source-role] a')
            focus(link)
            activate_role_action(driver, wait, link)
            wait.until(lambda _: driver.current_url.split('?')[0].endswith('/role'))
            choice = find('#source-role')
            focus(choice)
            choice.send_keys('b')
            assert choice.get_attribute('value') == 'background'
            submit()
            wait.until(lambda _: driver.current_url == reader)
            assert service.read(matter.matter_id, ACTOR, token)['source_role'] == 'background'
            check('Native keyboard reaches editor from source details and marks background')
            refresh_page(driver, wait)
            driver.get(reader + '/role')
            assert find('#source-role').get_attribute('value') == 'background'
            check('Designation survives reader navigation and reload')
            for width in (1440, 390, 320):
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(width=width, height=1100, deviceScaleFactor=1, mobile=False))
                assert driver.execute_script('return innerWidth') == width
                assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
                driver.save_screenshot(str(args.output / f'source-role-{width}.png'))
            check('Desktop and exact 390/320 pixel editors have no horizontal overflow')
            service.update(matter.matter_id, ACTOR, token, expected_revision=1, source_role='unclassified', request_id='synthetic-browser-competing-edit')
            submit()
            wait.until(lambda _: 'source role changed' in find('main').text)
            assert 'Current role: Unclassified' in find('#saved-source-role').text
            assert find('#source-role').get_attribute('value') == 'background'
            assert service.read(matter.matter_id, ACTOR, token)['source_role'] == 'unclassified'
            driver.save_screenshot(str(args.output / 'source-role-conflict-320.png'))
            check('Stale save retains the proposed choice and shows the competing saved role')
            submit()
            wait.until(lambda _: driver.current_url == reader)
            driver.get(reader + '/role')
            choice = find('#source-role')
            focus(choice)
            choice.send_keys('u')
            assert choice.get_attribute('value') == 'unclassified'
            submit()
            wait.until(lambda _: driver.current_url == reader)
            assert service.read(matter.matter_id, ACTOR, token)['source_role'] == 'unclassified'
            check('Reviewer deliberately reconciles the conflict and clears the role')
            assert not bench.workspace.answer_jobs(matter.matter_id, ACTOR)
            assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)
            check('Role changes create no answer or research jobs')
            report['passed'] = True
        except BaseException:
            if driver:
                driver.save_screenshot(str(args.output / 'failure.png'))
            raise
        finally:
            if driver:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive()
            listener.close()
            (args.output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
