#!/usr/bin/env python3
"""Synthetic discovery and reconciliation journey in a disposable loopback app, without a model."""
from __future__ import annotations
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
from selenium.common.exceptions import WebDriverException, StaleElementReferenceException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait, Select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'


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
    with tempfile.TemporaryDirectory(prefix='recordbench-entities-browser-') as temporary:
        root = Path(temporary).resolve()
        originals = []
        for name, text in [('depot', 'Alex Example visited the synthetic depot on 03/04/2026.'), ('archive', 'Alex Example called the synthetic archive on 03/04/2026.')]:
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
            wait = WebDriverWait(driver, 20, ignored_exceptions=(StaleElementReferenceException,))
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
            def click(selector, navigation=True):
                element = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", element)
                element.click()
                if not navigation:
                    return
                wait.until(detached(element))
                wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')
            def body():
                return driver.execute_script('return document.body ? document.body.innerText : ""')
            go('/matters/new')
            fill('#matter-name', 'Generated entity review')
            click('.matter-form button[type=submit]')
            slug = urlparse(driver.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            driver.find_element(By.CSS_SELECTOR, '[data-assistant-collapse]').click()
            collection_name = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic entity-discovery collection")
            driver.find_element(By.ID, 'source-files').send_keys('\n'.join(str(path) for path in originals))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]').is_enabled())
            confirm = driver.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", confirm)
            confirm.click()
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            wait.until(lambda _: len([d for d in bench.source_store(matter).documents.values() if d.state == 'ready']) == 2)
            record('Two synthetic originals uploaded and searchable without a model')
            from case_intelligence.intake_receipts import IntakeReceipts
            receipt = IntakeReceipts(bench.workspace).recent(matter.matter_id, ACTOR)[0]
            go(prefix + '/intake/' + receipt['receipt_id'])
            click('a[href*="/entities?return_to="]')
            assert 'No frozen text-review population yet' in body()
            click('a[href*="/intake/"]')
            assert 'Selected-file receipt' in body()
            record('Intake receipt leads to discovery readiness and preserves the receipt return path')
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
            # Populate slice-12 durable coverage without making an AI call.
            from case_intelligence.full_text_review import FullTextReviewLedger
            bench.full_review.close()
            _, version = bench.workspace.create_review_criterion(matter.matter_id, ACTOR,
                title='Generated entity discovery', instructions='Review synthetic people.')
            run = bench.workspace.queue_review_run(matter.matter_id, ACTOR, version.criterion_version_id,
                run_kind='full', review_mode='full_text')
            run = bench.workspace.claim_review_run('synthetic-browser-coverage')
            decision = bench.workspace.next_review_decision(run.run_id)
            document = bench.source_store(matter).get(decision.document_id)
            FullTextReviewLedger(bench.workspace).inventory(run, decision, document.parsed_units(), current_source=lambda: True)
            bench.workspace.fail_review_run(run.run_id, 'Synthetic inventory fixture finished; no generation requested.')
            go(prefix + '/entities')
            guided = driver.find_element(By.CSS_SELECTOR, 'details.guided-discovery')
            if guided.get_attribute('open') is None:
                guided.find_element(By.CSS_SELECTOR, ':scope > summary').click()
            guided.find_element(By.CSS_SELECTOR, ':scope > details > summary').click()
            click('a.button[href*="/entity-discovery/"]')
            assert '1 not yet inventoried' in body()
            click('form:has(input[value="discover"]) button')
            assert '1 units processed' in body()
            record('Discovery processes an inventoried unit without a model and shows remaining uninventoried sources honestly')
            assert '/entity-discovery/' in driver.current_url
            assert 'No runnable units remain' in body()
            driver.save_screenshot(str(args.output / 'guided-discovery-desktop.png'))
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=False))
            wait.until(lambda d: d.execute_script('return document.documentElement.scrollWidth <= innerWidth'))
            wait.until(lambda d: d.execute_script("return document.getElementById('matter-rail').getBoundingClientRect().right <= 1"))
            driver.save_screenshot(str(args.output / 'guided-discovery-mobile.png'))
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            driver.set_window_size(1440, 1000)
            assert len(driver.find_elements(By.CSS_SELECTOR, '[data-discovery-unit]')) == 1
            assert 'Showing up to 50 units' in body()
            record('Coverage details use a separate bounded source and unit page')
            rows = bench.entity_service(matter).list(matter.matter_id, ACTOR)[0]
            date = next(row for row in rows if row['entity_type'] == 'date')
            go(prefix + '/entities/' + date['entity_id'])
            assert '03/04/2026' in body() and 'unresolved' in body()
            assert 'Suggested' in body()
            click('a[href*="/assertions/new"][href*="support="]:not([href*="support=&"])')
            assert 'Selected original passage' in body() and '03/04/2026' in body()
            assert 'No events or assertions yet' in (go(prefix + '/chronology') or body())
            record('Suggested date retains ambiguity and cited event handoff; opening the form creates no event')
            discovered = next(row for row in rows if row['extractor_version'] and row['entity_type'] == 'person')
            discovered_path = prefix + '/entities/' + discovered['entity_id']
            go(discovered_path)
            assert 'Machine occurrence:' in body() and 'deterministic-entities-v1' in body()
            driver.save_screenshot(str(args.output / 'discovery-desktop.png'))
            click('form:has(input[value="merge"]) button')
            assert 'Undo merge' in body()
            click('form:has(input[value="undo"]) button')
            assert 'Original-source mentions (1)' in body()
            record('Reviewer confirms a candidate merge and undoes it without losing the extracted mention')
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=False))
            wait.until(lambda d: d.execute_script('return document.documentElement.scrollWidth <= innerWidth'))
            wait.until(lambda d: d.execute_script("return document.getElementById('matter-rail').getBoundingClientRect().right <= 1"))
            driver.save_screenshot(str(args.output / 'discovery-mobile.png'))
            heading = driver.find_element(By.XPATH, "//h2[text()='Reviewer reconciliation']")
            driver.execute_script("arguments[0].scrollIntoView({block:'start',behavior:'instant'})", heading)
            driver.save_screenshot(str(args.output / 'reconciliation-mobile.png'))
            record('Discovery and reconciliation reflow at 390 pixels with native labeled controls')
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            driver.set_window_size(1440, 1000)
            go(prefix + '/entities')
            fill('input[name=display_name]', 'Unrelated synthetic identity')
            click('.notebook-item-form button')
            target = next(row for row in rows if not row['extractor_version'] and row['display_name'] == 'Alex Example')
            target = bench.entity_service(matter).detail(matter.matter_id, ACTOR, target['entity_id'])[0]
            click('details:has(select[name=mention_id]) summary', navigation=False)
            fill('input[name=target_id]', target['entity_id'])
            fill('input[name=target_revision]', str(target['revision']))
            Select(driver.find_element(By.CSS_SELECTOR, 'select[name=action]')).select_by_value('alias')
            click('form:has(select[name=action]) button')
            assert 'Undo alias' in body() and 'Original-source mentions (0)' in body()
            click('form:has(input[value="undo"]) button')
            record('Dissimilar empty manual identities can confirm and undo an alias without selecting a split mention')
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
