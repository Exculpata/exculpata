#!/usr/bin/env python3
"""Exercise compact account controls with a disposable authenticated session."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.parse import urlsplit

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from case_intelligence.generation import UnavailableGenerator  # noqa: E402
from case_intelligence.identity import LocalAccountSettings, SESSION_COOKIE  # noqa: E402
from case_intelligence.local_accounts import LocalAccountRepository  # noqa: E402
from case_intelligence.managed_storage import StoragePolicy  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402
from synthetic_browser_environment import isolate_environment  # noqa: E402


def main():
    isolate_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome-binary', type=Path, required=True)
    parser.add_argument('--chromedriver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and
            (not args.output.is_dir() or any(args.output.iterdir()))):
        raise ValueError('Choose a fresh or empty output directory.')
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'synthetic_only': True, 'checks': [],
        'provenance': 'Temporary local account and browser profile; loopback only; no installed runtime.'}
    driver = None
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-account-browser-") as temporary:
        root = Path(temporary).resolve()
        accounts = LocalAccountRepository(root / 'accounts/accounts.json')
        accounts.initialize('synthetic.reviewer', 'Synthetic Reviewer',
            'synthetic-account-password', actor='synthetic-operator')
        app = create_workbench_app(root / 'runtime', auth_mode='local', secure_cookie=True,
            local_settings=LocalAccountSettings(accounts.path), generator=UnavailableGenerator(),
            learned_retrieval=False, background_ingestion=False,
            storage_policy=StoragePolicy(reserve_bytes=0))
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        # Chromium treats loopback as a trustworthy origin for Secure cookies.
        # The test never relaxes the application's secure-cookie requirement.
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            options = Options()
            options.binary_location = str(args.chrome_binary)
            # Match the existing disposable journeys on hosted Linux runners.
            if sys.platform == "linux":
                options.add_argument("--no-sandbox")
            for flag in ('--headless=new', '--disable-dev-shm-usage', '--no-proxy-server'):
                options.add_argument(flag)
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 15)
            wait.until(lambda _: server.started)
            find = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)
            js = driver.execute_script
            driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',
                {'width': 320, 'height': 568, 'deviceScaleFactor': 1, 'mobile': False})
            navigate_to(driver, base + '/auth/login?next=/matters/new', diagnostics=args.output)
            find('[name=username]').send_keys('synthetic.reviewer')
            find('[name=password]').send_keys('synthetic-account-password')
            login = find('.local-login-form button')
            js('arguments[0].scrollIntoView({block:"center",behavior:"instant"})', login)
            login.click()
            wait.until(lambda _: urlsplit(driver.current_url).path == '/matters/new')
            wait.until(lambda _: find('#matter-name').is_displayed())
            session = driver.get_cookie(SESSION_COOKIE)
            assert session and session['secure'] and session['httpOnly'], 'Authenticated session must be Secure and HttpOnly'
            report['checks'].append('Local login creates a Secure HttpOnly session at 320px')
            find('#matter-name').send_keys('Synthetic account-controls matter with a long name')
            find('#matter-descriptor').send_keys('Synthetic browser acceptance')
            find('#matter-name').submit()
            wait.until(lambda _: '/matters/m-' in driver.current_url)
            prefix = '/'.join(driver.current_url[len(base):].split('/')[:3])
            protected = base + prefix + '/notebook'
            navigate_to(driver, protected, diagnostics=args.output)
            wait.until(lambda _: js('return document.documentElement.scrollWidth <= innerWidth'))
            report['checks'].append('Authenticated long-name header fits 320px without horizontal overflow')
            from browser_appearance import select_appearance
            select_appearance(driver, wait, 'light', 'cyan')
            select_appearance(driver, wait, 'dark', 'amber')
            driver.refresh()
            wait.until(lambda _: js('return document.documentElement.dataset.theme') == 'dark'
                       and js('return document.documentElement.dataset.accent') == 'amber')
            report['checks'].append('Compact account appearance controls persist Dark and Amber across reload')
            find('.account-menu > summary').send_keys(Keys.ENTER)
            signout = find('.logout-form button')
            signout.send_keys(Keys.NULL)
            js('arguments[0].scrollIntoView({block:"nearest",behavior:"instant"})', signout)
            bounds = js('return arguments[0].getBoundingClientRect().toJSON()', signout)
            assert 0 <= bounds['left'] < bounds['right'] <= 320
            assert 0 <= bounds['top'] < bounds['bottom'] <= 568
            assert signout.text == 'Sign out'
            report['checks'].append('Sign out remains reachable inside the short mobile account panel')
            signout.send_keys(Keys.ENTER)
            wait.until(lambda _: '/auth/login' in driver.current_url)
            assert driver.get_cookie(SESSION_COOKIE) is None
            report['checks'].append('Keyboard sign-out clears the authenticated browser session')
            navigate_to(driver, protected, diagnostics=args.output)
            wait.until(lambda _: '/auth/login' in driver.current_url)
            # A replayed pre-logout token must also fail, proving server revocation.
            driver.add_cookie({'name': SESSION_COOKIE, 'value': session['value'],
                'path': '/', 'secure': True, 'httpOnly': True})
            navigate_to(driver, protected, diagnostics=args.output)
            wait.until(lambda _: '/auth/login' in driver.current_url)
            report['checks'].append('Protected matter rejects both signed-out access and the revoked session')
            report['passed'] = True
        except Exception as exc:
            report['error'] = {'type': type(exc).__name__, 'message': str(exc)[:1000]}
            raise
        finally:
            if driver:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            (args.output / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
