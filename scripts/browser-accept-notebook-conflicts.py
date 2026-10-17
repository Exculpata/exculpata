#!/usr/bin/env python3
"""Synthetic two-reviewer note conflicts, source support, export, and close in Chrome."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

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
from selenium.webdriver.support.ui import Select, WebDriverWait

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
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-note-conflicts-browser-") as temporary:
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
            owner, admin = browser(OWNER), browser(MEMBER)
            wait = WebDriverWait(owner, 20)
            wait.until(lambda _: server.started)
            def go(driver, path):
                navigate_to(driver, base + path, diagnostics=args.output)
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
            fill(owner, '#matter-name', 'Generated shared note review')
            submit(owner, '.matter-form button[type=submit]')
            slug = urlparse(owner.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            owner.find_element(By.CSS_SELECTOR, '[data-assistant-collapse]').click()
            collection_name = owner.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic notebook-conflicts collection")
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

            go(admin, '/matters/new')
            member_id = bench.workspace.connection.execute(
                'SELECT principal_id FROM workbench_principal WHERE provider_subject=?', (MEMBER,)
            ).fetchone()[0]
            bench.workspace.add_member(matter.matter_id, member_id, owner_id)
            note = bench.workspace.all_notebook_items(matter.matter_id, owner_id)[0]
            note_path = prefix + f'/notebook?edit={note.item_id}'
            editor = '.notebook-edit-panel '
            go(owner, note_path)
            go(admin, note_path)
            fill(owner, editor + 'input[name=title]', 'Owner saved title')
            fill(owner, editor + 'textarea[name=body]', 'Owner saved details.')
            submit(owner, editor + 'button[type=submit]')
            fill(admin, editor + 'input[name=title]', 'Member proposed title')
            fill(admin, editor + 'textarea[name=body]', 'Member unsaved details.')
            fill(admin, editor + 'input[name=date_label]', 'Date remains uncertain')
            Select(admin.find_element(By.CSS_SELECTOR, editor + 'select[name=item_type]')).select_by_value('event')
            Select(admin.find_element(By.CSS_SELECTOR, editor + 'select[name=status]')).select_by_value('disputed')
            click(admin, editor + 'input[name=pinned]')
            submit(admin, editor + 'button[type=submit]')
            assert 'changed since the page was opened' in body(admin)
            assert 'Owner saved details.' in body(admin)
            assert admin.find_element(By.CSS_SELECTOR, 'textarea[name=body]').get_attribute('value') == 'Member unsaved details.'
            assert admin.find_element(By.CSS_SELECTOR, 'input[name=date_label]').get_attribute('value') == 'Date remains uncertain'
            assert admin.find_element(By.CSS_SELECTOR, 'input[name=pinned]').is_selected()
            assert Select(admin.find_element(By.CSS_SELECTOR, 'select[name=item_type]')).first_selected_option.get_attribute('value') == 'event'
            admin.save_screenshot(str(args.output / 'note-conflict-desktop.png'))
            click(admin, '[data-assistant-collapse]')
            admin.set_window_size(390, 844)
            WebDriverWait(admin, 5).until(lambda d: d.execute_script("return document.getElementById('matter-rail').getBoundingClientRect().right <= 1"))
            assert admin.execute_script('return document.documentElement.scrollWidth <= innerWidth')
            admin.execute_script("arguments[0].scrollIntoView({block: 'start', behavior: 'instant'})", admin.find_element(By.ID, 'unsaved-note-heading'))
            admin.save_screenshot(str(args.output / 'note-conflict-mobile.png'))
            checks.append('Two team members see shared notes; stale edits retain all proposed fields with the saved version')

            go(owner, note_path)
            fill(owner, editor + 'textarea[name=body]', 'Owner follow-up details.')
            submit(owner, editor + 'button[type=submit]')
            submit(admin, '.notebook-item-form button[type=submit]')
            assert 'Owner follow-up details.' in body(admin) and 'changed since the page was opened' in body(admin)
            fill(admin, 'textarea[name=body]', 'Combined reviewed note: owner follow-up and member details.')
            Select(admin.find_element(By.CSS_SELECTOR, 'select[name=status]')).select_by_value('needs_review')
            submit(admin, '.notebook-item-form button[type=submit]')
            admin.set_window_size(1440, 1000)
            go(owner, prefix + '/notebook')
            assert 'Combined reviewed note' in body(owner)
            assert 'Updated by Member' in body(owner)
            go(admin, support)
            assert 'the blue vehicle arrived at noon' in admin.find_element(By.ID, 'support-pane').text
            checks.append('A second intervening edit conflicts again; reviewed combined edit saves with attribution and original source support')

            go(owner, prefix + '/notebook')
            go(admin, note_path)
            fill(admin, editor + 'textarea[name=body]', 'Combined reviewed note with a later addition.')
            submit(admin, editor + 'button[type=submit]')
            submit(owner, f'#{note.item_id} .confirm-action')
            assert 'changed since the page was opened' in body(owner)
            current = bench.workspace.notebook_item(matter.matter_id, owner_id, note.item_id)
            assert current.body == 'Combined reviewed note with a later addition.' and current.status == 'needs_review'
            go(owner, prefix + '/notebook')
            submit(owner, f'#{note.item_id} .confirm-action')
            reviewed = bench.workspace.notebook_item(matter.matter_id, owner_id, note.item_id)
            assert reviewed.body == current.body and reviewed.status == 'confirmed'
            checks.append('Stale Confirm refuses unseen changed text; refreshed confirmation changes status without rewriting the note')

            go(admin, prefix + '/notebook')
            go(owner, note_path)
            fill(owner, editor + 'textarea[name=body]', 'Combined reviewed note for export.')
            submit(owner, editor + 'button[type=submit]')
            def delete(driver, identifier):
                button = click(driver, f'#{identifier} .danger-link')
                WebDriverWait(driver, 10).until(EC.alert_is_present()).accept()
                WebDriverWait(driver, 20).until(EC.staleness_of(button))
            delete(admin, note.item_id)
            assert 'changed since the page was opened' in body(admin)
            assert bench.workspace.notebook_item(matter.matter_id, owner_id, note.item_id).body == 'Combined reviewed note for export.'
            checks.append('Stale Delete keeps newer teammate work and requires review of the current note')

            go(owner, prefix + '/notebook')
            fill(owner, '.notebook-tools input[name=title]', 'Generated removable note')
            fill(owner, '.notebook-tools textarea[name=body]', 'Temporary synthetic note.')
            submit(owner, '.notebook-tools .notebook-item-form button[type=submit]')
            removable = next(item for item in bench.workspace.all_notebook_items(matter.matter_id, owner_id) if item.title == 'Generated removable note')
            go(admin, prefix + f'/notebook?edit={removable.item_id}')
            fill(admin, editor + 'textarea[name=body]', 'Unsaved text retained after teammate deletion.')
            delete(owner, removable.item_id)
            submit(admin, editor + 'button[type=submit]')
            assert 'was deleted' in body(admin)
            assert admin.find_element(By.CSS_SELECTOR, 'textarea[name=body]').get_attribute('value') == 'Unsaved text retained after teammate deletion.'
            assert not admin.find_elements(By.CSS_SELECTOR, '.notebook-item-form button[type=submit]')
            assert len(bench.workspace.all_notebook_items(matter.matter_id, owner_id)) == 1
            checks.append('Deleted-note recovery retains unsaved text without recreating the note or source links')

            go(owner, prefix + '/settings')
            before_bundle = set(downloads.iterdir())
            click(owner, f'a[href="{prefix}/export"]')
            bundle = wait.until(lambda _: completed_zip(downloads, before_bundle))
            with zipfile.ZipFile(bundle) as archive:
                texts = [archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith(('.md', '.json'))]
                assert any('Combined reviewed note for export.' in text for text in texts)
                assert any('the blue vehicle arrived at noon' in text for text in texts)
                assert not any(name.endswith('generated-source.txt') for name in archive.namelist())
            exit_flow = BrowserCaseExit(owner, base, app, matter, downloads)
            exit_flow.prepare()
            exit_flow.confirm_name(matter.display_name)
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted')
            assert original.read_bytes() == original_bytes
            checks.append('Export contains the reviewed note and exact source support; normal closure preserves external original')
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
