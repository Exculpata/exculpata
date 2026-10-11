#!/usr/bin/env python3
"""Synthetic browser acceptance for explicit all-text review and coverage."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import io
import json
import runpy
from pathlib import Path
import socket
import sys
import tempfile
import threading
import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from case_intelligence.generation import VerifiedReviewDecision, GenerationRejected
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.workbench import create_workbench_app

class SyntheticGenerator:
    available = True
    def generate(self, **kwargs):
        raise AssertionError('Synthetic browser review uses the explicit deterministic classifier.')


def measure_ledger_contrast(driver, output: Path) -> dict:
    """Measure the populated ledger, compositing actual ancestor backgrounds."""
    contrast = runpy.run_path(str(ROOT / 'scripts/browser-accept-dusk.py'))['CONTRAST']
    original_theme = driver.execute_script('return document.documentElement.dataset.theme')
    report = {'synthetic_only': True, 'passed': False, 'minimum_text_ratio': 4.5, 'themes': {}}
    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
        {'width': 1440, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
    try:
        for theme in ('dark', 'light', 'dusk'):
            driver.execute_script("document.documentElement.dataset.theme=arguments[0];document.querySelectorAll('[data-theme-picker]').forEach(select=>select.value=arguments[0])", theme)
            cells = driver.find_elements(By.CSS_SELECTOR, '.text-review-ledger th, .text-review-ledger td, .text-review-ledger td a')
            assert len(driver.find_elements(By.CSS_SELECTOR, '.text-review-ledger tbody tr')) == 15
            measurements = []
            for cell in cells:
                pair = driver.execute_script(contrast, cell, None, 'color', False)
                measurements.append({'element': cell.tag_name, 'text': cell.text[:160], **pair})
            report['themes'][theme] = {'minimum_ratio': min(item['ratio'] for item in measurements), 'cells': measurements}
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", cells[0])
            driver.save_screenshot(str(output / f'text-ledger-{theme}.png'))
        report['passed'] = all(theme['minimum_ratio'] >= report['minimum_text_ratio'] for theme in report['themes'].values())
        (output / 'text-ledger-contrast.json').write_text(json.dumps(report, indent=2) + '\n')
        return report
    finally:
        driver.execute_script("document.documentElement.dataset.theme=arguments[0];document.querySelectorAll('[data-theme-picker]').forEach(select=>select.value=arguments[0])", original_theme)
        driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})


def main():
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ledger-contrast-only', action='store_true', help='Stop after measuring the populated synthetic text ledger in both themes.')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='recordbench-full-text-browser-') as temp:
        root = Path(temp).resolve()
        downloads = root / 'downloads'; downloads.mkdir()
        app = create_workbench_app(root / 'runtime', generator=SyntheticGenerator(), auth_mode='test')
        listener = socket.socket(); listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True); thread.start()
        options = Options(); options.binary_location = str(args.chrome_binary)
        for flag in ('--headless=new', '--disable-dev-shm-usage', '--window-size=1440,1000'):
            options.add_argument(flag)
        options.add_experimental_option('prefs', {'download.default_directory': str(downloads), 'download.prompt_for_download': False})
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 30); wait.until(lambda _: server.started)
            driver.get(base + '/')
            bench = app.state.workbench
            actor = 'development-taylor-morgan'
            matter = bench.create_matter('Synthetic all-text review', 'Browser acceptance', actor)
            store = bench.source_store(matter)
            document, _ = store.store_stream('synthetic-ledger.txt', 'text/plain', io.BytesIO(b'Synthetic source for browser acceptance.'))
            document.units = []
            for number in range(1, 16):
                text = 'The amber bicycle arrived at noon.' if number == 15 else 'Unreadable classification example.' if number == 4 else f'Synthetic routine entry {number}.'
                document.units.append(asdict(PilotUnit(number, text, excerpt_digest=hashlib.sha256(text.encode()).hexdigest())))
            store._write_units(document); bench._sync_source_catalog(matter, [document])
            criterion, _ = bench.workspace.create_review_criterion(matter.matter_id, actor,
                title='Find the bicycle', instructions='Include references to the amber bicycle.')
            def classify(**kwargs):
                text = kwargs['evidence'][0].excerpt
                if text.startswith('Unreadable'):
                    raise GenerationRejected('Synthetic failed classification')
                included = 'amber bicycle' in text
                return VerifiedReviewDecision('include' if included else 'not_identified', text if included else 'No bicycle mentioned.', ('S1',) if included else (), True, 1)
            bench.generator.classify_source = classify
            driver.get(base + f'/matters/{matter.slug}/full-review?criterion={criterion.criterion_id}')
            assert 'Check selected passages' in driver.page_source
            button = driver.find_element(By.CSS_SELECTOR, 'button[value="full_text"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", button); button.click()
            wait.until(lambda d: 'run=' in d.current_url)
            run = bench.workspace.review_runs(matter.matter_id, actor)[0]
            wait.until(lambda _: bench.workspace.review_run(matter.matter_id, actor, run.run_id).state == 'succeeded')
            driver.get(base + f'/matters/{matter.slug}/full-review?criterion={criterion.criterion_id}&run={run.run_id}&source={document.document_id}')
            assert 'Coverage gaps remain' in driver.find_element(By.ID, 'review-coverage').text
            assert '1 unit with failed ranges' in driver.find_element(By.ID, 'review-coverage').text
            for width in (1440, 390):
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                if width < 901:
                    wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                assert driver.execute_script("return document.querySelector('#review-controls').getBoundingClientRect().top < document.querySelector('#decision-ledger').getBoundingClientRect().top")
                assert driver.execute_script("return document.querySelector('#review-coverage').getBoundingClientRect().top < document.querySelector('#decision-ledger').getBoundingClientRect().top")
                driver.execute_script("document.querySelector('#review-controls').scrollIntoView({block:'start',behavior:'instant'})")
                driver.save_screenshot(str(args.output / f'full-review-controls-{width}.png'))
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            support = driver.find_element(By.CSS_SELECTOR, '#decision-inspector .decision-citations p')
            assert support.text == 'The amber bicycle arrived at noon.'
            retained = bench.workspace.review_decision(matter.matter_id, actor, run.run_id, document.document_id)
            assert 'excerpt' not in retained.citations[0]
            for width, label in ((1440, 'desktop'), (390, 'mobile')):
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                wait.until(lambda d: d.execute_script('return document.documentElement.scrollWidth <= window.innerWidth'))
                if width < 901:
                    wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", support)
                driver.save_screenshot(str(args.output / f'full-text-inspector-{label}.png'))
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            driver.get(base + f'/matters/{matter.slug}/full-review/{run.run_id}/text')
            assert '14</strong> units fully processed' in driver.page_source and '1</strong> unit with failed ranges' in driver.page_source
            assert 'The amber bicycle arrived at noon.' in driver.page_source
            collapse = driver.find_elements(By.CSS_SELECTOR, '[data-assistant-collapse]')
            if collapse:
                collapse[0].click()
            ledger_contrast = measure_ledger_contrast(driver, args.output)
            assert ledger_contrast['passed'], {theme: result['minimum_ratio'] for theme, result in ledger_contrast['themes'].items()}
            if args.ledger_contrast_only:
                print(json.dumps({'passed': True, 'synthetic_only': True, 'checks': ['Populated full-text ledger text meets 4.5:1 in Light and Dusk']}))
                return
            for width, label in ((1440, 'desktop'), (390, 'mobile')):
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                if width < 901:
                    wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                wait.until(lambda d: d.execute_script('return document.documentElement.scrollWidth <= window.innerWidth'))
                driver.save_screenshot(str(args.output / f'full-text-{label}.png'))
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            link = driver.find_element(By.CSS_SELECTOR, 'a[href$="format=json"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", link); link.click()
            wait.until(lambda _: bool(list(downloads.glob('*.json'))))
            records = json.loads(next(downloads.glob('*.json')).read_text())['records']
            assert len([row for row in records if row['record_type'] == 'range']) == 15
            driver.get(base + f'/matters/{matter.slug}/full-review?criterion={criterion.criterion_id}&run={run.run_id}')
            copy_form = driver.find_element(By.CSS_SELECTOR, f'form[action$="/{run.run_id}/report"]')
            details = copy_form.find_element(By.XPATH, 'ancestor::details')
            summary = details.find_element(By.TAG_NAME, 'summary')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", summary)
            summary.click()
            copy_form.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
            wait.until(lambda d: '/reports?report=' in d.current_url)
            page = driver.find_element(By.CSS_SELECTOR, '.report-reading-page')
            assert 'not the complete range ledger' in page.text
            assert 'selected passages. This is not an all-page read' not in page.text
            source_link = driver.find_element(By.CSS_SELECTOR, '.report-reading-sources a')
            assert 'synthetic-ledger.txt' in source_link.text
            report_url = driver.current_url
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", source_link)
            source_link.click()
            wait.until(lambda d: 'The amber bicycle arrived at noon.' in d.find_element(By.TAG_NAME, 'body').text)
            driver.get(report_url)
            wait.until(lambda d: bool(d.find_elements(By.CSS_SELECTOR, '.report-reading-page')))
            for width, label in ((1440, 'desktop'), (390, 'mobile')):
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                if width < 901:
                    wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                wait.until(lambda d: d.execute_script('return document.documentElement.scrollWidth <= window.innerWidth'))
                driver.execute_script('window.scrollTo(0,0)')
                driver.save_screenshot(str(args.output / f'full-text-report-{label}.png'))
            driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            markdown = driver.find_element(By.CSS_SELECTOR, 'a[href$="format=markdown"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", markdown)
            markdown.click()
            wait.until(lambda _: bool(list(downloads.glob('*.md'))))
            report_text = next(downloads.glob('*.md')).read_text()
            assert 'The amber bicycle arrived at noon.' in report_text and 'not the complete range ledger' in report_text
            bench.full_review.close()
            # Stop the worker so cancellation/resume state is deterministic.
            saved = bench.workspace.review_run(matter.matter_id, actor, run.run_id)
            queued = bench.workspace.queue_review_run(matter.matter_id, actor, saved.criterion_version_id, run_kind='full', review_mode='full_text')
            driver.get(base + f'/matters/{matter.slug}/full-review/{queued.run_id}/text')
            cancel = driver.find_element(By.CSS_SELECTOR, f'form[action$="/{queued.run_id}/cancel"] button')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", cancel)
            cancel.click()
            wait.until(lambda _: bench.workspace.review_run(matter.matter_id, actor, queued.run_id).state == 'cancelled')
            driver.get(base + f'/matters/{matter.slug}/full-review/{queued.run_id}/text')
            resume = driver.find_element(By.CSS_SELECTOR, f'form[action$="/{queued.run_id}/retry"] button')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", resume)
            driver.save_screenshot(str(args.output / 'full-review-resume.png'))
            resume.click()
            wait.until(lambda _: bench.workspace.review_run(matter.matter_id, actor, queued.run_id).state == 'queued')
            receipt = {'provenance': 'synthetic', 'checks': ['launch and coverage precede decision lists at desktop and mobile widths', 'text ledger safe cancellation and resume preserve the same run', 'explicit full-text launch', 'populated text-ledger text meets 4.5:1 in Light and Dusk', 'decision inspector shows exact support without persisting excerpts', 'late fifteenth-unit finding', 'separate failed-unit coverage', 'desktop and mobile without page overflow', 'complete JSON download', 'saved full-text run copied to readable Report', 'bounded full-text scope and source citation retained', 'Report desktop and mobile without overflow', 'Report Markdown download']}
            (args.output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps(receipt))
        finally:
            if driver: driver.quit()
            server.should_exit = True; thread.join(timeout=10); listener.close()

if __name__ == '__main__':
    main()
