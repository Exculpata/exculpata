#!/usr/bin/env python3
"""Synthetic separate upload occurrences through Chrome and disposable loopback state."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to, refresh_page

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urlparse

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
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
    parser.add_argument('--verify-byte-matches', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    downloads = output / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_only': True, 'passed': False, 'checks': []}

    def checked(message):
        report['checks'].append(message)
        print(message, flush=True)

    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-occurrence-browser-") as temp:
        root = Path(temp).resolve()
        bodies = [b'Synthetic copper journal: the first vehicle arrived at noon.\n',
                  b'Synthetic copper journal: the first vehicle arrived at noon.\n',
                  b'Synthetic silver journal: the later vehicle arrived at dusk.\n']
        originals = []
        for index, body in enumerate(bodies):
            path = root / f'Generated-{index + 1}' / 'Records' / 'report.txt'
            path.parent.mkdir(parents=True)
            path.write_bytes(body)
            originals.append(path)
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
                navigate_to(driver, base + path, diagnostics=args.output)
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

            def rows():
                return driver.find_elements(By.CSS_SELECTOR, '.source-table-row')

            preview_login(driver, base)
            go('/matters/new')
            driver.find_element(By.ID, 'matter-name').send_keys('Synthetic production occurrences')
            click('.matter-form button[type=submit]')
            slug = urlparse(driver.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            matter = bench.matter(slug, ACTOR)
            click('[data-assistant-collapse]', False)
            from case_intelligence.intake_receipts import IntakeReceipts
            receipts = IntakeReceipts(bench.workspace)
            documents = []
            collections = []
            for index, original in enumerate(originals):
                if index:
                    go(prefix + '/setup')
                wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-folder-input]').is_enabled())
                driver.find_element(By.CSS_SELECTOR, '[data-folder-input]').send_keys(str(original.parent))
                wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]').text == 'Upload 1 ready file')
                field = driver.find_element(By.CSS_SELECTOR, '[data-upload-collection-name]')
                field.clear()
                title = f'Generated production {index + 1}'
                field.send_keys(title)
                click('[data-upload-preflight-confirm]', False)
                wait.until(lambda _: len(bench.source_store(matter).documents) == index + 1
                    and not any(bench.workspace.active_matter_work_counts(matter.matter_id).values()))
                receipt = receipts.recent(matter.matter_id, ACTOR)[0]
                assert receipt['collection_name'] == title and receipt['counts']['searchable'] == 1
                click('[data-intake-receipt-open]')
                click_element(driver.find_element(By.LINK_TEXT, 'Open source'))
                assert bodies[index].decode().strip() in driver.find_element(By.TAG_NAME, 'body').text
                collection = next(c for c in bench.workspace.source_collections(matter.matter_id) if c.name == title)
                library = bench.source_library(matter, collection_id=collection.collection_id)
                assert library.total == 1
                documents.append(bench.source_store(matter).get(library.items[0].document_id))
                collections.append(collection)
            assert len({d.document_id for d in documents}) == 3
            assert len({d.version_id for d in documents}) == 3
            assert {d.relative_path for d in documents} == {'Records/report.txt'}
            checked('Three real folder confirmations retain separate same-path occurrences for equal and differing bytes')
            go(prefix + '/setup?view=list')
            assert len(rows()) == 3
            assert all(c.name in driver.find_element(By.CSS_SELECTOR, '.source-library-table').text for c in collections)
            driver.save_screenshot(str(output / 'synthetic-separate-productions.png'))
            for collection, body in zip(collections, bodies):
                go(prefix + '/setup?' + urlencode({'view': 'list', 'collection': collection.collection_id}))
                assert len(rows()) == 1
                click('.source-open-action')
                assert body.decode().strip() in driver.find_element(By.TAG_NAME, 'body').text
            checked('Each collection contains its own exact source and each receipt opens the matching received version')

            if args.verify_byte_matches:
                go(prefix + '/setup?' + urlencode({'view': 'list', 'collection': collections[0].collection_id}))
                assert len(rows()) == 1
                click('[data-source-byte-matches]')
                assert len(rows()) == 2  # Deliberate comparison crosses collections.
                assert collections[2].name not in driver.find_element(By.CSS_SELECTOR, '.source-library-table').text
                assert all(d.digest not in driver.page_source for d in documents)
                comparison_url = driver.current_url
                old = driver.find_element(By.TAG_NAME, 'html')
                refresh_page(driver, wait)
                wait.until(lambda _: detached(old))
                ready()
                assert len(rows()) == 2
                Select(driver.find_element(By.NAME, 'matching_only')).select_by_value('true')
                click('.source-library-filters button[type=submit]')
                assert len(rows()) == 2 and 'same_content=' in driver.current_url
                click('[data-source-folder-nav] li a')
                assert len(rows()) == 2 and 'same_content=' in driver.current_url
                for width in (1440, 430):
                    driver.set_window_size(width, 1000)
                    if width < 901:
                        wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-rail-toggle]').get_attribute('aria-expanded') == 'false')
                        wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                    assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth + 2')
                    assert 'identical bytes' in driver.find_element(By.CSS_SELECTOR, '[data-source-byte-comparison]').text
                    driver.execute_script('arguments[0].scrollIntoView({block:"start",behavior:"instant"});', driver.find_element(By.CSS_SELECTOR, '[data-source-byte-comparison]'))
                    driver.save_screenshot(str(output / f'synthetic-byte-comparison-{width}.png'))
                    driver.execute_script('arguments[0].scrollIntoView({block:"start",behavior:"instant"});', driver.find_element(By.CSS_SELECTOR, '.source-library-table'))
                    assert all('files with identical bytes' in row.text for row in rows())
                    driver.save_screenshot(str(output / f'synthetic-byte-match-rows-{width}.png'))
                driver.set_window_size(1440, 1000)
                click('[data-source-select-all]', False)
                Select(driver.find_element(By.CSS_SELECTOR, '[data-source-bulk-action]')).select_by_value('create_set')
                driver.find_element(By.CSS_SELECTOR, '[data-bulk-set-name]').send_keys('Generated identical byte review')
                click('[data-source-bulk-submit]')
                byte_group = next(g for g in bench.workspace.source_sets(matter.matter_id) if g.name == 'Generated identical byte review')
                assert byte_group.source_count == 2
                assert len(rows()) == 2 and 'same_content=' in driver.current_url
                go(prefix + '/setup?' + urlencode({'view': 'list', 'source_set': byte_group.source_set_id}))
                assert len(rows()) == 2
                click('.source-open-action')
                assert bodies[0].decode().strip() in driver.find_element(By.TAG_NAME, 'body').text
                checked('Identical-byte comparison crosses collections and survives reload, filters, folder navigation, source grouping and exact source opening at desktop and narrow widths')

            go(prefix + '/setup?' + urlencode({'view': 'list', 'collection': collections[0].collection_id}))
            click('[data-source-select-all]', False)
            Select(driver.find_element(By.CSS_SELECTOR, '[data-source-bulk-action]')).select_by_value('create_set')
            driver.find_element(By.CSS_SELECTOR, '[data-bulk-set-name]').send_keys('Generated first production review')
            click('[data-source-bulk-submit]')
            group = next(g for g in bench.workspace.source_sets(matter.matter_id) if g.name == 'Generated first production review')
            query = '?' + urlencode({'mode': 'search', 'q': 'copper journal', 'source_set': group.source_set_id})
            go(prefix + query)
            click('.open-support-link')
            assert 'the first vehicle arrived at noon' in driver.find_element(By.ID, 'support-pane').text
            click('.support-save-button')
            assert len(bench.workspace.all_notebook_items(matter.matter_id, ACTOR)) == 1
            checked('A source set selects one production occurrence and normal search saves its exact support to case notes')

            go(prefix + '/setup?' + urlencode({'view': 'list', 'collection': collections[1].collection_id}))
            assert len(rows()) == 1
            click('.source-table-row .remove-action')
            assert documents[0].document_id in bench.source_store(matter).documents
            assert documents[1].document_id not in bench.source_store(matter).documents
            assert documents[2].document_id in bench.source_store(matter).documents
            assert bench.workspace.source_set(matter.matter_id, group.source_set_id).source_count == 1
            go(prefix + query)
            click('.open-support-link')
            assert 'the first vehicle arrived at noon' in driver.find_element(By.ID, 'support-pane').text
            assert len(bench.workspace.all_notebook_items(matter.matter_id, ACTOR)) == 1
            checked('Removing one equal-content occurrence preserves the other source, its review group and saved support')
            if args.verify_byte_matches:
                removed_token = bench.source_store(matter).action_token(documents[1])
                go(prefix + '/setup?' + urlencode({'view': 'list', 'same_content': removed_token}))
                assert len(rows()) == 0 and 'comparison is unavailable' in driver.find_element(By.TAG_NAME, 'body').text
                click('[data-source-byte-comparison] a')
                assert len(rows()) == 2 and not driver.find_elements(By.CSS_SELECTOR, '[data-source-byte-matches]')
                checked('Removing a comparison reference gives an empty recoverable view and removes obsolete copy counts')


            other = 'generated-foreign-owner'
            bench.workspace.upsert_principal('test', other, 'Generated foreign owner', other, preferred_principal_id=other)
            foreign = bench.create_matter('Generated foreign occurrence canary', '', other)
            foreign_store = bench.source_store(foreign)
            import io
            hidden, _ = foreign_store.store_stream('report.txt', 'text/plain', io.BytesIO(b'Generated foreign source canary.\n'))
            go(f'/matters/{foreign.slug}/sources/{foreign_store.action_token(hidden)}')
            assert 'Generated foreign source canary.' not in driver.find_element(By.TAG_NAME, 'body').text
            assert len(bench.source_store(matter).documents) == 2
            checked('An equal-basename foreign-matter source stays inaccessible')
            if args.verify_byte_matches:
                go(prefix + '/setup?' + urlencode({'view': 'list', 'same_content': foreign_store.action_token(hidden)}))
                assert len(rows()) == 0 and 'comparison is unavailable' in driver.find_element(By.TAG_NAME, 'body').text
                assert 'Generated foreign source canary.' not in driver.page_source
                checked('A foreign-matter comparison token cannot disclose sources or broaden the list')


            go(prefix + '/settings')
            before_bundle = set(downloads.iterdir())
            click(f'a[href="{prefix}/export"]', False)
            bundle = wait.until(lambda _: completed_zip(downloads, before_bundle))
            with zipfile.ZipFile(bundle) as archive:
                selected = [json.loads(archive.read(name)) for name in archive.namelist()
                            if name.startswith('intake/') and name.endswith('.json')]
                assert {r['collection_name'] for r in selected} == {c.name for c in collections}
                assert all(r['counts']['received'] == 1 for r in selected)
                removed = next(r for r in selected if r['collection_name'] == collections[1].name)
                assert removed['counts']['unavailable'] == 1
                assert 'the first vehicle arrived at noon' in archive.read('notebook/matter-notebook.md').decode()
                assert not any(name.startswith('originals/') for name in archive.namelist())
                assert all(archive.read(name) not in bodies for name in archive.namelist() if not name.endswith('/'))
            exit_flow = BrowserCaseExit(driver, base, app, matter, downloads, preview_subject="taylor-morgan")
            exit_flow.prepare()
            exit_flow.confirm_name(matter.display_name)
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted')
            assert all(path.read_bytes() == body for path, body in zip(originals, bodies))
            checked('Final bundle retains all three production receipts and exact notes; owner closure preserves every external original')
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
