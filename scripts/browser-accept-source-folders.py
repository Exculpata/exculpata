#!/usr/bin/env python3
"""Synthetic folder navigation through Chrome and disposable loopback state."""
from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from browser_case_exit import BrowserCaseExit, completed_zip, preview_login  # noqa: E402
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402

ACTOR = 'development-taylor-morgan'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    downloads = output / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_only': True, 'passed': False, 'checks': []}

    def checked(message):
        report['checks'].append(message)
        print(message, flush=True)

    with tempfile.TemporaryDirectory(prefix='recordbench-folder-browser-') as temp:
        root = Path(temp).resolve()
        folder = root / 'Generated collection'
        fixtures = {
            'North/report.txt': b'Synthetic copper journal: the north vehicle arrived at noon.\n',
            'North/Sub/notes.txt': b'Synthetic nested northern interview notes.\n',
            'Northwest/report.txt': b'Synthetic northwest distinct source.\n',
            'South/report.txt': b'Synthetic southern distinct source.\n',
            'North-reference.txt': b'Synthetic root reference distinct source.\n',
            'Café/证据/report.txt': b'Synthetic unicode folder source.\n',
            '100%_batch/memo.txt': b'Synthetic literal punctuation folder source.\n',
        }
        for relative, body in fixtures.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        app = create_workbench_app(root / 'runtime', generator=UnavailableGenerator(),
            auth_mode='preview', background_ingestion=True, ingestion_workers=1)
        bench = app.state.workbench
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline:
                raise AssertionError('Synthetic server did not start')
            time.sleep(.05)
        base = f'http://127.0.0.1:{port}'
        options = Options()
        options.binary_location = str(args.chrome_binary)
        options.page_load_strategy = 'none'
        for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
                     '--disable-gpu', '--no-proxy-server', '--window-size=1440,1000'):
            options.add_argument(flag)
        options.add_experimental_option('prefs', {'download.default_directory': str(downloads),
            'download.prompt_for_download': False})
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 30)

            def ready():
                wait.until(lambda d: d.execute_script('return document.readyState') == 'complete')

            def detached(element):
                try:
                    return EC.staleness_of(element)(driver)
                except WebDriverException as exc:
                    # Some ChromeDriver versions report detached navigation nodes
                    # as an inspector error instead of StaleElementReferenceException.
                    if 'Node with given id does not belong to the document' not in exc.msg:
                        raise
                    return True

            def go(path):
                old = driver.find_element(By.TAG_NAME, 'html')
                driver.get(base + path)
                wait.until(lambda _: detached(old))
                ready()

            def click_element(element, navigation=True):
                driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"});', element)
                wait.until(lambda d: d.execute_script('const r=arguments[0].getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight;', element))
                element.click()
                if navigation:
                    wait.until(lambda _: detached(element))
                    ready()

            def click(selector, navigation=True):
                click_element(driver.find_element(By.CSS_SELECTOR, selector), navigation)

            def child(name):
                link = next(a for a in driver.find_elements(By.CSS_SELECTOR, '.source-folder-list a')
                    if a.find_element(By.TAG_NAME, 'span').text == name)
                click_element(link)

            def rows():
                return driver.find_elements(By.CSS_SELECTOR, '.source-table-row')

            preview_login(driver, base)
            go('/matters/new')
            driver.find_element(By.ID, 'matter-name').send_keys('Synthetic folder acceptance')
            click('.matter-form button[type=submit]')
            slug = urlparse(driver.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            matter = bench.matter(slug, ACTOR)
            click('[data-assistant-collapse]', False)
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-folder-input]').is_enabled())
            collection_name = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic source-folders collection")
            driver.find_element(By.CSS_SELECTOR, '[data-folder-input]').send_keys(str(folder))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]').text == 'Upload 7 ready files')
            click('[data-upload-preflight-confirm]', False)
            wait.until(lambda _: len(bench.source_store(matter).documents) == 7
                and not any(bench.workspace.active_matter_work_counts(matter.matter_id).values()))
            go(prefix + '/setup?view=list')
            child('Generated collection')
            assert len(rows()) == 7
            child('North')
            assert len(rows()) == 2
            assert all('Generated collection/North' in row.text for row in rows())
            assert 'Northwest' not in driver.find_element(By.CSS_SELECTOR, '.source-library-table').text
            north_url = driver.current_url
            child('Sub')
            assert len(rows()) == 1 and 'notes.txt' in rows()[0].text
            old = driver.find_element(By.TAG_NAME, 'html')
            driver.refresh()
            wait.until(lambda _: detached(old))
            ready()
            assert len(rows()) == 1
            click('.source-folder-parent')
            assert len(rows()) == 2
            driver.back()
            wait.until(lambda d: parse_qs(urlparse(d.current_url).query).get('folder') == ['Generated collection/North/Sub'])
            ready()
            assert len(rows()) == 1
            go(urlparse(north_url).path + '?' + urlparse(north_url).query)
            checked('Actual nested upload, exact sibling boundaries, reload, parent and browser back')

            # Reuse existing staff group actions; no second organization model.
            click('[data-source-select-all]', False)
            Select(driver.find_element(By.CSS_SELECTOR, '[data-source-bulk-action]')).select_by_value('create_set')
            driver.find_element(By.CSS_SELECTOR, '[data-bulk-set-name]').send_keys('Generated northern review')
            click('[data-source-bulk-submit]')
            groups = bench.workspace.source_sets(matter.matter_id)
            group = next(g for g in groups if g.name == 'Generated northern review')
            go(prefix + '/setup?' + urlencode({'view': 'list', 'folder': 'Generated collection/North',
                'source_set': group.source_set_id, 'status': 'ready'}))
            assert len(rows()) == 2
            Select(driver.find_element(By.CSS_SELECTOR, '.source-library-filters select[name=sort]')).select_by_value('name')
            click('.source-library-filters button')
            query = parse_qs(urlparse(driver.current_url).query)
            assert query['folder'] == ['Generated collection/North']
            assert query['source_set'] == [group.source_set_id] and query['status'] == ['ready']
            assert len(rows()) == 2 and bench.workspace.source_set(matter.matter_id, group.source_set_id).source_count == 2
            checked('Existing bulk source-set creation and Apply preserve folder, source set and status')

            click('.source-open-action')
            source_links = driver.find_elements(By.CSS_SELECTOR, '[data-source-browser] li a')
            assert len(source_links) == 2
            assert source_links[0].get_attribute('aria-current') == 'page'
            source_links[0].send_keys(Keys.ARROW_DOWN)
            assert driver.switch_to.active_element == source_links[1]
            source_links[1].send_keys(Keys.HOME)
            assert driver.switch_to.active_element == source_links[0]
            source_links[0].send_keys(Keys.END)
            assert driver.switch_to.active_element == source_links[1]
            old = driver.find_element(By.TAG_NAME, 'html')
            driver.switch_to.active_element.send_keys(Keys.ENTER)
            wait.until(lambda _: detached(old))
            ready()
            assert 'nested northern interview notes' in driver.find_element(By.TAG_NAME, 'body').text
            assert driver.find_element(By.CSS_SELECTOR, '[data-source-browser] a[aria-current]').text.startswith('notes.txt')
            assert driver.find_element(By.CSS_SELECTOR, '.review-sequence .next.disabled').is_displayed()
            for width in (1440, 390):
                driver.set_window_size(width, 1000)
                if width == 390 and driver.find_element(By.CSS_SELECTOR, '[data-rail-toggle]').get_attribute('aria-expanded') == 'true':
                    click('[data-rail-toggle]', False)
                wait.until(lambda d: not d.execute_script('return document.getAnimations().some(a => a.playState === "running")'))
                panel = driver.find_element(By.CSS_SELECTOR, '[data-source-browser]')
                driver.execute_script('arguments[0].scrollIntoView({block:"start",behavior:"instant"});window.scrollBy(0,-75);', panel)
                assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
                driver.save_screenshot(str(output / f'synthetic-continuous-{width}.png'))
            driver.set_window_size(1440, 1000)
            click('.review-sequence .previous')
            assert 'north vehicle arrived at noon' in driver.find_element(By.TAG_NAME, 'body').text
            click('[data-source-browser] > a')
            query = parse_qs(urlparse(driver.current_url).query)
            assert query['folder'] == ['Generated collection/North']
            assert query['source_set'] == [group.source_set_id] and query['status'] == ['ready']
            assert query['sort'] == ['name'] and len(rows()) == 2
            checked('Continuous source list, keyboard focus and Enter, previous/next boundaries, retained filters and responsive viewer')

            for width in (1440, 390):
                driver.set_window_size(width, 1000)
                wait.until(lambda d: not d.execute_script('return document.getAnimations().some(a => a.playState === "running")'))
                nav = driver.find_element(By.CSS_SELECTOR, '[data-source-folder-nav]')
                driver.execute_script('arguments[0].scrollIntoView({block:"start",behavior:"instant"});window.scrollBy(0,-75);', nav)
                assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
                driver.save_screenshot(str(output / f'synthetic-folders-{width}.png'))
            driver.set_window_size(1440, 1000)
            checked('Desktop and narrow layout retain visible folder navigation without horizontal overflow')

            go(prefix + '/setup?view=list')
            child('Generated collection')
            child('Café')
            child('证据')
            assert len(rows()) == 1
            click('.source-open-action')
            assert 'Synthetic unicode folder source.' in driver.find_element(By.TAG_NAME, 'body').text
            go(prefix + '/setup?view=list')
            child('Generated collection')
            child('100%_batch')
            assert len(rows()) == 1
            click('.source-open-action')
            assert 'Synthetic literal punctuation folder source.' in driver.find_element(By.TAG_NAME, 'body').text
            checked('Unicode and percent/underscore folder links open their exact source passages')

            go(prefix + '?' + urlencode({'mode': 'search', 'q': 'copper journal'}))
            click('.open-support-link')
            assert 'the north vehicle arrived at noon' in driver.find_element(By.ID, 'support-pane').text
            click('.support-save-button')
            assert len(bench.workspace.all_notebook_items(matter.matter_id, ACTOR)) == 1
            checked('Normal search opens exact northern source support and saves it to case notes')

            other = 'generated-foreign-owner'
            bench.workspace.upsert_principal('test', other, 'Generated foreign owner', other, preferred_principal_id=other)
            foreign = bench.create_matter('Generated foreign folder canary', '', other)
            go(f'/matters/{foreign.slug}/setup?folder=Generated')
            assert 'Generated foreign folder canary' not in driver.find_element(By.TAG_NAME, 'body').text
            go(prefix + '/setup?folder=Unknown')
            assert len(rows()) == 0 and driver.find_element(By.CSS_SELECTOR, '.source-folder-parent').is_displayed()
            checked('Foreign matter stays inaccessible; unknown folder has an empty view and parent recovery')

            go(prefix + '/settings')
            before_bundle = set(downloads.iterdir())
            click(f'a[href="{prefix}/export"]', False)
            bundle = wait.until(lambda _: completed_zip(downloads, before_bundle))
            with zipfile.ZipFile(bundle) as archive:
                assert 'the north vehicle arrived at noon' in archive.read('notebook/matter-notebook.md').decode()
                assert not any(name.startswith('originals/') for name in archive.namelist())
                assert all(archive.read(name) not in fixtures.values() for name in archive.namelist() if not name.endswith('/'))
            exit_flow = BrowserCaseExit(driver, base, app, matter, downloads, preview_subject="taylor-morgan")
            exit_flow.prepare()
            exit_flow.confirm_name(matter.display_name)
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted')
            assert all((folder / relative).read_bytes() == body for relative, body in fixtures.items())
            checked('Final browser bundle retains source-supported notes; deliberate close preserves every external original')
            report['passed'] = True
        except Exception:
            if driver is not None:
                driver.save_screenshot(str(output / 'failure.png'))
                (output / 'failure.html').write_text(driver.page_source)
            raise
        finally:
            (output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == '__main__':
    main()
