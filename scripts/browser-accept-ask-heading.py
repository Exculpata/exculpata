#!/usr/bin/env python3
"""Measure Ask and Review readability with the offline Harbor Street demo."""
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
from selenium.webdriver.support.ui import WebDriverWait

from demo import require_response, seed_demo

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
                    from browser_appearance import APPEARANCES, select_appearance
                    for mode, accent in (*APPEARANCES, ('light', 'cyan')):
                        appearance = f'{mode}-{accent}'
                        print(f'Measuring {width}px {appearance} heading', flush=True)
                        select_appearance(driver, wait, mode, accent)
                        driver.execute_script('window.scrollTo(0, 0)')
                        heading = one('.matter-heading h1')
                        assert heading.is_displayed()
                        assert driver.execute_script('''const r=arguments[0].getBoundingClientRect();
                            return innerWidth === arguments[1] && r.left >= 0 && r.right <= innerWidth
                                && r.top >= 0 && r.bottom <= innerHeight
                                && document.documentElement.scrollWidth <= innerWidth;''', heading, width)
                        measured = driver.execute_script(CONTRAST, heading, None, 'color', False)
                        report['measurements'].append({'width': width, 'theme': appearance, **measured})
                        if mode == 'light':
                            # Light headings use the shared readable text token, including after round trip.
                            assert measured['fg'] == [11, 13, 16, 1], measured
                        driver.save_screenshot(str(output / f'ask-heading-{appearance}-{width}.png'))
                        report['checks'].append(f'{width}px {appearance}: heading visible without overflow')
                    navigate(one(f'.matter-section-tabs a[href="{prefix}/exact-search"]'))
                    assert urlsplit(driver.current_url).path == prefix + '/exact-search'
                    current_section('Search')
                    navigate(one(f'.matter-section-tabs a[href="{prefix}"]'))
                    current_section('Ask')
                    report['checks'].append(f'{width}px: Ask → Search → Ask navigation')
                # Reproduce the remaining Review/selected-conversation contrast gaps
                # at the reported viewport, using saved empty conversations (no inference).
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                    'width': 1920, 'height': 1080, 'deviceScaleFactor': 1, 'mobile': False})
                conversations = []
                for title in ('Harbor Street timeline', 'Harbor Street records'):
                    response = require_response(demo.client.post(prefix + '/conversations',
                        headers={'Accept': 'application/json'}), 201).json()
                    conversations.append(response['conversation_id'])
                    require_response(demo.client.post(
                        prefix + '/conversations/' + response['conversation_id'] + '/rename',
                        data={'title': title}), 303)
                light_before = None
                for mode, accent in (('light', 'cyan'), *APPEARANCES, ('light', 'cyan')):
                    appearance = f'{mode}-{accent}'
                    select_appearance(driver, wait, mode, accent)
                    driver.get(base + prefix + '/setup')
                    assert one('.setup-heading h1').text == 'Review'
                    assert one('.source-intake-heading h2').text == 'Add sources'
                    review = one('.setup-heading h1')
                    assert review.is_displayed()
                    measured = driver.execute_script(CONTRAST, review, None, 'color', False)
                    report['measurements'].append({
                        'width': 1920, 'theme': appearance, 'target': 'review-heading', **measured})
                    driver.save_screenshot(str(output / f'ask-heading-review-{appearance}-1920.png'))
                    report['checks'].append(f'1920px {appearance}: Review / Add sources heading visible')

                    driver.get(base + prefix + '?conversation=' + conversations[0])
                    selected = one('.conversation-history-item.selected')
                    assert selected.get_attribute('aria-current') == 'true'
                    assert selected.find_element(By.CSS_SELECTOR, 'span').text == 'Harbor Street timeline'
                    colors = {'ask-heading': driver.execute_script(
                        CONTRAST, one('.matter-heading h1'), None, 'color', False)}
                    report['measurements'].append({'width': 1920, 'theme': appearance,
                        'target': 'ask-heading', **colors['ask-heading']})
                    driver.execute_script(
                        'arguments[0].scrollIntoView({block:"center",behavior:"instant"})', selected)
                    for target, selector in (
                        ('selected-title', '.conversation-history-item.selected > span'),
                        ('selected-metadata', '.conversation-history-item.selected small'),
                        ('unselected-title', '.conversation-history-item:not(.selected) > span'),
                        ('unselected-metadata', '.conversation-history-item:not(.selected) small'),
                    ):
                        element = one(selector)
                        assert element.is_displayed()
                        assert driver.execute_script('''const r=arguments[0].getBoundingClientRect();
                            return innerWidth === 1920 && innerHeight === 1080 && r.left >= 0
                                && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight
                                && document.documentElement.scrollWidth <= innerWidth;''', element)
                        measured = driver.execute_script(CONTRAST, element, None, 'color', False)
                        colors[target] = measured
                        report['measurements'].append({
                            'width': 1920, 'theme': appearance, 'target': target, **measured})
                    if mode == 'light':
                        # Light selected cards use raised surfaces and the shared text tokens.
                        assert colors['selected-title']['fg'] == [11, 13, 16, 1]
                        assert colors['selected-metadata']['fg'] == [90, 96, 105, 1]
                        assert colors['selected-title']['bg'] == [255, 255, 255, 1]
                        assert colors['selected-metadata']['bg'] == [255, 255, 255, 1]
                        if light_before is not None:
                            assert colors == light_before
                        light_before = colors
                    driver.save_screenshot(str(output / f'ask-heading-conversation-{appearance}-1920.png'))
                    report['checks'].append(f'1920px {appearance}: selected and unselected conversation labels visible')
                report['checks'].append('1920px: Light selected-card token colors preserved through six-palette round trip')
                # Capture every state even when the original dark contrast fails.
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
