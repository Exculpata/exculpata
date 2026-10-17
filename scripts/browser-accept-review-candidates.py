#!/usr/bin/env python3
"""Synthetic desktop/mobile candidate, cited-source and review-state journey."""
import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading

import httpx
import uvicorn
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app


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
    with tempfile.TemporaryDirectory(prefix='exculpata-review-candidates-') as temporary:
        app = create_workbench_app(Path(temporary).resolve()/'runtime', generator=UnavailableGenerator(), auth_mode='test')
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False, ws='none'))
        thread = threading.Thread(target=server.run, kwargs={'sockets':[listener]}, daemon=True)
        thread.start()
        driver = None
        try:
            WebDriverWait(None, 30).until(lambda _: server.started)
            with httpx.Client(base_url=base, timeout=30) as client:
                response = client.post('/matters', data=dict(name='Synthetic candidate review', descriptor='Generated statements only'))
                assert response.status_code == 303
                slug = response.headers['location'].split('/')[2]
                text = 'The witness said no deal was offered during the interview.'
                response = client.post(f'/matters/{slug}/uploads', follow_redirects=True, files=[('files',
                    ('Synthetic-review.txt', text.encode(), 'text/plain'))])
                assert response.status_code == 200
            options = Options()
            options.binary_location = str(args.chrome_binary)
            for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1000'):
                options.add_argument(flag)
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            wait = WebDriverWait(driver, 30, ignored_exceptions=(StaleElementReferenceException,))
            path = f'/matters/{slug}/review-candidates'
            for width in (1440, 390):
                driver.set_window_size(width, 1100)
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", dict(width=width, height=1100, deviceScaleFactor=1, mobile=width <= 480))
                driver.get(base+path)
                wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '.review-candidate-card'))
                notice = driver.find_element(By.CSS_SELECTOR, '.review-candidate-notice')
                assert notice.is_displayed() and 'does not decide' in notice.text
                assert notice.rect['y'] < driver.find_element(By.CSS_SELECTOR, '.review-category-chips').rect['y']
                chips = driver.find_element(By.CSS_SELECTOR, '.review-category-chips')
                assert driver.execute_script('return arguments[0].scrollWidth > arguments[0].clientWidth', chips)
                assert all(e.rect['width'] <= width for e in driver.find_elements(By.CSS_SELECTOR, '.review-candidate-card'))
                assert all(e.rect['height'] >= 44 for e in driver.find_elements(By.CSS_SELECTOR, '.review-candidate-actions button'))
                assert driver.execute_script('return document.documentElement.scrollWidth <= window.innerWidth')
                marks = driver.find_elements(By.CSS_SELECTOR, '.review-candidate-passage mark')
                assert len(marks) == 1 and all(m.text for m in marks)
                driver.save_screenshot(str(args.output/f'candidates-{width}.png'))
                checks.append(f'{width}px: visible notice, isolated chip scrolling, contained highlights, tap targets and no horizontal overflow')
            link = driver.find_element(By.CSS_SELECTOR, '.open-support-link')
            driver.execute_script('arguments[0].scrollIntoView({behavior: "instant", block: "center"})', link)
            link.click()
            wait.until(lambda d: 'support=' in d.current_url)
            wait.until(lambda d: 'no deal was offered' in d.find_element(By.ID, 'support-pane').text)
            checks.append('Cited-context link opens the original passage')
            driver.get(base+path)
            button = driver.find_element(By.CSS_SELECTOR, 'button[value="reviewed"]')
            driver.execute_script('arguments[0].scrollIntoView({behavior: "instant", block: "center"})', button)
            button.click()
            wait.until(lambda d: 'Decision by' in d.find_element(By.TAG_NAME, 'body').text)
            driver.refresh()
            assert 'Reviewed' in driver.find_element(By.CSS_SELECTOR, '.review-candidate-card').text
            button = driver.find_element(By.CSS_SELECTOR, 'button[value="dismiss"]')
            driver.execute_script('arguments[0].scrollIntoView({behavior: "instant", block: "center"})', button)
            button.click()
            wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, '.review-candidate-card'))
            Select(driver.find_element(By.NAME, 'status')).select_by_value('dismissed')
            driver.find_element(By.CSS_SELECTOR, '.review-candidate-filters button').click()
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, 'button[value="reopen"]'))
            button = driver.find_element(By.CSS_SELECTOR, 'button[value="reopen"]')
            driver.execute_script('arguments[0].scrollIntoView({behavior: "instant", block: "center"})', button)
            button.click()
            wait.until(lambda d: 'Open' in d.find_element(By.CSS_SELECTOR, '.review-candidate-card').text)
            checks.append('Reviewed survives reload; dismiss hides result; status filter and reopen work')
            with httpx.Client(base_url=base, timeout=30) as client:
                for kind in ('json', 'csv', 'markdown'):
                    response = client.get(path+'/export', params={'format':kind})
                    assert response.status_code == 200 and 'Synthetic-review.txt' in response.text.replace('\\', '')
            checks.append('JSON, CSV and Markdown exports retain the cited passage')
        finally:
            if driver:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=30)
            listener.close()
        (args.output/'checks.json').write_text(json.dumps(dict(checks=checks), indent=2))
        print(json.dumps(dict(checks=checks), indent=2))


if __name__ == '__main__':
    main()
