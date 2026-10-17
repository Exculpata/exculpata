#!/usr/bin/env python3
"""Synthetic manual entity journey in a disposable loopback app, without a model."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.parse import urlparse

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'


DETACHED_NODE = 'Node with given id does not belong to the document'


def page_text(driver, attempts=5):
    """The body text, re-read if the page is replaced between locating and reading it.

    Chrome reports a replaced document either as a stale reference or as the
    detached-node inspector error; any other driver failure is real.
    """
    for attempt in range(attempts):
        try:
            return driver.find_element(By.TAG_NAME, 'body').text
        except StaleElementReferenceException:
            pass
        except WebDriverException as exc:
            if DETACHED_NODE not in (exc.msg or ''):
                raise
        if attempt == attempts - 1:
            raise TimeoutException('The page kept changing while its text was read.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    isolate_environment()
    checks = []
    def record(message):
        checks.append(message)
        print(message, flush=True)
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-entities-browser-") as temporary:
        root = Path(temporary).resolve()
        originals = []
        for name, text in [('depot', 'Alex Example visited the synthetic depot.'), ('archive', 'Alex Example called the synthetic archive.')]:
            path = root / f'generated-{name}.txt'
            path.write_text(text)
            originals.append(path)
        app = create_workbench_app(root / 'runtime', generator=UnavailableGenerator(), auth_mode='test', background_ingestion=True)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False, ws='none'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        drivers = []
        try:
            def browser():
                options = Options()
                options.binary_location = str(args.chrome_binary)
                for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1000'):
                    options.add_argument(flag)
                driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
                driver.set_page_load_timeout(30)
                driver.set_script_timeout(30)
                drivers.append(driver)
                return driver
            driver = browser()
            wait = WebDriverWait(driver, 20)
            wait.until(lambda _: server.started)
            def go(path):
                driver.get(base + path)
            def fill(selector, text):
                element = driver.find_element(By.CSS_SELECTOR, selector)
                element.clear()
                element.send_keys(text)
            def detached(element):
                def check(current):
                    try:
                        return EC.staleness_of(element)(current)
                    except WebDriverException as exc:
                        if 'Node with given id does not belong to the document' not in exc.msg:
                            raise
                        return True
                return check
            def click(selector):
                element = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", element)
                element.click()
                wait.until(detached(element))
                wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')
            def body():
                return page_text(driver)
            go('/matters/new')
            fill('#matter-name', 'Generated entity review')
            click('.matter-form button[type=submit]')
            slug = urlparse(driver.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            driver.find_element(By.CSS_SELECTOR, '[data-assistant-collapse]').click()
            collection_name = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic entities collection")
            driver.find_element(By.ID, 'source-files').send_keys('\n'.join(str(path) for path in originals))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]').is_enabled())
            confirm = driver.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", confirm)
            confirm.click()
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            wait.until(lambda _: len([d for d in bench.source_store(matter).documents.values() if d.state == 'ready']) == 2)
            record('Two synthetic originals uploaded and searchable without a model')
            go(prefix + '/entities')
            assert 'No entities yet' in body()
            field = driver.find_element(By.CSS_SELECTOR, 'input[name=display_name]')
            field.send_keys('Alex Example', Keys.TAB, Keys.TAB, Keys.TAB, 'A. Example', Keys.TAB, Keys.ENTER)
            wait.until(lambda _: 'Original-source mentions (0)' in body())
            entity_path = urlparse(driver.current_url).path
            record('Visible empty state and keyboard creation of a person with an alias')
            for term in ('depot', 'archive'):
                go(prefix + '?mode=search&q=' + term)
                link = driver.find_element(By.CSS_SELECTOR, 'a[href*="?support="]')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", link)
                link.click()
                wait.until(lambda d: d.find_element(By.ID, 'support-pane'))
                if term == 'archive':
                    click('.support-header-open')
                click('a[href*="/entities?support="]')
                assert 'Selected original passage' in body()
                click('form:has(input[value="attach"]) button')
                assert 'Original-source mentions' in body()
                driver.find_element(By.LINK_TEXT, 'Return to source review').click()
                assert 'mode=search' in driver.current_url and 'q=' + term in driver.current_url
                go(entity_path)
            assert 'Original-source mentions (2)' in body()
            detail = bench.entity_service(matter).detail(matter.matter_id, ACTOR, entity_path.split('/')[-1])
            assert len(detail[1]) == 2
            driver.save_screenshot(str(args.output / 'entity-desktop.png'))
            for index in range(2):
                go(entity_path)
                links = driver.find_elements(By.LINK_TEXT, 'Open original passage')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", links[index])
                links[index].click()
                wait.until(lambda d: d.find_element(By.ID, 'support-pane'))
                assert 'Alex Example' in driver.find_element(By.ID, 'support-pane').text
            record('Two exact source passages attached to one identity and both reopened from its detail page')
            go(prefix + '/entities')
            fill('input[name=display_name]', 'Alex Example')
            click('.notebook-item-form button')
            assert 'Original-source mentions (0)' in body()
            assert bench.entity_service(matter).list(matter.matter_id, ACTOR)[1] == 2
            record('Another same-name person remains distinct, with no automatic alias merge')
            go(entity_path)
            other = browser()
            other.get(base + entity_path)
            name = other.find_element(By.CSS_SELECTOR, 'input[name=display_name]')
            name.clear()
            name.send_keys('Alex Example — reviewed')
            button = other.find_element(By.CSS_SELECTOR, '.notebook-item-form button')
            other.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", button)
            button.click()
            WebDriverWait(other, 20).until(detached(button))
            fill('input[name=display_name]', 'Unsaved synthetic correction')
            fill('textarea[name=aliases]', 'Unsaved synthetic alias')
            click('.notebook-item-form button')
            assert 'changed since you opened' in body() and 'Alex Example — reviewed' in body()
            assert driver.find_element(By.CSS_SELECTOR, 'input[name=display_name]').get_attribute('value') == 'Unsaved synthetic correction'
            assert driver.find_element(By.CSS_SELECTOR, 'textarea[name=aliases]').get_attribute('value') == 'Unsaved synthetic alias'
            driver.save_screenshot(str(args.output / 'entity-conflict.png'))
            record('Shared-edit conflict preserves the submitted name and aliases alongside the current saved identity')
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=False))
            assert driver.execute_script('return innerWidth') == 390
            wait.until(lambda d: d.execute_script("return document.getElementById('matter-rail').getBoundingClientRect().right <= 1"))
            wait.until(lambda d: d.execute_script('return document.documentElement.scrollWidth <= innerWidth'))
            driver.save_screenshot(str(args.output / 'entity-mobile.png'))
            driver.execute_script("arguments[0].scrollIntoView({block:'start',behavior:'instant'})", driver.find_element(By.ID, 'entity-form-heading'))
            driver.save_screenshot(str(args.output / 'entity-mobile-recovery.png'))
            record('390-pixel reflow without horizontal page overflow')
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            driver.set_window_size(1440, 1000)
            click('.notebook-item-form button')
            assert 'Unsaved synthetic correction' in body()
            go(prefix + '/entities?return_to=' + prefix.replace('/', '%2F') + '%3Fmode%3Dsearch%26q%3Ddepot')
            driver.find_element(By.LINK_TEXT, 'Return to source review').click()
            assert 'mode=search' in driver.current_url and 'q=depot' in driver.current_url
            record('Return link retains the source-review search context')
            receipt = dict(synthetic=True, success=True, checks=checks, browser=driver.capabilities.get('browserVersion'))
            (args.output / 'receipt.json').write_text(json.dumps(receipt, indent=2))
        except Exception as exc:
            if drivers:
                drivers[0].save_screenshot(str(args.output / 'failure.png'))
            (args.output / 'receipt.json').write_text(json.dumps(dict(synthetic=True, success=False, checks=checks, error=type(exc).__name__), indent=2))
            raise
        finally:
            for driver in drivers:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == '__main__':
    main()
