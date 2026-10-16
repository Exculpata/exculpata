#!/usr/bin/env python3
"""Exercise production summary source links, exports and responsive layout."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import tempfile
import threading

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from demo import seed_demo


@contextmanager
def serve(app):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        WebDriverWait(server, 20).until(lambda current: current.started)
        yield f'http://127.0.0.1:{listener.getsockname()[1]}'
    finally:
        server.should_exit = True
        thread.join(15)
        listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and any(args.output.iterdir())):
        raise ValueError('Use a new or empty output directory.')
    args.output.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.binary_location = str(args.chrome_binary)
    for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server'):
        options.add_argument(flag)
    report = dict(synthetic_only=True, passed=False, checks=[])
    driver = None
    try:
        with tempfile.TemporaryDirectory(prefix='production-summary-browser-') as temporary:
            with seed_demo(Path(temporary) / 'demo') as demo, serve(demo.app) as base:
                driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
                driver.set_page_load_timeout(30)
                wait = WebDriverWait(driver, 30)
                driver.get(base)
                for cookie in demo.client.cookies.jar:
                    driver.add_cookie(dict(name=cookie.name, value=cookie.value, path='/'))
                for width in (1440, 390):
                    driver.set_window_size(width, 1000)
                    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', dict(width=width, height=1000, deviceScaleFactor=1, mobile=width < 720))
                    assert driver.execute_script('return window.innerWidth') == width
                    driver.get(base + demo.home_path)
                    button = driver.find_element(By.XPATH, '//button[normalize-space()="Summarize production"]')
                    driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", button)
                    button.click()
                    wait.until(lambda current: '/production-summary' in current.current_url)
                    def completed(current):
                        status = current.find_element(By.CSS_SELECTOR, '[data-summary-job]').get_attribute('data-summary-job')
                        if status == 'failed':
                            raise AssertionError('Synthetic summary job failed')
                        if status != 'completed':
                            current.refresh()
                        return status == 'completed'
                    wait.until(completed)
                    assert '40 documents' in driver.find_element(By.TAG_NAME, 'main').text
                    assert '11 documents' in driver.find_element(By.TAG_NAME, 'main').text
                    assert '40-damaged-transfer-note.txt' in driver.find_element(By.TAG_NAME, 'main').text
                    assert driver.find_elements(By.LINK_TEXT, 'Download Markdown')
                    assert driver.find_elements(By.LINK_TEXT, 'Download JSON')
                    assert driver.execute_script('return document.documentElement.scrollWidth <= window.innerWidth')
                    assert driver.execute_script("return getComputedStyle(document.querySelector('main.production-summary')).display") == 'block'
                    cards = driver.find_elements(By.CSS_SELECTOR, '.production-summary-grid > section')
                    assert cards[0].find_element(By.TAG_NAME, 'h2').text == 'Gaps'
                    if width == 390:
                        assert len({round(card.rect['x']) for card in cards}) == 1
                    else:
                        assert cards[1].rect['y'] == cards[2].rect['y']
                        assert cards[1].rect['x'] < cards[2].rect['x']
                    detail = driver.find_element(By.CSS_SELECTOR, '#summary-sources details')
                    assert not detail.get_attribute('open')
                    toggle = detail.find_element(By.TAG_NAME, 'summary')
                    driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", toggle)
                    toggle.click()
                    assert detail.get_attribute('open')
                    driver.get(base + f'/matters/{demo.matter.slug}/production-summary')
                    driver.save_screenshot(str(args.output / f'production-summary-{width}.png'))
                    report['checks'].append(f'Home action, counts, gaps, collapsible source list and layout at {width}px')
                    href = driver.find_element(By.CSS_SELECTOR, '.summary-card a[href*="support="]').get_attribute('href')
                    driver.get(href)
                    assert driver.find_element(By.ID, 'support-pane')
                    report['checks'].append(f'Original passage opens at {width}px')
                driver.get(base + demo.receipt_url)
                receipt_button = driver.find_element(By.XPATH, '//button[normalize-space()="Summarize production"]')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", receipt_button)
                receipt_button.click()
                wait.until(lambda current: 'scope=intake-' in current.current_url)
                report['checks'].append('Receipt action retains selected receipt scope')
                for format in ('markdown', 'json'):
                    response = demo.client.get(f'/matters/{demo.matter.slug}/production-summary/export?format={format}')
                    assert response.status_code == 200 and 'attachment;' in response.headers['content-disposition']
                report['checks'].append('Markdown and JSON downloads return source-bearing attachments')
                report['passed'] = True
    except Exception:
        if driver:
            driver.save_screenshot(str(args.output / 'failure.png'))
        raise
    finally:
        if driver:
            driver.quit()
        (args.output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
