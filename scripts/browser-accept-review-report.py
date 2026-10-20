#!/usr/bin/env python3
"""Convert synthetic research, edit its gaps, and download the retained basis."""


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

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from case_intelligence.generation import UnavailableGenerator
from case_intelligence.workbench import create_workbench_app
from tests.test_report_review_basis import ACTOR, saved_research


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-review-report-") as temporary:
        root = Path(temporary).resolve()
        downloads = root / "downloads"
        downloads.mkdir()
        app = create_workbench_app(root / "runtime", generator=UnavailableGenerator(), auth_mode="test")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        for flag in ("--headless=new", "--disable-dev-shm-usage", "--window-size=1440,1000"):
            options.add_argument(flag)
        options.add_experimental_option("prefs", {"download.default_directory": str(downloads), "download.prompt_for_download": False})
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 20)
            wait.until(lambda _: server.started)
            navigate_to(driver, base + "/", diagnostics=args.output)
            bench = app.state.workbench
            bench.research.close()
            matter = bench.workspace.create_matter("Synthetic Report review", "Browser acceptance", ACTOR)
            job, _ = saved_research(bench, matter)
            navigate_to(driver, base + f"/matters/{matter.slug}/research?job={job.job_id}", diagnostics=args.output)
            for details in driver.find_elements(By.CSS_SELECTOR, 'details'):
                if details.find_elements(By.CSS_SELECTOR, 'form[action$="/report"]'):
                    driver.execute_script("arguments[0].open = true", details)
            button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'form[action$="/report"] button')))
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", button)
            button.click()
            wait.until(lambda d: "/reports?report=" in d.current_url)
            edit_link = wait.until(EC.element_to_be_clickable((By.LINK_TEXT, 'Edit this draft')))
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", edit_link)
            wait.until(lambda d: d.execute_script("const r=arguments[0].getBoundingClientRect(); return r.top >= 56 && r.bottom < innerHeight;", edit_link))
            edit_link.click()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".report-section-card")) == 7)
            heading = driver.find_element(By.CSS_SELECTOR, 'input[value="Gaps and unresolved questions"]')
            form = heading.find_element(By.XPATH, "ancestor::form")
            body = form.find_element(By.CSS_SELECTOR, 'textarea[name="body"]')
            previous = body.get_attribute("value")
            marker = "Reviewer will request the missing gate log."
            body.clear()
            body.send_keys(previous + "\n" + marker)
            save = form.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", save)
            save.click()
            wait.until(EC.staleness_of(save))
            edit_link = wait.until(EC.element_to_be_clickable((By.LINK_TEXT, 'Edit this draft')))
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", edit_link)
            wait.until(lambda d: d.execute_script("const r=arguments[0].getBoundingClientRect(); return r.top >= 56 && r.bottom < innerHeight;", edit_link))
            edit_link.click()
            assert any(marker in element.get_attribute("value") for element in driver.find_elements(By.CSS_SELECTOR, 'textarea[name="body"]'))

            for width, name in ((1440, "desktop"), (390, "mobile")):
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False,
                })
                assert driver.execute_script("return window.innerWidth") == width
                if width < 901:
                    wait.until(lambda d: d.execute_script(
                        "return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"
                    ))
                assert driver.execute_script("return document.documentElement.scrollWidth <= window.innerWidth")
                coverage = driver.find_element(By.CSS_SELECTOR, 'input[value="Scope and coverage"]')
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", coverage)
                driver.save_screenshot(str(args.output / f"review-report-{name}.png"))
            driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
            download = driver.find_element(By.CSS_SELECTOR, 'a[href$="format=markdown"]')
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", download)
            download.click()
            wait.until(lambda _: bool(list(downloads.glob("*.md"))))
            text = next(downloads.glob("*.md")).read_text()
            assert marker in text and "Scope and coverage" in text and job.job_id in text
            receipt = {"provenance": "synthetic", "checks": ["browser conversion", "seven editable sections",
                "saved gap edit", "desktop and 390px mobile without horizontal overflow", "download retains coverage and original run"]}
            (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt))
        finally:
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == "__main__":
    main()
