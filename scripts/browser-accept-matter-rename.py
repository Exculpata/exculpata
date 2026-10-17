#!/usr/bin/env python3
"""Synthetic two-session rename, source support, export, and close in Chrome."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import json
import socket
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from browser_case_exit import BrowserCaseExit, completed_zip  # noqa: E402
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.identity import KerberosSettings, KERBEROS_SECRET_HEADER, KERBEROS_USER_HEADER  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402

OWNER = 'owner@EXAMPLE.TEST'
ADMIN = 'admin@EXAMPLE.TEST'
MEMBER = 'member@EXAMPLE.TEST'
SECRET = 'synthetic_browser_proxy_secret_' + '0' * 40


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-rename-browser-") as temporary:
        root = Path(temporary).resolve()
        downloads = root / 'downloads'
        downloads.mkdir()
        original = root / 'generated-source.txt'
        original.write_bytes(b'Synthetic source: the blue vehicle arrived at noon.\n')
        original_bytes = original.read_bytes()
        app = create_workbench_app(
            root / 'runtime', generator=UnavailableGenerator(), auth_mode='kerberos',
            secure_cookie=True, background_ingestion=True,
            kerberos_settings=KerberosSettings(
                realm='EXAMPLE.TEST', proxy_secret=SECRET,
                allowed_groups=frozenset({'reviewers'}),
                administrator_groups=frozenset({'administrators'}),
            ),
            kerberos_profile_resolver=lambda identity: (identity.split('@')[0].title(), identity.lower()),
            kerberos_group_resolver=lambda identity: {'administrators'} if identity == ADMIN else {'reviewers'},
        )
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False, ws='none'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        browsers = []
        try:
            def browser(identity):
                options = Options()
                options.binary_location = str(args.chrome_binary)
                for flag in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,1000'):
                    options.add_argument(flag)
                options.add_experimental_option('prefs', {'download.default_directory': str(downloads)})
                driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
                browsers.append(driver)
                driver.execute_cdp_cmd('Network.enable', {})
                driver.execute_cdp_cmd('Network.setExtraHTTPHeaders', {'headers': {
                    KERBEROS_USER_HEADER: identity, KERBEROS_SECRET_HEADER: SECRET,
                }})
                return driver
            owner, admin = browser(OWNER), browser(ADMIN)
            wait = WebDriverWait(owner, 20)
            wait.until(lambda _: server.started)
            def go(driver, path):
                driver.get(base + path)
            def fill(driver, selector, value):
                field = driver.find_element(By.CSS_SELECTOR, selector)
                field.clear()
                field.send_keys(value)
            def click(driver, selector):
                button = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'instant'})", button)
                button.click()
                return button
            def submit(driver, selector):
                button = click(driver, selector)
                WebDriverWait(driver, 20).until(EC.staleness_of(button))
            def body(driver):
                return driver.find_element(By.TAG_NAME, 'body').text

            go(owner, '/matters/new')
            fill(owner, '#matter-name', 'Generated original')
            submit(owner, '.matter-form button[type=submit]')
            slug = urlparse(owner.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            owner.find_element(By.CSS_SELECTOR, '[data-assistant-collapse]').click()
            collection_name = owner.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic matter-rename collection")
            owner.find_element(By.ID, 'source-files').send_keys(str(original))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '[data-upload-preflight-confirm]').is_enabled())
            click(owner, '[data-upload-preflight-confirm]')
            bench = app.state.workbench
            owner_id = bench.workspace.connection.execute(
                'SELECT principal_id FROM workbench_principal WHERE provider_subject=?', (OWNER,)
            ).fetchone()[0]
            matter = bench.matter(slug, owner_id)
            document = wait.until(lambda _: next((d for d in bench.source_store(matter).documents.values() if d.state == 'ready'), None))
            token = bench._support_token(bench._candidate(matter, document, document.parsed_units()[0], 1))
            support = prefix + f'?support={token}#support-pane'
            go(owner, support)
            assert 'the blue vehicle arrived at noon' in owner.find_element(By.ID, 'support-pane').text
            submit(owner, '.support-save-button')
            checks.append('Browser upload, exact source passage, and saved case note')

            go(owner, prefix + '/settings')
            go(admin, prefix + '/settings')
            fill(owner, '#matter-name', 'Owner revised name')
            submit(owner, '#rename-matter-form button')
            assert 'Matter renamed.' in body(owner)
            fill(admin, '#matter-name', 'Administrator revised name')
            submit(admin, '#rename-matter-form button')
            assert 'changed while you were editing' in body(admin)
            assert admin.find_element(By.ID, 'matter-name').get_attribute('value') == 'Administrator revised name'
            assert admin.find_element(By.TAG_NAME, 'h1').text == 'Owner revised name'
            admin.save_screenshot(str(args.output / 'conflict-desktop.png'))
            submit(admin, '#rename-matter-form button')
            assert 'Matter renamed.' in body(admin)
            checks.append('Separate owner/admin sessions preserve stale proposed name and explicitly resubmit')
            go(owner, prefix + '/home')
            assert 'Administrator revised name' in body(owner)
            go(owner, support)
            assert 'the blue vehicle arrived at noon' in owner.find_element(By.ID, 'support-pane').text
            go(owner, prefix + '/notebook')
            assert 'the blue vehicle arrived at noon' in body(owner)
            assert bench.matter(slug, owner_id).matter_id == matter.matter_id
            assert original.read_bytes() == original_bytes
            checks.append('Team refresh, saved note, unchanged source token/version and external original')

            member = browser(MEMBER)
            go(member, '/matters/new')
            member_id = bench.workspace.connection.execute(
                'SELECT principal_id FROM workbench_principal WHERE provider_subject=?', (MEMBER,)
            ).fetchone()[0]
            bench.workspace.add_member(matter.matter_id, member_id, owner_id)
            go(member, prefix + '/settings')
            assert not member.find_elements(By.ID, 'rename-matter-form')
            assert 'Administrator revised name' in body(member)
            owner.set_window_size(390, 844)
            go(owner, prefix + '/settings')
            assert owner.execute_script('return document.documentElement.scrollWidth <= innerWidth')
            assert owner.find_element(By.ID, 'matter-name').is_displayed()
            owner.save_screenshot(str(args.output / 'settings-mobile.png'))
            checks.append('Ordinary member reads updated name; owner form fits mobile viewport')

            owner.set_window_size(1440, 1000)
            go(owner, prefix + '/settings')
            before_bundle = set(downloads.iterdir())
            click(owner, f'a[href="{prefix}/export"]')
            bundle = wait.until(lambda _: completed_zip(downloads, before_bundle))
            with zipfile.ZipFile(bundle) as archive:
                texts = [archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith(('.md', '.json'))]
                assert any('Administrator revised name' in text for text in texts)
                assert any('the blue vehicle arrived at noon' in text for text in texts)
                assert not any(name.endswith('generated-source.txt') for name in archive.namelist())
            exit_flow = BrowserCaseExit(owner, base, app, matter, downloads)
            exit_flow.prepare()
            exit_flow.confirm_name('Generated original')
            assert 'exactly as shown' in body(owner)
            assert bench.workspace.matter_lifecycle(matter.matter_id).state == 'closing'
            exit_flow.fresh_confirmation()
            exit_flow.confirm_name('Administrator revised name')
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted')
            assert original.read_bytes() == original_bytes
            checks.append('Downloaded renamed bundle with citations, old-name closure rejection, current-name deletion')
            (args.output / 'result.json').write_text(json.dumps({'passed': len(checks), 'checks': checks}, indent=2) + '\n')
            print(json.dumps({'passed': len(checks), 'checks': checks}))
        except Exception:
            for index, driver in enumerate(browsers):
                driver.save_screenshot(str(args.output / f'failure-{index}.png'))
            raise
        finally:
            for driver in browsers:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError('Synthetic server did not stop')


if __name__ == '__main__':
    main()
