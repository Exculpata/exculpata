#!/usr/bin/env python3
"""Measure the real Ask heading with the offline, synthetic Harbor Street demo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import socket
import tempfile
import threading
from urllib.parse import urlsplit

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from demo import seed_demo

ROOT = Path(__file__).resolve().parents[1]
# Reuse the existing ancestor-background/opacity-aware contrast measurement.
CONTRAST = runpy.run_path(str(ROOT / 'scripts/browser-accept-dusk.py'))['CONTRAST']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_only': True, 'passed': False, 'checks': [], 'measurements': []}
    with tempfile.TemporaryDirectory(prefix='recordbench-ask-heading-') as temporary:
        print('Preparing isolated synthetic Harbor Street demo', flush=True)
        with seed_demo(Path(temporary)) as demo, socket.socket() as listener:
            print('Synthetic intake and indexing finished; starting Chrome', flush=True)
            listener.bind(('127.0.0.1', 0))
            base = f'http://127.0.0.1:{listener.getsockname()[1]}'
            prefix = f'/matters/{demo.matter.slug}'
            server = uvicorn.Server(uvicorn.Config(demo.app, lifespan='off',
                log_level='warning', access_log=False, proxy_headers=False))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
            thread.start()
            options = Options()
            options.binary_location = str(args.chrome_binary)
            for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
                         '--disable-gpu', '--no-proxy-server', '--window-size=1440,1000'):
                options.add_argument(flag)
            driver = None
            try:
                driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
                driver.set_page_load_timeout(30)
                driver.set_script_timeout(30)
                report['browser_version'] = driver.capabilities['browserVersion']
                wait = WebDriverWait(driver, 30)
                wait.until(lambda _: server.started)

                def one(selector):
                    return driver.find_element(By.CSS_SELECTOR, selector)

                def navigate(element):
                    old = one('html')
                    if not element.is_displayed():
                        one('.matter-section-disclosure > summary').click()
                    driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"})', element)
                    element.send_keys(Keys.ENTER)
                    wait.until(EC.staleness_of(old))
                    wait.until(lambda _: driver.execute_script('return document.readyState') == 'complete')

                def theme(value):
                    if one('html').get_attribute('data-theme') == value:
                        return
                    picker = one('#appearance-theme')
                    if not picker.is_displayed():
                        one('.account-menu > summary').click()
                        picker = one('#account-appearance-theme')
                    old = one('html')
                    Select(picker).select_by_value(value)
                    wait.until(EC.staleness_of(old))
                    wait.until(lambda _: one('html').get_attribute('data-theme') == value
                        and driver.execute_script('return document.readyState') == 'complete')

                def current_section(label):
                    selected = one('.matter-section-tabs [aria-current="page"] > span')
                    assert selected.get_attribute('textContent') == label

                driver.get(base + demo.home_path)
                navigate(one('button[name="identity_subject"][value="taylor-morgan"]'))
                wait.until(lambda _: urlsplit(driver.current_url).path == demo.home_path)
                for width, height in ((1440, 1000), (390, 844)):
                    # CDP sets an exact CSS viewport, including below desktop window minimums.
                    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                        'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False})
                    driver.get(base + demo.home_path)
                    navigate(one(f'.matter-section-tabs a[href="{prefix}"]'))
                    current_section('Ask')
                    assert one('.matter-heading h1').text == 'Harbor Street'
                    report['checks'].append(f'{width}px: Home → Ask navigation')
                    for appearance in ('light', 'dark', 'dusk', 'light'):
                        print(f'Measuring {width}px {appearance} heading', flush=True)
                        theme(appearance)
                        driver.execute_script('window.scrollTo(0, 0)')
                        heading = one('.matter-heading h1')
                        assert heading.is_displayed()
                        assert driver.execute_script('''const r=arguments[0].getBoundingClientRect();
                            return innerWidth === arguments[1] && r.left >= 0 && r.right <= innerWidth
                                && r.top >= 0 && r.bottom <= innerHeight
                                && document.documentElement.scrollWidth <= innerWidth;''', heading, width)
                        measured = driver.execute_script(CONTRAST, heading, None, 'color', False)
                        report['measurements'].append({'width': width, 'theme': appearance, **measured})
                        if appearance == 'light':
                            # Preserve the pre-fix Light foreground exactly, including after round trip.
                            assert measured['fg'] == [10, 22, 48, 1], measured
                        driver.save_screenshot(str(output / f'ask-heading-{appearance}-{width}.png'))
                        report['checks'].append(f'{width}px {appearance}: heading visible without overflow')
                    navigate(one(f'.matter-section-tabs a[href="{prefix}/exact-search"]'))
                    assert urlsplit(driver.current_url).path == prefix + '/exact-search'
                    current_section('Search')
                    navigate(one(f'.matter-section-tabs a[href="{prefix}"]'))
                    current_section('Ask')
                    report['checks'].append(f'{width}px: Ask → Search → Ask navigation')
                # Capture both widths/themes even when the original dark contrast fails.
                for measured in report['measurements']:
                    assert measured['ratio'] >= 4.5, measured
                report['checks'].append('All measured heading colors meet 4.5:1 against the painted background')
                report['passed'] = True
            finally:
                try:
                    if driver:
                        try:
                            if not report['passed']:
                                driver.save_screenshot(str(output / 'failure.png'))
                        except WebDriverException:
                            report['diagnostic_screenshot_unavailable'] = True
                        finally:
                            driver.quit()
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    (output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
