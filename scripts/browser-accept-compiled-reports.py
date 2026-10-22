#!/usr/bin/env python3
"""Walk all automatic report types using synthetic saved work in real Chrome."""

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
import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from case_intelligence.workbench import create_workbench_app
from tests.test_report_compilation import SourceEchoClient
from tests.test_report_review_basis import ACTOR, saved_research


def main():
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-compiled-report-") as temporary:
        root = Path(temporary).resolve()
        downloads = root / 'downloads'; downloads.mkdir()
        app = create_workbench_app(root / 'runtime', generator=SourceEchoClient(), auth_mode='test')
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
            wait = WebDriverWait(driver, 25); wait.until(lambda _: server.started)
            navigate_to(driver, base + '/', diagnostics=args.output)
            bench = app.state.workbench; bench.research.close()
            matter = bench.workspace.create_matter('The bicycle delivery', 'Synthetic report acceptance', ACTOR)
            research, document = saved_research(bench, matter)
            bench.workspace.create_notebook_item(matter.matter_id, ACTOR, item_type='event', status='disputed',
                title='Arrival needs confirmation', body='The arrival time needs comparison with the missing gate log.')
            def click(element):
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", element)
                element.click()
            def screenshot(name, width):
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                if width < 901:
                    wait.until(lambda d: d.execute_script("return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                assert driver.execute_script('return document.documentElement.scrollWidth <= window.innerWidth'), name
                driver.execute_script('window.scrollTo(0,0)')
                driver.save_screenshot(str(args.output / (name + '.png')))
                driver.execute_cdp_cmd('Emulation.clearDeviceMetricsOverride', {})
            for kind in ('timeline', 'entities', 'topic'):
                navigate_to(driver, base + f'/matters/{matter.slug}/reports/new', diagnostics=args.output)
                wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, f'input[name="kind"][value="{kind}"]'))).click()
                if kind == 'topic':
                    driver.find_element(By.CSS_SELECTOR, 'textarea[name="topic"]').send_keys('The bicycle arrival')
                if kind == 'timeline':
                    screenshot('report-composer-desktop', 1440)
                    screenshot('report-composer-mobile', 390)
                click(driver.find_element(By.CSS_SELECTOR, '.report-compose-submit button'))
                wait.until(lambda d: '/reports?report=' in d.current_url)
                wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.report-reading-page')))
                assert driver.find_elements(By.CSS_SELECTOR, '.report-reading-sources a')
                assert not driver.find_elements(By.CSS_SELECTOR, '.report-section-card textarea[name="body"]')
                screenshot('compiled-' + kind + '-desktop', 1440)
                screenshot('compiled-' + kind + '-mobile', 390)
            click(driver.find_element(By.LINK_TEXT, 'Edit this draft'))
            body = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.report-section-card textarea[name="body"]')))
            marker = 'Reviewer will request the missing gate log.'
            body.send_keys('\n' + marker)
            form = body.find_element(By.XPATH, 'ancestor::form')
            click(form.find_element(By.CSS_SELECTOR, 'button[type="submit"]'))
            wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.report-reading-page')))
            assert marker in driver.find_element(By.CSS_SELECTOR, '.report-reading-page').text
            click(driver.find_element(By.CSS_SELECTOR, 'a[href$="format=markdown"]'))
            wait.until(lambda _: bool(list(downloads.glob('*.md'))))
            assert marker in next(downloads.glob('*.md')).read_text()
            click(driver.find_element(By.CSS_SELECTOR, 'a[href$="format=docx"]'))
            wait.until(lambda _: bool(list(downloads.glob('*.docx'))))
            # Deleted results stay reachable through the Reports list and can be
            # explicitly recompiled without restoring the deleted document.
            current_report = bench.workspace.reports(matter.matter_id, ACTOR)[0]
            completed = next(job for job in bench.report_compilation.jobs.list(matter.matter_id, ACTOR)
                             if job.report_id == current_report.report_id)
            bench.workspace.delete_report(matter.matter_id, current_report.report_id, ACTOR,
                                          expected_updated_at=current_report.updated_at)
            navigate_to(driver, base + f'/matters/{matter.slug}/reports', diagnostics=args.output)
            recovery = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR,
                f'.report-job-list a[href$="job={completed.job_id}"]')))
            assert 'This draft was deleted' in recovery.text
            click(recovery)
            click(wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'form[action$="/retry"] button'))))
            wait.until(lambda d: '/reports?report=' in d.current_url)
            assert current_report.report_id not in driver.current_url
            # Hold the worker stopped so the queued cancellation state is
            # deterministic; this uses only this script's synthetic runtime.
            bench.report_compilation.coordinator.close()
            navigate_to(driver, base + f'/matters/{matter.slug}/reports/new', diagnostics=args.output)
            click(wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '.report-compose-submit button'))))
            click(wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'form[action$="/cancel"] button'))))
            wait.until(lambda d: 'Compilation cancelled.' in d.find_element(By.TAG_NAME, 'body').text)
            assert 'Cancelling compilation.' not in driver.find_element(By.TAG_NAME, 'body').text
            screenshot('report-cancelled-mobile', 390)
            receipt = {'provenance': 'synthetic source-echo model; workflow validation, not model quality evaluation', 'checks': [
                'all three report types compile from saved work', 'durable background completion opens readable document',
                'source links retained', 'human edit saved', 'deleted draft discoverable and explicitly retried', 'queued cancellation reports terminal state', 'Word and Markdown downloads', 'desktop and 390px mobile without horizontal overflow']}
            (args.output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps(receipt))
        except Exception:
            if driver:
                driver.save_screenshot(str(args.output / 'failure.png'))
                print(driver.current_url)
                print(driver.find_element(By.TAG_NAME, 'body').text[-3500:])
                print(driver.get_log('browser'))
            raise
        finally:
            if driver: driver.quit()
            server.should_exit = True; thread.join(timeout=10); listener.close()

if __name__ == '__main__': main()
