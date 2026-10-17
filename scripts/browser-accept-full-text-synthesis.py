#!/usr/bin/env python3
"""Synthetic terminal full-text review to synthesis, recovery, and exports in Chrome.

The deterministic source-echo client exercises workflow plumbing and the real
grounding verifier. It is not a representative local-model quality evaluation.
"""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

from browser_navigation import navigate_to

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlparse
import zipfile

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from case_intelligence.generation import GenerationRejected, VerifiedReviewDecision
from case_intelligence.pilot_uploads import PilotUnit
from case_intelligence.workbench import create_workbench_app

ACTOR = "development-taylor-morgan"
SUPPORT = "Synthetic dispatch record 1 states the amber crate was delivered."
COMPETING = "Synthetic dispatch record 24 states the amber crate was not delivered."
LATE = "LateMarginCanary states the amber crate was returned undelivered."
UNSUPPORTED = "A submarine transported 9999 satellites to Jupiter."
MANUAL_NOTE = "Compare both dispatch accounts with the original gate record."


class SourceEcho:
    available = True

    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return {"answerable": bool(kwargs["evidence"]), "claims": [
            {"text": item.excerpt, "evidence_ids": [item.evidence_id]}
            for item in kwargs["evidence"][:8]], "limitation": None,
            "missing_information": ""}


def main():
    from synthetic_browser_environment import isolate_environment
    isolate_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    downloads = output / "downloads"
    downloads.mkdir(exist_ok=True)
    receipt = {"synthetic_only": True, "passed": False,
        "provenance": "Deterministic source echo; workflow validation, not model quality evaluation.",
        "checks": []}

    def checked(message):
        receipt["checks"].append(message)
        print(message, flush=True)

    with tempfile.TemporaryDirectory(prefix=f"{LEGACY_STEM}-full-text-synthesis-browser-") as temporary:
        root = Path(temporary).resolve()
        generator = SourceEcho()
        app = create_workbench_app(root / "runtime", generator=generator, auth_mode="test")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = str(args.chrome_binary)
        # Wait for document detachment ourselves: this avoids a pinned Chrome
        # navigation race while retaining genuine browser clicks and downloads.
        options.page_load_strategy = "none"
        for flag in ("--headless=new", "--disable-dev-shm-usage", "--disable-gpu",
                     "--no-proxy-server", "--window-size=1440,1000"):
            options.add_argument(flag)
        options.add_experimental_option("prefs", {"download.default_directory": str(downloads),
            "download.prompt_for_download": False})
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 45)
            wait.until(lambda _: server.started)

            def ready():
                wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

            def detached(element):
                try:
                    return EC.staleness_of(element)(driver)
                except WebDriverException as exc:
                    if "Node with given id does not belong to the document" not in exc.msg:
                        raise
                    return True

            def go(path):
                old = driver.find_element(By.TAG_NAME, "html")
                navigate_to(driver, base + path, diagnostics=args.output)
                wait.until(lambda _: detached(old))
                ready()

            def activate(selector, *, keyboard=False, navigation=True):
                element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", element)
                if keyboard:
                    # Focus precedes a real Enter key event, never form.submit().
                    driver.execute_script("arguments[0].focus()", element)
                    assert driver.switch_to.active_element == element
                    element.send_keys(Keys.ENTER)
                else:
                    element.click()
                if navigation:
                    wait.until(lambda _: detached(element))
                    ready()

            def screenshot(name, width, target=None):
                driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
                if width < 901:
                    wait.until(lambda d: d.execute_script(
                        "return document.querySelector('[data-matter-rail]').getBoundingClientRect().right <= 1"))
                wait.until(lambda d: d.execute_script(
                    "return document.documentElement.scrollWidth <= window.innerWidth + 2"))
                if target:
                    driver.execute_script("arguments[0].scrollIntoView({block:'start',behavior:'instant'})",
                        driver.find_element(By.CSS_SELECTOR, target))
                    driver.execute_script("window.scrollBy({top:-80,left:0,behavior:'instant'})")
                else:
                    driver.execute_script("window.scrollTo(0,0)")
                driver.save_screenshot(str(output / f"{name}-{width}.png"))
                driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})

            def download(job_id, format_name, suffix):
                selector = f'a[href="{prefix}/research/{job_id}/export?format={format_name}"]'
                link = driver.find_element(By.CSS_SELECTOR, selector)
                details = link.find_element(By.XPATH, "ancestor::details")
                if not details.get_attribute("open"):
                    summary = details.find_element(By.TAG_NAME, "summary")
                    driver.execute_script("arguments[0].scrollIntoView({block:'center',behavior:'instant'})", summary)
                    summary.click()
                before = set(downloads.glob("*" + suffix))
                activate(selector, navigation=False)
                return wait.until(lambda _: next((path for path in downloads.glob("*" + suffix)
                    if path not in before and path.stat().st_size > 0), None))

            go("/")
            bench = app.state.workbench
            matter = bench.create_matter("Synthetic dispatch synthesis", "Browser acceptance", ACTOR)
            prefix = f"/matters/{matter.slug}"
            store = bench.source_store(matter)
            documents = []

            def source(name, texts):
                document, _ = store.store_stream(name, "text/plain", io.BytesIO(
                    ("Synthetic original: " + name).encode()))
                document.units = [asdict(PilotUnit(number, value,
                    excerpt_digest=hashlib.sha256(value.encode()).hexdigest()))
                    for number, value in enumerate(texts, 1)]
                document.page_count = len(texts)
                document.completed_units = document.total_units = len(texts)
                store._save([document.document_id])
                documents.append(document)
                return document

            dispatch = source("synthetic-dispatch.txt", [
                f"Synthetic dispatch record {number} states the amber crate was delivered."
                if number < 24 else COMPETING for number in range(1, 25)])
            source("synthetic-no-finding.txt", ["Routine entry about a blue parcel."])
            source("synthetic-failed.txt", ["ClassificationFailureCanary needs direct review."])
            source("synthetic-empty.txt", [""])
            source("synthetic-unsupported.txt", ["UnsupportedFindingCanary records a routine parcel."])
            long_text = "Routine margin. " * 500 + LATE
            assert long_text.index(LATE) > 6000
            source("synthetic-late-margin.txt", [long_text])
            bench._sync_source_catalog(matter, documents)
            criterion, _ = bench.workspace.create_review_criterion(matter.matter_id, ACTOR,
                title="Amber crate dispatch", instructions="Find statements about delivery of the amber crate.")

            def classify(**kwargs):
                text = kwargs["evidence"][0].excerpt
                if "ClassificationFailureCanary" in text:
                    raise GenerationRejected("Synthetic classification failure")
                if "UnsupportedFindingCanary" in text:
                    return VerifiedReviewDecision("include", UNSUPPORTED, ("S1",), True, 1)
                included = "amber crate" in text
                rationale = LATE if LATE in text else text if included else "No amber crate finding."
                return VerifiedReviewDecision("include" if included else "not_identified",
                    rationale, ("S1",) if included else (), True, 1)

            bench.generator.classify_source = classify
            go(prefix + f"/full-review?criterion={criterion.criterion_id}")
            activate('button[value="full_text"]')
            run_id = parse_qs(urlparse(driver.current_url).query)["run"][0]
            wait.until(lambda _: bench.workspace.review_run(matter.matter_id, ACTOR, run_id).state == "succeeded")
            review_path = prefix + f"/full-review?criterion={criterion.criterion_id}&run={run_id}"
            go(review_path)
            assert "Synthesize saved findings" in driver.find_element(By.TAG_NAME, "body").text
            screenshot("terminal-review", 1440, f'form[action$="/{run_id}/synthesize"]')
            screenshot("terminal-review", 390, f'form[action$="/{run_id}/synthesize"]')
            checked("Terminal full-text review offers one synthesis action after actual 24-unit review with separate gap outcomes")

            # Interrupt after two verified issue nodes and the next charged call
            # have been saved, before that call can return any model response.
            checkpoint = bench.workspace.checkpoint_research_job
            interrupted = []

            def interrupt_once(job_id, result, *args, **kwargs):
                saved = checkpoint(job_id, result, *args, **kwargs)
                hierarchy = result.get("hierarchical_synthesis", {})
                if not interrupted and len(hierarchy.get("issue", [])) == 2 and hierarchy.get("requests_spent") == 3:
                    interrupted.append(deepcopy(hierarchy))
                    raise RuntimeError("Synthetic browser interruption after saved issue work")
                return saved

            bench.workspace.checkpoint_research_job = interrupt_once
            activate(f'form[action$="/{run_id}/synthesize"] button', keyboard=True)
            job_id = parse_qs(urlparse(driver.current_url).query)["job"][0]
            wait.until(lambda _: bench.workspace.research_job(matter.matter_id, ACTOR, job_id).state == "failed")
            bench.workspace.checkpoint_research_job = checkpoint
            assert interrupted
            go(prefix + f"/research?job={job_id}")
            activate("details.research-finding > summary", navigation=False)
            assert "3 of 32 generation requests charged" in driver.find_element(By.TAG_NAME, "body").text
            assert "Issue section 1" in driver.find_element(By.TAG_NAME, "body").text
            assert "Resume synthesis" in driver.find_element(By.TAG_NAME, "body").text
            screenshot("saved-interruption", 390, "details.research-finding")
            checked("Keyboard synthesis activation reaches inspectable failed checkpoint with two saved issue nodes and three charged requests")

            activate(f'form[action$="/{job_id}/retry"] button', keyboard=True)
            wait.until(lambda _: bench.workspace.research_job(matter.matter_id, ACTOR, job_id).state == "succeeded")
            go(prefix + f"/research?job={job_id}")
            job = bench.workspace.research_job(matter.matter_id, ACTOR, job_id)
            hierarchy = job.result["hierarchical_synthesis"]
            assert hierarchy["issue"][:2] == interrupted[0]["issue"]
            assert len({node["id"] for node in hierarchy["issue"]}) == len(hierarchy["issue"])
            assert hierarchy["requests_spent"] == len(generator.calls) + 1
            assert SUPPORT in job.result["summary"] and COMPETING in job.result["summary"]
            assert UNSUPPORTED not in job.result["summary"]
            input_receipt = job.result["full_text_synthesis_input"]
            reasons = {reason for _, reason in input_receipt["outcomes"]}
            assert {"unsupported_finding", "oversized_original"} <= reasons
            assert input_receipt["counts"]["admitted"] == 24
            assert input_receipt["coverage"]["ranges"]["failed"] == 1
            assert input_receipt["coverage"]["ranges"]["no_finding"] >= 2
            assert input_receipt["coverage"]["units"]["empty"] == 1
            assert len(input_receipt["sources"]) == 6
            assert input_receipt["human_decisions"]["mode"] == "frozen_context_not_evidence"
            assert input_receipt["decision_revision_digest"]
            assert LATE not in job.result["summary"]
            assert all(LATE not in item.excerpt for call in generator.calls for item in call["evidence"])
            checked("Resume preserves exact saved nodes, charges the interrupted request, and retains both original accounts across more than twelve units")
            checked("Unsupported rationale and the decisive statement after character 6000 have inspectable omission reasons and never become model evidence")

            activate(".synthesis-input-receipt > details > summary", navigation=False)
            receipt_card = driver.find_element(By.CSS_SELECTOR, ".synthesis-input-receipt")
            assert "24 of 26 candidate findings admitted" in receipt_card.text
            assert "Unsupported finding" in receipt_card.text and "Oversized original" in receipt_card.text
            assert "empty 1" in receipt_card.text and "failed 1" in receipt_card.text
            screenshot("synthesis-input-omissions", 390, ".synthesis-input-receipt")
            activate("details.research-finding > summary", navigation=False)
            inspector = driver.find_element(By.CSS_SELECTOR, "details.research-finding")
            assert SUPPORT in inspector.text and COMPETING in inspector.text
            assert "Intermediate summaries are not evidence sources" in inspector.text
            assert "partial" in driver.find_element(By.TAG_NAME, "body").text.lower()
            for width in (1440, 390):
                screenshot("synthesis-inspector", width, "details.research-finding")
            research_path = prefix + f"/research?job={job_id}"
            for unit_number, text in ((1, SUPPORT), (24, COMPETING)):
                matching = [link for link in driver.find_elements(By.CSS_SELECTOR, "#research-support a")
                    if f"page {unit_number}" in link.text.lower()]
                assert matching, f"No original navigation for unit {unit_number}"
                href = matching[0].get_attribute("href")
                go(href.removeprefix(base))
                wait.until(lambda d: text in d.find_element(By.TAG_NAME, "body").text)
                assert text in driver.find_element(By.ID, "support-pane").text
                activate(".support-header-open")
                assert text in driver.find_element(By.CSS_SELECTOR, ".extracted-reader").text
                go(research_path)
            checked("Shared hierarchy inspector and original-source reader retain supporting and competing accounts with 390px reflow")

            exported_paths = {}
            for format_name, suffix in (("json", ".json"), ("markdown", ".md"), ("docx", ".docx")):
                path = download(job_id, format_name, suffix)
                exported_paths[format_name] = {"file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                if format_name == "docx":
                    with zipfile.ZipFile(path) as archive:
                        exported_text = archive.read("word/document.xml").decode()
                else:
                    exported_text = path.read_text()
                assert SUPPORT in exported_text and COMPETING in exported_text
                assert "synthetic-dispatch.txt" in exported_text
                assert "partial" in exported_text.lower()
                if format_name == "json":
                    exported = json.loads(exported_text)["investigation"]
                    assert exported["full_text_synthesis_input"] == input_receipt
                    assert exported["hierarchical_synthesis"]["requests_spent"] == hierarchy["requests_spent"]
            receipt["exports"] = exported_paths
            checked("Browser JSON, Markdown, and Word downloads retain both accounts, original citations, input receipt, hierarchy, and partial coverage")

            # Human validation remains available with AI unavailable. This
            # decision edit occurs after exports and must not change their bytes.
            generator.available = False
            go(review_path + f"&source={dispatch.document_id}")
            activate('.review-decision-form input[value="uncertain"]', navigation=False)
            note = driver.find_element(By.CSS_SELECTOR, '.review-decision-form textarea[name="note"]')
            note.send_keys(MANUAL_NOTE)
            activate('.review-decision-form button[type="submit"]', keyboard=True)
            decision = bench.workspace.review_decision(matter.matter_id, ACTOR, run_id, dispatch.document_id)
            assert decision.human_decision == "uncertain" and decision.human_note == MANUAL_NOTE
            assert "Open source" in driver.find_element(By.ID, "decision-inspector").text
            screenshot("manual-ai-unavailable", 390, ".review-decision-form")
            checked("AI-unavailable terminal review still permits keyboard human validation and original-source navigation")
            receipt.update(passed=True, browser_version=driver.capabilities.get("browserVersion"),
                generation_requests_charged=hierarchy["requests_spent"],
                generation_responses=len(generator.calls),
                input_receipt=job.result["full_text_synthesis_input"])
        except Exception:
            if driver is not None:
                driver.save_screenshot(str(output / "failure.png"))
                (output / "failure.html").write_text(driver.page_source)
                print(driver.find_element(By.TAG_NAME, "body").text[-4500:], flush=True)
            raise
        finally:
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
    print(json.dumps({key: value for key, value in receipt.items() if key != "input_receipt"}))


if __name__ == "__main__":
    main()
