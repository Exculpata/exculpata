#!/usr/bin/env python3
"""Synthetic original-source/event review in a disposable app with no model."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlencode, urlparse

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'
ORIGINALS = (
    ('supporting-account', 'Morgan Sample says Alex Example delivered the red parcel to Cedar Depot on 03/04/2026.'),
    ('competing-account', 'Riley Demo says Alex Example did not deliver the red parcel to Cedar Depot on 03/04/2026.'),
    ('unrelated-identity', 'Alex Example, the unrelated museum volunteer, catalogued postcards.'),
    ('visitor-list', 'The visitor list contains Jordan Sample and Casey Demo. No interaction or relationship is recorded.'),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise ValueError('Choose an empty output directory.')
    isolate_environment()
    checks = []
    drivers = []

    def record(message):
        checks.append(message)
        print(message, flush=True)

    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-assertions-browser-") as temporary:
        root = Path(temporary).resolve()
        downloads = root / 'downloads'
        downloads.mkdir()
        originals = []
        for name, content in ORIGINALS:
            original = root / ('generated-' + name + '.txt')
            original.write_text(content)
            originals.append(original)
        app = create_workbench_app(root / 'runtime', generator=UnavailableGenerator(),
            auth_mode='test', background_ingestion=True)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False, ws='none'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            def browser():
                options = Options()
                options.binary_location = str(args.chrome_binary)
                for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1000'):
                    options.add_argument(flag)
                options.add_experimental_option('prefs', {'download.default_directory': str(downloads),
                    'download.prompt_for_download': False, 'download.directory_upgrade': True})
                current = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
                current.set_page_load_timeout(30)
                current.set_script_timeout(30)
                drivers.append(current)
                return current

            driver = browser()
            wait = WebDriverWait(driver, 20)
            wait.until(lambda _: server.started)

            def body(current=None):
                try:
                    return (current or driver).find_element(By.TAG_NAME, 'body').text
                except StaleElementReferenceException:
                    return ''

            def go(path):
                navigate_to(driver, base + path, diagnostics=args.output)

            def detached(element):
                def check(current):
                    try:
                        return EC.staleness_of(element)(current)
                    except WebDriverException as exc:
                        if 'Node with given id does not belong to the document' not in exc.msg:
                            raise
                        return True
                return check

            def click_element(element, *, current=None):
                current = current or driver
                current.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", element)
                element.click()
                WebDriverWait(current, 20).until(detached(element))
                WebDriverWait(current, 20).until(lambda d: d.execute_script('return document.readyState') == 'complete')

            def click(selector):
                click_element(driver.find_element(By.CSS_SELECTOR, selector))

            def form(action, current=None):
                return (current or driver).find_element(By.CSS_SELECTOR,
                    f'form:has(input[name="action"][value="{action}"])')

            def fill(name, text, *, action=None, current=None):
                current = current or driver
                element = (form(action, current) if action else current).find_element(By.CSS_SELECTOR, f'[name="{name}"]')
                element.clear()
                element.send_keys(text)

            def select(name, value, *, action, current=None):
                Select(form(action, current).find_element(By.CSS_SELECTOR, f'select[name="{name}"]')).select_by_value(value)

            def submit(action, *, current=None):
                click_element(form(action, current).find_element(By.CSS_SELECTOR, 'button[type="submit"], button:not([type])'), current=current)

            def source(term):
                go(prefix + '?mode=search&q=' + term)
                click('a.open-support-link')
                wait.until(lambda d: d.find_element(By.ID, 'support-pane'))
                assert term in driver.find_element(By.ID, 'support-pane').text

            def create_identity(term, name, entity_type='person'):
                source(term)
                click('a[href*="/entities?support="]')
                fill('display_name', name)
                select('entity_type', entity_type, action='create')
                submit('create')
                path = urlparse(driver.current_url).path
                assert 'Original-source mentions (1)' in body()
                return path

            def create_record(title, record_type='event', *, keyboard=False):
                go(entity_path)
                click_element(driver.find_element(By.LINK_TEXT, 'Create event or assertion'))
                assert 'Morgan Sample' in body()
                select('record_type', record_type, action='create')
                fill('title', title, action='create')
                fill('statement', 'Alex Example delivered the red parcel to Cedar Depot.', action='create')
                fill('raw_date', '03/04/2026', action='create')
                fill('date_uncertainty', 'Day/month order and time zone are unresolved.', action='create')
                fill('attributed_to', 'Morgan Sample, supporting account', action='create')
                support_choices = form('create').find_elements(By.CSS_SELECTOR, 'select[name="support"]')
                if support_choices:
                    Select(support_choices[0]).select_by_index(1)
                if keyboard:
                    form('create').find_element(By.CSS_SELECTOR, '[name="title"]').click()
                    for _ in range(30):
                        active = driver.switch_to.active_element
                        if active.tag_name == 'button' and active.get_attribute('type') == 'submit':
                            active.send_keys(Keys.ENTER)
                            wait.until(detached(active))
                            break
                        active.send_keys(Keys.TAB)
                    else:
                        raise AssertionError('Keyboard traversal did not reach the creation button')
                else:
                    submit('create')
                path = urlparse(driver.current_url).path
                assert '/assertions/' in path and not path.endswith('/new')
                return path

            go('/matters/new')
            element = driver.find_element(By.ID, 'matter-name')
            element.send_keys('Generated assertion evidence review')
            click('.matter-form button[type="submit"]')
            slug = urlparse(driver.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            driver.find_element(By.CSS_SELECTOR, '[data-assistant-collapse]').click()
            collection_name = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic assertions collection")
            driver.find_element(By.ID, 'source-files').send_keys('\n'.join(str(path) for path in originals))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]').is_enabled())
            confirm = driver.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", confirm)
            confirm.click()
            bench = app.state.workbench
            matter = bench.matter(slug, ACTOR)
            wait.until(lambda _: len([document for document in bench.source_store(matter).documents.values() if document.state == 'ready']) == 4)
            record('Four synthetic originals uploaded and searchable with generation unavailable')
            for width in (1440, 1200):
                driver.set_window_size(width, 1000)
                source('Morgan')
                save = driver.find_element(By.CSS_SELECTOR, '.support-save-button')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", save)
                # The real pointer target must be the button, rather than an
                # overlapping source-navigation arrow or another toolbar item.
                assert driver.execute_script('''const button = arguments[0];
                    const box = button.getBoundingClientRect();
                    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
                    return !!hit && button.contains(hit);''', save)
                if width == 1200:
                    assert driver.find_element(By.ID, 'support-pane').rect['width'] <= 400
                driver.save_screenshot(str(args.output / f'assertion-source-toolbar-{width}.png'))
                click_element(save)
                assert urlparse(driver.current_url).path == prefix + '/notebook'
                assert ORIGINALS[0][1] in body()
            driver.set_window_size(1440, 1000)
            record('Source toolbar saves the original to case notes through real pointer clicks at desktop and narrow-pane widths without overlapping navigation controls')
            entity_path = create_identity('Morgan', 'Alex Example')
            driver.find_element(By.LINK_TEXT, 'Return to source review').click()
            assert 'mode=search' in driver.current_url and 'q=Morgan' in driver.current_url
            record('Search original passage, attach to entity, and return to the same source-review query')
            event_path = create_record('Disputed parcel delivery', keyboard=True)
            event_id = event_path.split('/')[-1]
            record('Keyboard traversal creates an event with typed entity role, raw uncertain date, attribution, and supporting original')
            source('Riley')
            click('a[href*="/chronology?support="]')
            click(f'a[href*="/assertions/{event_id}"]')
            select('stance', 'competing', action='attach')
            fill('attributed_to', 'Riley Demo, competing account', action='attach')
            submit('attach')
            assert 'Morgan Sample' in body() and 'Riley Demo' in body()
            driver.save_screenshot(str(args.output / 'assertion-desktop.png'))
            driver.execute_script("arguments[0].scrollIntoView({block:'start',behavior:'instant'})", driver.find_element(By.ID, 'accounts-heading'))
            driver.save_screenshot(str(args.output / 'assertion-accounts.png'))
            detail = bench.assertion_service(matter).detail(matter.matter_id, ACTOR, event_id)
            assert detail['record']['status'] == 'needs_review'
            assert detail['record']['raw_date'] == '03/04/2026' and not detail['record']['sort_date']
            assert {account['stance'] for account in detail['accounts']} == {'supporting', 'competing'}
            assert {account['excerpt'] for account in detail['accounts']} == {ORIGINALS[0][1], ORIGINALS[1][1]}
            review_return = prefix + '?mode=search&q=Riley#search-results'
            for index in range(2):
                go(event_path + '?' + urlencode(dict(return_to=review_return)))
                links = driver.find_elements(By.LINK_TEXT, 'Open original passage')
                assert len(links) >= 2
                click_element(links[index])
                wait.until(lambda d: d.find_element(By.ID, 'support-pane'))
                assert any(content in driver.find_element(By.ID, 'support-pane').text for _, content in ORIGINALS[:2])
                if index == 1:
                    click('a.support-header-open')
                click_element(driver.find_element(By.LINK_TEXT, 'Return to review context'))
                wait.until(lambda _: 'Disputed parcel delivery' in body())
                assert urlparse(driver.current_url).path == event_path
                assert parse_qs(urlparse(driver.current_url).query)['return_to'] == [review_return]
            record('Both attributed accounts open their original passages; support pane and full source return directly to the same assertion and review context')
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=False))
            go(event_path + '?' + urlencode(dict(return_to=review_return)))
            click_element(driver.find_elements(By.LINK_TEXT, 'Open original passage')[0])
            wait.until(lambda d: d.find_element(By.ID, 'support-pane'))
            # The page behind the drawer keeps its own return link, which may scroll under the
            # sticky question box; the drawer's link is the one the reviewer must be able to reach.
            return_link = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '#support-pane a.support-return-context')))
            # Judge the settled drawer, not a frame of its slide-in animation.
            wait.until(lambda d: d.execute_script(
                "return document.getElementById('support-pane').getAnimations().every(a => a.playState !== 'running')"))
            assert return_link.text == 'Return to review context'
            wait.until(lambda d: d.execute_script('''const link = arguments[0];
                const box = link.getBoundingClientRect();
                const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
                return box.top >= 0 && box.bottom <= innerHeight && !!hit && link.contains(hit);''', return_link))
            assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
            driver.save_screenshot(str(args.output / 'assertion-source-return-mobile.png'))
            click_element(return_link)
            assert urlparse(driver.current_url).path == event_path
            assert parse_qs(urlparse(driver.current_url).query)['return_to'] == [review_return]
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            driver.set_window_size(1440, 1000)
            record('At 390 pixels the source drawer exposes a visible clickable return link to the same assertion and source-review query')
            depot_path = create_identity('Morgan', 'Cedar Depot', entity_type='place')
            go(event_path)
            role_form = form('add_role')
            role_details = role_form.find_element(By.XPATH, 'ancestor::details')
            summary = role_details.find_element(By.TAG_NAME, 'summary')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", summary)
            summary.click()
            choices = Select(form('add_role').find_element(By.CSS_SELECTOR, 'select[name="entity_selection"]'))
            selected = next(option for option in choices.options if option.text.startswith('Cedar Depot'))
            choices.select_by_value(selected.get_attribute('value'))
            select('role', 'location', action='add_role')
            submit('add_role')
            roles = bench.assertion_service(matter).detail(matter.matter_id, ACTOR, event_id)['roles']
            assert {(role['entity_id'], role['role']) for role in roles} == {
                (entity_path.split('/')[-1], 'subject'), (depot_path.split('/')[-1], 'location')}
            record('Reviewer explicitly links the person as subject and the depot as location through labeled role controls')
            go(event_path)
            select('status', 'confirmed', action='update')
            submit('update')
            select('status', 'disputed', action='update')
            fill('title', 'Delivery accounts remain incompatible', action='update')
            fill('statement', 'Morgan reports a delivery; Riley denies that delivery. Review has not resolved the disagreement.', action='update')
            submit('update')
            after = bench.assertion_service(matter).detail(matter.matter_id, ACTOR, event_id)
            assert after['record']['status'] == 'disputed' and after['accounts'] == detail['accounts']
            record('Reviewer confirms, then disputes and corrects the interpretation without changing either source account')
            unrelated_path = create_identity('museum', 'Alex Example')
            assert unrelated_path != entity_path and 'Delivery accounts remain incompatible' not in body()
            for name in ('Jordan Sample', 'Casey Demo'):
                create_identity('visitor', name)
                assert 'Delivery accounts remain incompatible' not in body()
            record('Unrelated same-name identity and co-mentioned people acquire no event or relationship automatically')
            temporary_path = create_record('Temporary reviewer assertion', record_type='assertion')
            fill('title', 'Corrected temporary assertion', action='update')
            select('status', 'disputed', action='update')
            submit('update')
            delete_form = form('delete')
            enclosing = delete_form.find_elements(By.XPATH, 'ancestor::details[not(@open)]')
            for details_element in enclosing:
                summary = details_element.find_element(By.TAG_NAME, 'summary')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", summary)
                summary.click()
            submit('delete')
            go(prefix + '/chronology')
            assert 'Corrected temporary assertion' not in body()
            record('A separate assertion can be corrected and removed from chronology')
            go(event_path)
            other = browser()
            navigate_to(other, base + event_path, diagnostics=args.output)
            fill('title', 'Another reviewer saved title', action='update', current=other)
            submit('update', current=other)
            fill('title', 'Unsaved reviewer correction', action='update')
            fill('statement', 'Unsaved interpretation kept for comparison.', action='update')
            fill('raw_date', 'around 03/04/2026', action='update')
            fill('date_uncertainty', 'Unsaved date caveat', action='update')
            submit('update')
            assert 'changed since you opened' in body()
            assert 'Another reviewer saved title' in body()
            for name, value in (('title', 'Unsaved reviewer correction'), ('statement', 'Unsaved interpretation kept for comparison.'),
                                ('raw_date', 'around 03/04/2026'), ('date_uncertainty', 'Unsaved date caveat')):
                assert form('update').find_element(By.CSS_SELECTOR, f'[name="{name}"]').get_attribute('value') == value
            driver.save_screenshot(str(args.output / 'assertion-conflict.png'))
            record('Two-browser stale edit preserves all submitted interpretation and date fields beside the saved revision')
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=1, mobile=False))
            wait.until(lambda d: d.execute_script('return document.documentElement.scrollWidth <= innerWidth'))
            wait.until(lambda d: d.execute_script("return document.getElementById('matter-rail').getBoundingClientRect().right <= 1"))
            driver.save_screenshot(str(args.output / 'assertion-mobile.png'))
            driver.execute_script("arguments[0].scrollIntoView({block:'start',behavior:'instant'})", form('update'))
            driver.save_screenshot(str(args.output / 'assertion-mobile-recovery.png'))
            record('390-pixel reflow preserves evidence and correction controls without horizontal page overflow')
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            driver.set_window_size(1440, 1000)
            fill('title', 'Delivery accounts remain incompatible', action='update')
            fill('statement', 'Morgan reports a delivery; Riley denies that delivery. Review has not resolved the disagreement.', action='update')
            fill('raw_date', '03/04/2026', action='update')
            fill('date_uncertainty', 'Day/month order and time zone are unresolved.', action='update')
            submit('update')
            go('/matters/new')
            go(prefix + '/chronology')
            assert 'Delivery accounts remain incompatible' in body()
            driver.save_screenshot(str(args.output / 'assertion-chronology.png'))
            click(f'a[href*="/assertions/{event_id}"]')
            assert 'Morgan Sample' in body() and 'Riley Demo' in body()
            record('Leaving and returning through chronology restores the reviewed disagreement and source support')
            before = set(downloads.iterdir())
            export_link = driver.find_element(By.CSS_SELECTOR, f'a[href="{event_path}/export"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", export_link)
            export_link.click()
            wait.until(lambda _: any(path not in before and path.suffix == '.json' for path in downloads.iterdir()))
            exported_path = next(path for path in downloads.iterdir() if path not in before and path.suffix == '.json')
            exported = json.loads(exported_path.read_text())
            assert exported['record']['status'] == 'disputed'
            assert exported['record']['raw_date'] == '03/04/2026'
            assert {account['stance'] for account in exported['accounts']} == {'supporting', 'competing'}
            assert {account['excerpt'] for account in exported['accounts']} == {ORIGINALS[0][1], ORIGINALS[1][1]}
            assert exported['history'] and {(role['entity_id'], role['role']) for role in exported['roles']} == {
                (entity_path.split('/')[-1], 'subject'), (depot_path.split('/')[-1], 'location')}
            record('Visible export action downloads review status, typed role, uncertain date, history, and both incompatible original accounts')
            (args.output / 'receipt.json').write_text(json.dumps(dict(synthetic_only=True, passed=True,
                checks=checks, browser=driver.capabilities.get('browserVersion'),
                scope='Manual browser workflow without a model. Complete original-source restore, authorization, source changes and purge are covered by test_assertion_workflow.py.'), indent=2))
        except Exception as exc:
            if drivers:
                drivers[0].save_screenshot(str(args.output / 'failure.png'))
            (args.output / 'receipt.json').write_text(json.dumps(dict(synthetic_only=True, passed=False,
                checks=checks, error=type(exc).__name__), indent=2))
            raise
        finally:
            for driver in drivers:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == '__main__':
    main()
