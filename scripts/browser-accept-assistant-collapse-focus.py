#!/usr/bin/env python3
"""Synthetic regression for deferred assistant collapse and expand focus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from browser_navigation import navigate_to
from synthetic_browser_environment import isolate_environment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app

DELAY_COLLAPSE_FRAME = """
(() => {
    const originalFrame = window.requestAnimationFrame.bind(window);
    let delayNext = false;
    window.__assistantCollapseFrame = {completed: false};
    document.addEventListener('click', event => {
        if (event.target.closest('[data-assistant-collapse]')) {
            delayNext = true;
            window.__assistantCollapseFrame = {completed: false};
        }
    }, true);
    window.requestAnimationFrame = callback => {
        if (!delayNext) return originalFrame(callback);
        delayNext = false;
        const frame = window.__assistantCollapseFrame;
        return setTimeout(() => originalFrame(timestamp => {
            frame.collectionFocused = document.activeElement ===
                document.querySelector('[data-upload-collection-name]');
            callback(timestamp);
            frame.completed = true;
        }), 150);
    };
})();
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("Choose a new or empty output directory.")
    args.output.mkdir(parents=True, exist_ok=True)
    isolate_environment()
    report = dict(synthetic_only=True, passed=False, checks=[])
    driver = None
    with tempfile.TemporaryDirectory(prefix="exculpata-collapse-focus-") as temporary:
        app = create_workbench_app(
            Path(temporary).resolve() / "runtime", auth_mode="preview",
            generator=UnavailableGenerator(), background_ingestion=False,
            learned_retrieval=False,
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                     "--no-proxy-server", "--window-size=1440,1000"):
            options.add_argument(flag)
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(30)
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": DELAY_COLLAPSE_FRAME,
            })
            report["browser_version"] = driver.capabilities["browserVersion"]
            wait = WebDriverWait(driver, 20)
            wait.until(lambda _: server.started)

            def find(selector):
                return driver.find_element(By.CSS_SELECTOR, selector)

            def go(path):
                navigate_to(driver, base + path, diagnostics=args.output)

            def submit(selector):
                element = find(selector)
                element.click()
                wait.until(EC.staleness_of(element))

            def collapsed():
                return driver.execute_script(
                    "return document.body.classList.contains('assistant-collapsed')")

            def wait_for_collapse_frame():
                time.sleep(0.4)
                wait.until(lambda d: d.execute_script(
                    "return window.__assistantCollapseFrame.completed"))

            def expand():
                find("[data-assistant-expand]").click()
                wait.until(lambda d: d.execute_script("""
                    const dock = document.querySelector('[data-assistant-dock]');
                    const target = dock.querySelector('textarea:not(:disabled)')
                        || dock.querySelector('[data-assistant-collapse]');
                    return !document.body.classList.contains('assistant-collapsed')
                        && document.activeElement === target;
                """))

            go("/auth/login?next=/matters/manage")
            submit('button[name="identity_subject"][value="taylor-morgan"]')
            wait.until(lambda d: "/auth/login" not in d.current_url)
            go("/matters/new")
            find("#matter-name").send_keys("Synthetic assistant focus acceptance")
            submit(".matter-form button[type=submit]")
            wait.until(lambda d: "/setup" in d.current_url)
            if collapsed():
                expand()
            assert not collapsed()

            name = find("[data-upload-collection-name]")
            find("[data-assistant-collapse]").click()
            name.click()
            name.send_keys("Synthetic collapse focus check")
            wait_for_collapse_frame()
            assert driver.execute_script(
                "return window.__assistantCollapseFrame.collectionFocused"
            ), "The name field must take focus before the delayed collapse callback"
            try:
                assert name.get_attribute("value") == "Synthetic collapse focus check", "Collection name was truncated"
                assert collapsed(), "Assistant did not remain collapsed"
                assert driver.switch_to.active_element == name, "Collapse stole collection name focus"
            except AssertionError as exc:
                report["failures"] = [f"A: {exc}"]
            else:
                report["checks"].append("A: Delayed collapse preserves collection name typing and focus")

            expand()
            find("[data-assistant-collapse]").click()
            wait_for_collapse_frame()
            assert collapsed()
            assert driver.switch_to.active_element == find("[data-assistant-expand]")
            report["checks"].append("B: Collapse with no other action focuses the expand tab")

            expand()
            report["checks"].append("C: Expand with no other action focuses the enabled textarea or collapse button")
            assert not report.get("failures"), report.get("failures")
            report["passed"] = True
        finally:
            try:
                if driver:
                    driver.quit()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                listener.close()
                (args.output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
