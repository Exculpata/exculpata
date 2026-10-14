#!/usr/bin/env python3
"""Verify native Chrome zoom and scaled workflows with disposable synthetic data.

ChromeDriver's prefs are confined to its temporary profile:
https://developer.chrome.com/docs/chromedriver/capabilities
Chromium's ChromeZoomLevelPrefs maps the default partition to key "x" and
passes its default_zoom_level preference directly to HostZoomMap:
https://chromium.googlesource.com/chromium/src/+/lkgr/chrome/browser/ui/zoom/chrome_zoom_level_prefs.cc
Zoom level is log(factor) / log(1.2). Every run proves the preference took effect
using the unchanged outer window, inverse layout viewport and scaled DPR.
No viewport, deviceScaleFactor, pinch/page scale or CSS zoom override is used.
"""
from __future__ import annotations

import argparse
import io
import json
import math
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
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.managed_storage import StoragePolicy  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402
from synthetic_browser_environment import isolate_environment  # noqa: E402

ACTOR = 'development-taylor-morgan'
SOURCE_NAME = 'Synthetic source — North Annex receiving notes with a long recognizable filename.txt'
SOURCE_TEXT = 'The blue crate arrived at the North Annex at 09:15.'
ZOOMS = (1, 1.25, 1.5, 2, 4)


def replaced(element):
    """Wait condition: the element's document was replaced.

    Chrome can report a handle from a replaced document as a generic inspector
    error rather than a stale reference; any other driver failure is real.
    """
    def condition(driver):
        try:
            return EC.staleness_of(element)(driver)
        except WebDriverException as exc:
            if 'Node with given id does not belong to the document' not in (exc.msg or ''):
                raise
            return True
    return condition


def main():
    isolate_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and
            (not args.output.is_dir() or any(args.output.iterdir()))):
        raise ValueError('Choose a fresh or empty output directory; existing evidence was preserved.')
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'synthetic_only': True, 'checks': [], 'measurements': {},
        'method': 'Native Chrome default zoom in a new temporary profile per factor; fixed 1280x1200 outer window; no emulation or CSS zoom.',
        'unrun_checks': ['OS display scaling', 'Safari/WebKit native zoom', 'physical mobile', 'screen reader']}
    driver = None
    with tempfile.TemporaryDirectory(prefix='recordbench-zoom-') as temporary:
        app = create_workbench_app(Path(temporary).resolve() / 'runtime', auth_mode='test',
            generator=UnavailableGenerator(), learned_retrieval=False, background_ingestion=False,
            storage_policy=StoragePolicy(reserve_bytes=0))
        bench = app.state.workbench
        matter = bench.create_matter('Synthetic scaled workspace with a long case title', 'Generated zoom acceptance', ACTOR)
        bench.source_store(matter).store_stream(SOURCE_NAME, 'text/plain', io.BytesIO(SOURCE_TEXT.encode()))
        for number in range(20):
            bench.workspace.create_notebook_item(matter.matter_id, ACTOR,
                item_type='note', status='needs_review', title=f'Synthetic observation {number:02}',
                body='The invented reviewer is checking the blue crate against its original source. ' * 12)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        prefix = f'/matters/{matter.slug}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            baseline = None
            for factor in ZOOMS:
                percent = str(round(factor * 100))
                options = Options()
                options.binary_location = str(args.chrome_binary)
                # Match the existing disposable journeys on hosted Linux runners.
                if sys.platform == "linux":
                    options.add_argument("--no-sandbox")
                for flag in ('--headless=new', '--disable-dev-shm-usage', '--no-proxy-server', '--window-size=1280,1200'):
                    options.add_argument(flag)
                options.add_experimental_option('prefs', {
                    'partition.default_zoom_level': {'x': math.log(factor) / math.log(1.2)}})
                driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
                wait = WebDriverWait(driver, 20)
                wait.until(lambda _: server.started)
                driver.get(base + prefix + '/notebook')
                js = driver.execute_script
                find = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)
                metrics = js('''return {inner_width:innerWidth,inner_height:innerHeight,
                    outer_width:outerWidth,outer_height:outerHeight,dpr:devicePixelRatio,
                    visual_scale:visualViewport.scale,root_css_zoom:getComputedStyle(document.documentElement).zoom,
                    body_css_zoom:getComputedStyle(document.body).zoom};''')
                report['measurements'][percent] = metrics
                if baseline is None:
                    baseline = metrics
                assert metrics['outer_width'] == baseline['outer_width'], metrics
                assert metrics['outer_height'] == baseline['outer_height'], metrics
                assert abs(metrics['inner_width'] - baseline['inner_width'] / factor) <= 1, metrics
                assert abs(metrics['inner_height'] - baseline['inner_height'] / factor) <= 1, metrics
                assert abs(metrics['dpr'] / baseline['dpr'] - factor) <= .02, metrics
                assert metrics['visual_scale'] == 1, metrics
                assert metrics['root_css_zoom'] == metrics['body_css_zoom'] == '1', metrics
                if factor == 1:
                    driver.quit()
                    driver = None
                    continue
                report['checks'].append(f'{percent}% native zoom verified by unchanged outer window, inverse layout viewport, DPR and unscaled visual viewport')

                def no_overflow():
                    assert js('return document.documentElement.scrollWidth <= innerWidth + 1'), (percent, 'Page scrolls sideways')

                def reachable(element):
                    js('arguments[0].scrollIntoView({block:"center",inline:"nearest",behavior:"instant"})', element)
                    wait.until(lambda _: js('''const e=arguments[0],r=e.getBoundingClientRect();
                        return r.width>0 && r.left>=-1 && r.right<=innerWidth+1 && r.top>=0 && r.bottom<=innerHeight+1
                        && e.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2));''', element))

                no_overflow()
                suggestion = find('.suggestion-tool button')
                reachable(suggestion)
                assert suggestion.is_enabled()
                driver.save_screenshot(str(args.output / f'zoom-{percent}-notes.png'))
                suggestion.click()
                wait.until(replaced(suggestion))
                wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, '.notebook-item.status-suggested .notebook-provenance a'))

                def open_support():
                    # Locate, reveal and click with fresh handles; if the page is
                    # replaced meanwhile, the wait simply tries again.
                    try:
                        candidate = next(item for item in driver.find_elements(By.CSS_SELECTOR, '.notebook-item.status-suggested')
                            if 'North Annex' in item.text)
                        support = candidate.find_element(By.CSS_SELECTOR, '.notebook-provenance a')
                        reachable(support)
                        support.click()
                        return True
                    except TimeoutException:
                        raise
                    except (StopIteration, WebDriverException):
                        return False

                wait.until(lambda _: open_support())
                # The link may load a new page: read the pane in one script call so
                # no element handle from the previous document is ever reused.
                def support_shown():
                    try:
                        return js("""const pane = document.querySelector('.support-pane');
                            return !!pane && pane.getClientRects().length > 0 && pane.textContent.includes('North Annex');""")
                    except TimeoutException:
                        raise
                    except WebDriverException:
                        return False

                wait.until(lambda _: support_shown())
                report['checks'].append(f'{percent}% zoom: Find review suggestions remains reachable, submits, and opens the Suggested place original support')

                driver.get(base + prefix + '/setup?view=list')
                no_overflow()
                # Six destinations appear as direct links when they fit, otherwise
                # in the section navigator. Both must be keyboard reachable.
                compact = driver.find_elements(By.CSS_SELECTOR, '.matter-section-disclosure')
                if compact:
                    disclosure = compact[0]
                    summary = disclosure.find_element(By.CSS_SELECTOR, 'summary')
                    assert 'Review' in summary.text
                    reachable(summary)
                    summary.send_keys(Keys.ENTER)
                    links = disclosure.find_elements(By.CSS_SELECTOR, 'a')
                else:
                    links = find('main .matter-section-tabs').find_elements(By.CSS_SELECTOR, ':scope > a')
                    js('arguments[0].focus()', links[0])
                    links = links[1:]
                    assert driver.switch_to.active_element.text.strip() == 'Home'
                assert len(links) == (6 if compact else 5)
                for link in links:
                    driver.switch_to.active_element.send_keys(Keys.TAB)
                    assert driver.switch_to.active_element == link
                    reachable(link)
                if compact:
                    reachable(summary)
                    summary.click()
                row = find('.source-table-row')
                name = row.find_element(By.CSS_SELECTOR, '.source-name-cell strong')
                assert name.text == SOURCE_NAME
                width = js('return arguments[0].getBoundingClientRect().width', name)
                assert width >= 140, (percent, width)
                assert js('''const r=document.createRange();r.selectNodeContents(arguments[0]);
                    const b=arguments[1].getBoundingClientRect();return [...r.getClientRects()].every(
                    a=>a.left>=b.left-1&&a.right<=b.right+1);''', name, row)
                read = row.find_element(By.CSS_SELECTOR, '.source-open-action')
                reachable(read)
                no_overflow()
                driver.save_screenshot(str(args.output / f'zoom-{percent}-sources.png'))
                read.click()
                wait.until(lambda _: SOURCE_TEXT in find('main').text)
                report['checks'].append(f'{percent}% zoom: matter navigation exposes all six keyboard destinations; full source identity and Read document remain usable')
                driver.quit()
                driver = None
            assert report['measurements']['400']['inner_width'] == 320, report['measurements']['400']
            report.update(passed=True, browser='Chrome for Testing',
                commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip())
        except Exception as error:
            report['error'] = {'type': type(error).__name__, 'message': str(error)[:2000]}
            if driver is not None:
                try:
                    driver.save_screenshot(str(args.output / 'failure.png'))
                except Exception as capture_error:
                    report['screenshot_error'] = type(capture_error).__name__
            raise
        finally:
            (args.output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(10)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
