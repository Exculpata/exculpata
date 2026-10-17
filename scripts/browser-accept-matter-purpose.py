#!/usr/bin/env python3
"""Synthetic optional orientation: native keyboard edits, conflict and reload."""
from browser_navigation import navigate_to, refresh_page

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading

import uvicorn
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from synthetic_browser_environment import isolate_environment
from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app

ACTOR = 'development-taylor-morgan'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Choose a new or empty output directory.')
    args.output.mkdir(parents=True, exist_ok=True)
    isolate_environment()
    report = dict(synthetic_only=True, passed=False, checks=[])
    driver = None
    with tempfile.TemporaryDirectory(prefix='exculpata-purpose-') as temporary:
        runtime = Path(temporary).resolve() / 'runtime'
        app = create_workbench_app(runtime, auth_mode='test', generator=UnavailableGenerator(),
                                   background_ingestion=False, learned_retrieval=False)
        bench = app.state.workbench
        matter = bench.create_matter('Synthetic matter orientation', '', ACTOR)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        for flag in ('--headless=new', '--no-sandbox', '--no-proxy-server', '--window-size=1440,1100'):
            options.add_argument(flag)
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            wait = WebDriverWait(driver, 30, ignored_exceptions=(StaleElementReferenceException,))
            driver.set_script_timeout(30)
            wait.until(lambda _: server.started)
            home = base + f'/matters/{matter.slug}/home'
            def find(selector):
                return driver.find_element(By.CSS_SELECTOR, selector)
            def focus(element):
                for _ in range(100):
                    if driver.switch_to.active_element == element:
                        assert element.is_displayed() and element.is_enabled()
                        assert driver.execute_script('return arguments[0].matches(":focus-visible")', element)
                        return
                    ActionChains(driver).send_keys(Keys.TAB).perform()
                raise AssertionError('Control is not reachable with native Tab')
            def open_editor():
                element = find('#matter-purpose summary')
                focus(element)
                element.send_keys(Keys.ENTER)
                wait.until(lambda _: find('#matter-objective').is_displayed())
            def submit():
                button = find('.matter-purpose-form button')
                focus(button)
                button.send_keys(Keys.ENTER)
                wait.until(lambda _: '/home' in driver.current_url and not driver.find_elements(By.CSS_SELECTOR, '#matter-purpose details[open]'))
            navigate_to(driver, home, diagnostics=args.output)
            assert 'Add an objective.' in find('#matter-purpose').text
            driver.save_screenshot(str(args.output / 'purpose-empty-1440.png'))
            print('Empty Home rendered', flush=True)
            report['checks'].append('Old/empty matter invites optional objective and background')
            open_editor()
            objective = find('#matter-objective')
            focus(objective)
            objective.send_keys('Understand the synthetic arrival timeline.')
            ActionChains(driver).send_keys(Keys.TAB).perform()
            assert driver.switch_to.active_element == find('#matter-background')
            find('#matter-background').send_keys('Synthetic reviewer background.\nKeep the competing accounts visible.')
            submit()
            assert bench.workspace.matter_purpose(matter.matter_id, ACTOR)['revision'] == 1
            report['checks'].append('Native keyboard edits both optional fields and saves one revision')
            refresh_page(driver, wait)
            assert 'Understand the synthetic arrival timeline.' in find('#matter-purpose').text
            for width in (1440, 390, 320):
                driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                    dict(width=width, height=1100, deviceScaleFactor=1, mobile=False))
                assert driver.execute_script('return innerWidth') == width
                open_editor()
                focus(find('#matter-objective'))
                assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
                driver.save_screenshot(str(args.output / f'purpose-edit-{width}.png'))
                navigate_to(driver, home, diagnostics=args.output)
            report['checks'].append('Saved values survive reload; desktop and 390/320 pixel editors have no overflow')
            open_editor()
            find('#matter-objective').clear()
            find('#matter-objective').send_keys('Unsaved synthetic draft')
            bench.workspace.update_matter_purpose(matter.matter_id, ACTOR, expected_revision=1,
                objective='Synthetic competing tab edit', background='', request_id='synthetic-browser-concurrent')
            button = find('.matter-purpose-form button')
            focus(button)
            button.send_keys(Keys.ENTER)
            wait.until(EC.staleness_of(button))
            wait.until(lambda _: 'Review your unsaved draft' in find('main').text)
            assert find('#matter-objective').get_attribute('value') == 'Unsaved synthetic draft'
            assert 'Synthetic competing tab edit' in find('main').text
            driver.save_screenshot(str(args.output / 'purpose-conflict-320.png'))
            report['checks'].append('Stale save shows current values and retains unsaved draft without overwrite')
            navigate_to(driver, home, diagnostics=args.output)
            open_editor()
            find('#matter-objective').clear()
            find('#matter-background').clear()
            submit()
            saved = bench.workspace.matter_purpose(matter.matter_id, ACTOR)
            assert saved['objective'] == saved['background'] == '' and saved['revision'] == 3
            report['checks'].append('Reviewer clears both fields through the panel')
            assert not bench.workspace.answer_jobs(matter.matter_id, ACTOR)
            assert not bench.workspace.research_jobs(matter.matter_id, ACTOR)
            report['checks'].append('Orientation edits create no answer or research jobs')
            report['passed'] = True
        except BaseException:
            if driver is not None:
                driver.save_screenshot(str(args.output / 'failure.png'))
            raise
        finally:
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive()
            listener.close()
            (args.output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
