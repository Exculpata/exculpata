#!/usr/bin/env python3
"""Exercise plain exact/proximity search with temporary synthetic sources."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlencode

import uvicorn
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.workbench import create_workbench_app

ACTOR = "development-taylor-morgan"


class SyntheticAnswerGenerator:
    available = True

    def generate(self, **kwargs):
        evidence = kwargs["evidence"]
        return {"answerable": bool(evidence), "claims": [
            {"text": item.excerpt, "evidence_ids": [item.evidence_id]}
            for item in evidence[:1]], "limitation": None, "missing_information": ""}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # This acceptance process must not inherit deployed storage, source mounts,
    # PostgreSQL, or worker endpoints from the host's Exculpata environment.
    for name in tuple(os.environ):
        if name.startswith(("CASE_INTELLIGENCE_", "CASE_REVIEW_")):
            os.environ.pop(name)
    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-exact-search-") as temporary:
        app = create_workbench_app(Path(temporary).resolve() / "runtime", auth_mode="test",
            generator=SyntheticAnswerGenerator(), learned_retrieval=False, background_ingestion=False)
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
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 20)
            wait.until(lambda _: server.started)
            navigate_to(driver, base + "/", diagnostics=args.output)
            bench = app.state.workbench
            matter = bench.create_matter("Synthetic nearby details", "Invented browser acceptance records", ACTOR)
            store = bench.source_store(matter)
            for index in range(31):
                text = ("The depot recorded the red bicycle." if index == 1 else
                        "A red bicycle " + "neutral " * 120 + "depot." if index == 2 else
                        "A red bicycle returned to the depot.")
                store.store_stream(f"Synthetic record {index:02}.txt", "text/plain", io.BytesIO(text.encode()))
            collection = bench.workspace.create_source_collection(matter.matter_id, "Synthetic selected collection", "upload", ACTOR)
            bench._sync_source_catalog(matter, list(store.documents.values()))
            bench.workspace.reconcile_source_organizations(matter.matter_id, tuple((item.document_id, "upload", item.display_name) for item in store.documents.values()))
            bench.workspace.move_sources_to_collection(matter.matter_id, list(store.documents), collection.collection_id, ACTOR)
            path = base + f"/matters/{matter.slug}/exact-search"

            def click(element):
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});", element)
                element.click()

            def fill(identifier, value):
                element = driver.find_element(By.ID, identifier)
                element.clear()
                element.send_keys(value)
                return element

            def expect_count(number):
                # Read within one browser command so a navigation cannot detach
                # an element between Selenium's lookup and text extraction.
                wait.until(lambda d: d.execute_script(
                    "return document.getElementById('find-results-heading')?.textContent.trim() === arguments[0]",
                    f"{number} sources found"))

            def screenshot(name, width):
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False,
                })
                if width < 901:
                    wait.until(lambda d: d.execute_script(
                        "return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                assert driver.execute_script("return document.documentElement.scrollWidth <= window.innerWidth")
                driver.save_screenshot(str(args.output / f"{name}.png"))

            navigate_to(driver, base + f"/matters/{matter.slug}/home", diagnostics=args.output)
            click(driver.find_element(By.CSS_SELECTOR, '.matter-section-tabs a[href$="/exact-search"]'))
            wait.until(lambda d: d.current_url == path)
            assert driver.find_element(By.CSS_SELECTOR, '.matter-section-tabs [aria-current="page"]').text == "Search"
            assert not driver.find_element(By.ID, "find-query").is_displayed()
            for width in (901, 960, 1000):
                screenshot(f"exact-search-tabs-{width}", width)
                tabs = driver.find_element(By.CSS_SELECTOR, ".matter-section-tabs")
                last = tabs.find_elements(By.CSS_SELECTOR, "a")[-1]
                driver.execute_script("arguments[0].scrollIntoView({block:'nearest',inline:'end'})", last)
                assert driver.execute_script("const a=arguments[0].getBoundingClientRect(), b=arguments[1].getBoundingClientRect(); return a.left >= b.left-1 && a.right <= b.right+1", last, tabs)
            screenshot("exact-search-first-use", 1440)
            click(driver.find_element(By.CSS_SELECTOR, ".find-refinements > summary"))
            collection = Select(driver.find_element(By.ID, "find-collection"))
            collection.select_by_visible_text("Synthetic selected collection")
            selected_collection = collection.first_selected_option.get_attribute("value")
            fill("find-words", "bicycle").send_keys(Keys.ENTER)
            expect_count(31)
            click(driver.find_element(By.CSS_SELECTOR, ".find-pagination a"))
            wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR, ".find-pagination"), "Page 2 of 2"))
            assert len(driver.find_elements(By.CSS_SELECTOR, ".find-document")) == 6
            assert "collection=" + selected_collection in driver.current_url
            search_context = driver.current_url
            click(driver.find_element(By.CSS_SELECTOR, ".find-open-source"))
            wait.until(lambda d: "/sources/" in d.current_url)
            click(driver.find_element(By.XPATH, '//a[text()="Return to review context"]'))
            wait.until(lambda d: d.current_url == search_context)
            click(driver.find_element(By.CSS_SELECTOR, '.find-document form button'))
            wait.until(lambda d: "source_set=" in d.current_url and "#matter-question" in d.current_url)
            assert Select(driver.find_element(By.ID, "answer-source-set")).first_selected_option.text.startswith("Only")
            fill("matter-question", "What does the bicycle record say?")
            click(driver.find_element(By.CSS_SELECTOR, "[data-question-form] .ask-button"))
            wait.until(lambda d: d.current_url.endswith("#latest"))
            assert "entity_return_to=" in driver.current_url
            # Keep the synthetic investigation queued so its native controls are deterministic.
            bench.research.close()
            bench.research = None
            fill("matter-question", "Investigate the bicycle arrival")
            click(driver.find_element(By.CSS_SELECTOR, "[data-question-form] .investigate-button"))
            details = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".conversation-research-details")))
            click(details)
            cancel = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'form[action*="/cancel"] button')))
            click(cancel)
            resume = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'form[action*="/retry"] button')))
            click(resume)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'form[action*="/cancel"] button')))
            click(driver.find_element(By.XPATH, '//a[text()="Return to review context"]'))
            wait.until(lambda d: d.current_url == search_context)
            assert len(driver.find_elements(By.CSS_SELECTOR, ".find-document")) == 6


            navigate_to(driver, path, diagnostics=args.output)
            click(driver.find_element(By.CSS_SELECTOR, ".find-refinements > summary"))
            fill("find-proximity-first", "red bicycle")
            fill("find-proximity-second", "depot")
            fill("find-proximity-gap", "4")
            Select(driver.find_element(By.ID, "find-proximity-order")).select_by_value("first")
            driver.find_element(By.ID, "find-proximity-second").send_keys(Keys.ENTER)
            expect_count(29)
            assert "proximity_order=first" in driver.current_url
            assert "red bicycle returned to the depot" in driver.find_element(By.CSS_SELECTOR, ".find-excerpt mark").text
            driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'});",
                                  driver.find_element(By.CSS_SELECTOR, ".find-proximity"))
            screenshot("proximity-controls-desktop", 1440)
            screenshot("proximity-controls-mobile", 390)
            Select(driver.find_element(By.ID, "find-proximity-order")).select_by_value("either")
            click(driver.find_element(By.CSS_SELECTOR, ".find-filter-actions button"))
            expect_count(30)
            click(driver.find_element(By.CSS_SELECTOR, ".find-pagination a"))
            wait.until(EC.text_to_be_present_in_element((By.CSS_SELECTOR, ".find-pagination"), "Page 2 of 2"))
            assert len(driver.find_elements(By.CSS_SELECTOR, ".find-document")) == 5
            assert "proximity_first=red+bicycle" in driver.current_url
            assert driver.execute_script("return document.documentElement.scrollWidth <= window.innerWidth")

            # Retained basic words may not replace an expression submitted by Enter.
            navigate_to(driver, path + "?words=missing", diagnostics=args.output)
            click(driver.find_element(By.CSS_SELECTOR, ".find-refinements > summary"))
            click(driver.find_element(By.CSS_SELECTOR, ".find-advanced > summary"))
            fill("find-query", '"red bicycle" NEAR/4 depot').send_keys(Keys.ENTER)
            expect_count(30)
            assert "advanced=1" in driver.current_url
            link = driver.find_element(By.CSS_SELECTOR, ".find-location")
            assert "?unit=1" in link.get_attribute("href")
            click(link)
            wait.until(lambda d: "/sources/" in d.current_url)
            assert "Review Synthetic record" in driver.title
            assert "red bicycle returned to the depot" in driver.find_element(By.TAG_NAME, "body").text
            screenshot("proximity-source-mobile", 390)
            # Exercise distinct conditions in one long unit through rendered
            # highlights, including a literal inside the proximity span.
            mixed = "red " + "neutral " * 50 + "anchor " + "neutral " * 49 + "bicycle"
            store.store_stream("Synthetic mixed evidence.txt", "text/plain", io.BytesIO(mixed.encode()))
            navigate_to(driver, path + "?" + urlencode({"q": "anchor AND red NEAR/100 bicycle"}), diagnostics=args.output)
            wait.until(lambda d: "1 source found" in d.find_element(By.ID, "find-results-heading").text)
            excerpt = driver.find_element(By.CSS_SELECTOR, ".find-excerpt").text
            assert all(word in excerpt for word in ("anchor", "red", "bicycle"))
            assert len(excerpt) <= 605
            screenshot("mixed-evidence-mobile", 390)
            receipt = {
                "provenance": "temporary synthetic sources; no model or live backend required",
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "working_tree_clean": not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip(),
                "checks": ["matter Search navigation and active label", "collection and page-2 search retained through inspection and source-scoped conversation", "plain first-use form", "primary Enter search and complete pagination",
                    "plain ordered proximity Enter returns 29 of 31", "either-order Apply filters returns 30 of 31",
                    "proximity fields retained on page 2", "advanced Enter overrides retained basic words",
                    "matching span highlight", "mixed literal/proximity evidence remains visible", "source link opens original unit", "desktop and 390px mobile without horizontal overflow"],
            }
            (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt))
        except Exception:
            if driver:
                driver.save_screenshot(str(args.output / "failure.png"))
            raise
        finally:
            if driver:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == "__main__":
    main()
