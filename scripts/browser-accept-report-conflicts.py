#!/usr/bin/env python3
"""Synthetic two-reviewer Report conflicts, source support, export, and close in Chrome."""

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
from selenium.common.exceptions import WebDriverException
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
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-report-conflicts-browser-") as temporary:
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
            def detached(element):
                def check(driver):
                    try:
                        return EC.staleness_of(element)(driver)
                    except WebDriverException as exc:
                        if 'Node with given id does not belong to the document' not in exc.msg:
                            raise
                        return True
                return check
            def enter_editor(driver):
                links = driver.find_elements(By.CSS_SELECTOR, ".report-reading-page a[href*='edit=true']")
                if links:
                    links[0].click()
                    WebDriverWait(driver, 20).until(detached(links[0]))
            def go(driver, path):
                navigate_to(driver, base + path, diagnostics=args.output)
                enter_editor(driver)
            def fill(driver, selector, value):
                field = driver.find_element(By.CSS_SELECTOR, selector)
                field.clear()
                field.send_keys(value)
            def click(driver, selector):
                if selector != '[data-assistant-collapse]' and 'assistant-collapsed' not in driver.find_element(By.TAG_NAME, 'body').get_attribute('class').split():
                    toggles = driver.find_elements(By.CSS_SELECTOR, '[data-assistant-collapse]')
                    if toggles and toggles[0].is_displayed():
                        toggles[0].click()
                button = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'instant'})", button)
                button.click()
                return button
            def submit(driver, selector):
                button = click(driver, selector)
                WebDriverWait(driver, 20).until(detached(button))
                enter_editor(driver)
            def body(driver):
                return driver.find_element(By.TAG_NAME, 'body').text

            go(owner, '/matters/new')
            fill(owner, '#matter-name', 'Generated shared Report review')
            submit(owner, '.matter-form button[type=submit]')
            slug = urlparse(owner.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            owner.find_element(By.CSS_SELECTOR, '[data-assistant-collapse]').click()
            collection_name = owner.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic report-conflicts collection")
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
            go(owner, prefix + '/reports')
            click(owner, '.report-create-card summary')
            fill(owner, '.report-create-card input[name=title]', 'Generated shared Report')
            fill(owner, '.report-create-card textarea[name=purpose]', 'Original purpose')
            submit(owner, '.report-create-card button[type=submit]')
            report = bench.workspace.reports(matter.matter_id, owner_id)[0]
            report_path = prefix + '/reports?report=' + report.report_id
            details = owner.find_element(By.CSS_SELECTOR, '.report-add-material details:nth-child(2)')
            click(owner, '.report-add-material details:nth-child(2) summary')
            submit(owner, f'form[action$="/from-notebook/{note.item_id}"] button')
            section = bench.workspace.report_sections(matter.matter_id, report.report_id)[0]
            edit = f'#{section.section_id} .report-section-content form:first-child '
            header = '.report-settings form:first-child '
            recovery = '.report-recovery-panel form '
            def add_section(driver, heading, text):
                if not driver.find_element(By.CSS_SELECTOR, '.report-add-material details:first-child').get_attribute('open'):
                    click(driver, '.report-add-material details:first-child summary')
                fill(driver, '.report-add-material details:first-child input[name=heading]', heading)
                fill(driver, '.report-add-material details:first-child textarea[name=body]', text)
                submit(driver, '.report-add-material details:first-child button[type=submit]')
            add_section(owner, 'Generated second section', 'Original second section')
            second = bench.workspace.report_sections(matter.matter_id, report.report_id)[1]
            second_edit = f'#{second.section_id} .report-section-content form:first-child '
            go(admin, report_path)
            if admin.find_elements(By.CSS_SELECTOR, '[data-assistant-collapse]'):
                click(admin, '[data-assistant-collapse]')
            fill(owner, header + 'textarea[name=purpose]', 'Owner saved purpose')
            submit(owner, header + 'button[type=submit]')
            fill(admin, header + 'input[name=title]', 'Member proposed title')
            fill(admin, header + 'textarea[name=purpose]', 'Member unsaved purpose')
            Select(admin.find_element(By.CSS_SELECTOR, header + 'select[name=status]')).select_by_value('final')
            submit(admin, header + 'button[type=submit]')
            assert 'changed since the page was opened' in body(admin) and 'Owner saved purpose' in body(admin)
            assert admin.find_element(By.CSS_SELECTOR, recovery + 'textarea[name=purpose]').get_attribute('value') == 'Member unsaved purpose'
            assert Select(admin.find_element(By.CSS_SELECTOR, recovery + 'select[name=status]')).first_selected_option.get_attribute('value') == 'final'
            # Resolve metadata deliberately as Draft before testing Final below.
            Select(admin.find_element(By.CSS_SELECTOR, recovery + 'select[name=status]')).select_by_value('draft')
            fill(admin, recovery + 'textarea[name=purpose]', 'Reviewed combined purpose')
            submit(admin, recovery + 'button[type=submit]')
            checks.append('Two reviewers discover the same Report; stale title, purpose and status survive comparison and resolution')

            go(owner, report_path)
            go(admin, report_path)
            fill(owner, edit + 'textarea[name=body]', 'Owner saved section')
            submit(owner, edit + 'button[type=submit]')
            fill(admin, edit + 'input[name=heading]', 'Member proposed heading')
            fill(admin, edit + 'textarea[name=body]', 'Member unsaved section')
            submit(admin, edit + 'button[type=submit]')
            assert 'Owner saved section' in body(admin)
            assert admin.find_element(By.CSS_SELECTOR, recovery + 'textarea[name=body]').get_attribute('value') == 'Member unsaved section'
            assert admin.find_element(By.CSS_SELECTOR, '.report-citations a').get_attribute('href') == base + support
            admin.save_screenshot(str(args.output / 'report-conflict-desktop.png'))
            admin.set_window_size(390, 844)
            WebDriverWait(admin, 5).until(lambda d: d.execute_script("return document.getElementById('matter-rail').getBoundingClientRect().right <= 1"))
            assert admin.execute_script('return document.documentElement.scrollWidth <= innerWidth')
            admin.execute_script("arguments[0].scrollIntoView({block: 'start', behavior: 'instant'})", admin.find_element(By.ID, 'unsaved-report-heading'))
            admin.save_screenshot(str(args.output / 'report-conflict-mobile.png'))
            fill(owner, edit + 'textarea[name=body]', 'Owner intervening section')
            submit(owner, edit + 'button[type=submit]')
            submit(admin, recovery + 'button[type=submit]')
            assert 'Owner intervening section' in body(admin)
            fill(admin, recovery + 'textarea[name=body]', 'Combined reviewed Report section for export.')
            submit(admin, recovery + 'button[type=submit]')
            admin.set_window_size(1440, 1000)
            go(admin, support)
            assert 'the blue vehicle arrived at noon' in admin.find_element(By.ID, 'support-pane').text
            checks.append('Section conflict preserves heading, body and exact support; another intervening edit conflicts again and mobile resolution saves')

            go(owner, report_path)
            go(admin, report_path)
            fill(owner, edit + 'input[name=heading]', 'Reviewed source section')
            submit(owner, edit + 'button[type=submit]')
            fill(admin, second_edit + 'textarea[name=body]', 'Member independently reviewed second section')
            submit(admin, second_edit + 'button[type=submit]')
            assert admin.find_element(By.CSS_SELECTOR, second_edit + 'textarea[name=body]').get_attribute('value') == 'Member independently reviewed second section'
            add_section(owner, 'Owner independent addition', 'Owner appended text')
            add_section(admin, 'Member independent addition', 'Member appended text')
            assert len(bench.workspace.report_sections(matter.matter_id, report.report_id)) == 4
            checks.append('Independent section edits and additions from older pages both succeed without losing teammate work')

            go(owner, report_path)
            go(admin, report_path)
            fill(owner, second_edit + 'textarea[name=body]', 'Later section requiring review before Final')
            submit(owner, second_edit + 'button[type=submit]')
            Select(admin.find_element(By.CSS_SELECTOR, header + 'select[name=status]')).select_by_value('final')
            submit(admin, header + 'button[type=submit]')
            assert 'changed since the page was opened' in body(admin)
            assert bench.workspace.report(matter.matter_id, report.report_id).status == 'draft'
            go(admin, report_path)
            assert admin.find_element(By.CSS_SELECTOR, second_edit + 'textarea[name=body]').get_attribute('value') == 'Later section requiring review before Final'
            Select(admin.find_element(By.CSS_SELECTOR, header + 'select[name=status]')).select_by_value('final')
            submit(admin, header + 'button[type=submit]')
            fill(owner, edit + 'textarea[name=body]', 'Older Draft form text')
            submit(owner, edit + 'button[type=submit]')
            assert 'changed since the page was opened' in body(owner) and 'currently Final' in body(owner)
            assert owner.find_element(By.CSS_SELECTOR, recovery + 'textarea[name=body]').get_attribute('value') == 'Older Draft form text'
            go(owner, report_path)
            fill(owner, second_edit + 'textarea[name=body]', 'Deliberate edit of current Final')
            submit(owner, second_edit + 'button[type=submit]')
            assert bench.workspace.report(matter.matter_id, report.report_id).status == 'final'
            checks.append('Final refuses unseen edits; old Draft actions preserve text after Final and current Final remains deliberately editable')

            go(owner, report_path)
            go(admin, report_path)
            submit(owner, f'#{second.section_id} .report-section-order form:first-of-type button')
            submit(admin, f'#{second.section_id} .report-section-order form:first-of-type button')
            assert 'changed since the page was opened' in body(admin)
            go(admin, report_path)
            fill(owner, edit + 'input[name=heading]', 'Reviewed source section for export')
            submit(owner, edit + 'button[type=submit]')
            def delete(driver, selector):
                button = click(driver, selector)
                WebDriverWait(driver, 10).until(EC.alert_is_present()).accept()
                WebDriverWait(driver, 20).until(detached(button))
            delete(admin, f'#{section.section_id} .danger-link')
            assert 'changed since the page was opened' in body(admin)
            go(admin, report_path)
            fill(owner, second_edit + 'input[name=heading]', 'Current Final second section')
            submit(owner, second_edit + 'button[type=submit]')
            delete(admin, '.report-settings .danger-link')
            assert 'changed since the page was opened' in body(admin)
            assert len(bench.workspace.report_sections(matter.matter_id, report.report_id)) == 4
            checks.append('Stale movement, section removal and whole-Report deletion require review and retain newer work')

            go(owner, report_path)
            go(admin, report_path)
            removable = bench.workspace.report_sections(matter.matter_id, report.report_id)[-1]
            removable_edit = f'#{removable.section_id} .report-section-content form:first-child '
            fill(admin, removable_edit + 'textarea[name=body]', 'Unsaved text after section deletion')
            delete(owner, f'#{removable.section_id} .danger-link')
            submit(admin, removable_edit + 'button[type=submit]')
            assert 'was deleted' in body(admin)
            assert admin.find_element(By.CSS_SELECTOR, recovery + 'textarea[name=body]').get_attribute('value') == 'Unsaved text after section deletion'
            assert not admin.find_elements(By.CSS_SELECTOR, recovery + 'button[type=submit]')
            checks.append('Deleted-section recovery retains the unsaved text without recreating the section')

            go(owner, prefix + '/settings')
            before_bundle = set(downloads.iterdir())
            click(owner, f'a[href="{prefix}/export"]')
            bundle = wait.until(lambda _: completed_zip(downloads, before_bundle))
            with zipfile.ZipFile(bundle) as archive:
                texts = [archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith(('.md', '.json'))]
                assert any('Combined reviewed Report section for export.' in text for text in texts)
                manifest = json.loads(archive.read('manifest.json'))
                assert manifest['report_count'] == 1
                entry = manifest['reports'][0]
                assert entry['status'] == 'final' and entry['section_count'] == 3
                assert entry['markdown'] in archive.namelist() and entry['docx'] in archive.namelist()
                assert any('the blue vehicle arrived at noon' in text for text in texts)
                assert not any(name.endswith('generated-source.txt') for name in archive.namelist())
            exit_flow = BrowserCaseExit(owner, base, app, matter, downloads)
            exit_flow.prepare()
            exit_flow.confirm_name(matter.display_name)
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted')
            assert original.read_bytes() == original_bytes
            checks.append('Export contains the reviewed Final Report in both formats and exact source support; normal closure preserves external original')
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
