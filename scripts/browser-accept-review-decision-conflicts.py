#!/usr/bin/env python3
"""Synthetic shared source-validation conflicts, support, export and revocation in Chrome."""
from __future__ import annotations

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


class EvidenceEchoGenerator:
    """Deterministic synthetic source classification through the normal adapter."""
    available = True

    def generate(self, **kwargs):
        evidence = kwargs["evidence"]
        return {
            "answerable": bool(evidence),
            "claims": [{"text": item.excerpt[:600], "evidence_ids": [item.evidence_id]} for item in evidence[:1]],
            "limitation": None, "missing_information": "",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix='recordbench-decision-conflicts-browser-') as temporary:
        root = Path(temporary).resolve()
        downloads = root / 'downloads'
        downloads.mkdir()
        original = root / 'generated-source.txt'
        original.write_bytes(b'Synthetic source: the blue vehicle arrived at noon.\n')
        original_bytes = original.read_bytes()
        app = create_workbench_app(
            root / 'runtime', generator=EvidenceEchoGenerator(), auth_mode='kerberos',
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
            fill(owner, '#matter-name', 'Generated shared decision review')
            submit(owner, '.matter-form button[type=submit]')
            slug = urlparse(owner.current_url).path.split('/')[2]
            prefix = f'/matters/{slug}'
            owner.find_element(By.CSS_SELECTOR, '[data-assistant-collapse]').click()
            collection_name = owner.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic review-decision-conflicts collection")
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
            go(owner, prefix + '/full-review')
            fill(owner, '.criterion-onboarding input[name=title]', 'Generated blue vehicle check')
            fill(owner, '.criterion-onboarding textarea[name=instructions]', 'Include sources that describe the blue vehicle arriving at noon.')
            submit(owner, '.criterion-onboarding button[type=submit]')
            submit(owner, '.review-launcher button[value=sample]')
            run = wait.until(lambda _: next((item for item in bench.workspace.review_runs(matter.matter_id, owner_id) if item.state in ('succeeded','failed')), None))
            assert run.state == 'succeeded'
            go(admin, prefix + '/full-review')
            assert 'Generated blue vehicle check' in body(admin) and 'Source check complete' in body(admin)
            click(admin, '.decision-source')
            decision_path = urlparse(admin.current_url).path + '?' + urlparse(admin.current_url).query
            go(owner, decision_path)
            decision = bench.workspace.review_decision(matter.matter_id, owner_id, run.run_id, document.document_id)
            before_machine = (decision.machine_decision, decision.rationale, decision.citations, decision.source_version_id)
            checks.append('Normal browser criterion/sample run completed; another reviewer discovers the same saved run and source decision')
            form = '#decision-inspector form '
            click(owner, form + 'input[value=include]')
            fill(owner, form + 'textarea[name=note]', 'Owner saved review note')
            submit(owner, form + 'button[type=submit]')
            click(admin, form + 'input[value=exclude]')
            fill(admin, form + 'textarea[name=note]', 'Member old-page review note')
            submit(admin, form + 'button[type=submit]')
            current = bench.workspace.review_decision(matter.matter_id, owner_id, run.run_id, document.document_id)
            assert current.human_decision == 'include' and current.human_note == 'Owner saved review note'
            assert current.reviewed_by == owner_id and 'changed since the page was opened' in body(admin)
            assert 'Saved by Owner' in body(admin) and 'Owner saved review note' in body(admin)
            assert admin.find_element(By.CSS_SELECTOR, 'textarea[name=note]').get_attribute('value') == 'Member old-page review note'
            assert admin.find_element(By.CSS_SELECTOR, 'input[value=exclude]').is_selected()
            assert admin.find_element(By.CSS_SELECTOR, '.decision-citations a').get_attribute('href') == base + support
            admin.save_screenshot(str(args.output / 'decision-conflict-desktop.png'))
            admin.set_window_size(390, 844)
            collapse = admin.find_element(By.CSS_SELECTOR, "[data-assistant-collapse]")
            if collapse.is_displayed():
                click(admin, "[data-assistant-collapse]")
            WebDriverWait(admin, 5).until(lambda d: d.execute_script("return document.getElementById('matter-rail').getBoundingClientRect().right <= 1"))
            assert admin.execute_script('return document.documentElement.scrollWidth <= innerWidth')
            admin.execute_script("arguments[0].scrollIntoView({block: 'start', behavior: 'instant'})", admin.find_element(By.ID, 'unsaved-review-heading'))
            admin.save_screenshot(str(args.output / 'decision-conflict-mobile.png'))
            checks.append('Stale validation preserves proposed choice and note beside current saved reviewer, decision and exact support on desktop/mobile')
            fill(owner, form + 'textarea[name=note]', 'Owner intervening review note')
            submit(owner, form + 'button[type=submit]')
            submit(admin, '.review-decision-form button[type=submit]')
            assert 'Owner intervening review note' in body(admin) and 'changed since the page was opened' in body(admin)
            click(admin, '.review-decision-form input[value=uncertain]')
            fill(admin, '.review-decision-form textarea[name=note]', 'Combined reviewed team note for export')
            submit(admin, '.review-decision-form button[type=submit]')
            admin.set_window_size(1440, 1000)
            current = bench.workspace.review_decision(matter.matter_id, owner_id, run.run_id, document.document_id)
            assert current.human_decision == 'uncertain' and current.human_note == 'Combined reviewed team note for export'
            assert (current.machine_decision, current.rationale, current.citations, current.source_version_id) == before_machine
            assert current.reviewed_by == member_id
            go(owner, decision_path)
            assert 'Saved by Member' in owner.find_element(By.ID, 'decision-inspector').text
            assert owner.find_element(By.CSS_SELECTOR, form + 'textarea[name=note]').get_attribute('value') == current.human_note
            checks.append('Further intervening edit conflicts again; a deliberate mobile resolution saves with team attribution while machine result remains unchanged')
            # A fresh browser request after ordinary membership revocation must fail.
            go(admin, decision_path)
            bench.workspace.revoke_member(matter.matter_id, member_id, owner_id)
            fill(admin, form + 'textarea[name=note]', 'Revoked reviewer canary')
            submit(admin, form + 'button[type=submit]')
            denied = body(admin)
            assert 'Combined reviewed team note for export' not in denied and 'Source decision' not in denied
            assert bench.workspace.review_decision(matter.matter_id, owner_id, run.run_id, document.document_id).human_note == current.human_note
            go(admin, prefix + '/full-review')
            assert 'Generated blue vehicle check' not in body(admin)
            checks.append('Revoked reviewer cannot save an already-open form or reopen shared run; saved machine and human records remain unchanged')
            go(owner, support)
            assert 'the blue vehicle arrived at noon' in owner.find_element(By.ID, 'support-pane').text
            go(owner, prefix + '/settings')
            before_bundle = set(downloads.iterdir())
            click(owner, f'a[href="{prefix}/export"]')
            bundle = wait.until(lambda _: completed_zip(downloads, before_bundle))
            with zipfile.ZipFile(bundle) as archive:
                texts = [archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith(('.md', '.json'))]
                assert any('Combined reviewed team note for export' in text for text in texts)
                assert any('uncertain' in text for text in texts)
                assert any('the blue vehicle arrived at noon' in text for text in texts)
                assert not any(name.endswith('generated-source.txt') for name in archive.namelist())
            exit_flow = BrowserCaseExit(owner, base, app, matter, downloads)
            exit_flow.prepare()
            exit_flow.confirm_name(matter.display_name)
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == 'deleted')
            assert original.read_bytes() == original_bytes
            checks.append('Complete export preserves the current human review, unchanged machine result and exact source support; normal closure preserves external original')
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
