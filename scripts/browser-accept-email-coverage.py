#!/usr/bin/env python3
"""Generated email coverage through actual Chrome upload, review and export."""
from __future__ import annotations

import argparse
from email.message import EmailMessage
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, urlparse
import zipfile

import uvicorn
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from browser_case_exit import BrowserCaseExit, completed_zip, preview_login  # noqa: E402
from case_intelligence.extended_extract import EMAIL_COVERAGE_NOTICE  # noqa: E402
from case_intelligence.malware_scan import MalwareScanResult  # noqa: E402
from case_intelligence.workbench import create_workbench_app  # noqa: E402

ACTOR = "development-taylor-morgan"


class CleanScanner:
    def scan(self, _path):
        return MalwareScanResult("clean", "synthetic")


class EvidenceEchoGenerator:
    available = True

    def generate(self, **kwargs):
        evidence = kwargs["evidence"]
        return {"answerable": bool(evidence), "claims": [
            {"text": evidence[0].excerpt, "evidence_ids": [evidence[0].evidence_id]}
        ] if evidence else [], "limitation": None,
            "missing_information": "" if evidence else "No matching support."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome-binary", type=Path, required=True)
    parser.add_argument("--chromedriver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    downloads = output / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    report = {"synthetic_only": True, "passed": False, "checks": []}

    def checked(message):
        report["checks"].append(message)
        print(message, flush=True)

    with tempfile.TemporaryDirectory(prefix="recordbench-email-browser-") as temporary:
        root = Path(temporary).resolve()
        original = root / "generated.eml"
        message = EmailMessage()
        message["From"] = "sender@example.test"
        message["To"] = "reader@example.test"
        message["Subject"] = "Generated attachment review"
        message.set_content("ParentBodyCanary recorded the generated meeting at noon.")
        attached = EmailMessage()
        attached["Subject"] = "Generated attached email"
        attached.set_content("AttachmentOnlyCanary describes a different event.")
        message.add_attachment(attached, filename="forwarded.eml")
        message.add_attachment("TextAttachmentCanary", filename="notes.txt")
        original_bytes = message.as_bytes()
        original.write_bytes(original_bytes)
        app = create_workbench_app(root / "runtime", generator=EvidenceEchoGenerator(),
            auth_mode="preview", background_ingestion=True, ingestion_workers=1,
            malware_scanner=CleanScanner(), malware_scan_mode="extended")
        bench = app.state.workbench
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
            log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            assert time.monotonic() < deadline, "Generated server did not start"
            time.sleep(.05)
        base = f"http://127.0.0.1:{port}"
        options = Options()
        options.binary_location = str(args.chrome_binary)
        options.page_load_strategy = "none"
        for flag in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                     "--disable-gpu", "--no-proxy-server", "--window-size=1440,1000"):
            options.add_argument(flag)
        options.add_experimental_option("prefs", {"download.default_directory": str(downloads),
            "download.prompt_for_download": False})
        driver = None
        try:
            driver = webdriver.Chrome(service=Service(str(args.chromedriver)), options=options)
            wait = WebDriverWait(driver, 30)

            def ready():
                wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

            def detached(element):
                try:
                    return EC.staleness_of(element)(driver)
                except WebDriverException as exc:
                    if "Node with given id does not belong to the document" not in exc.msg:
                        raise
                    return True

            def page_text():
                # Pages can swap in new content while a wait reads them.
                try:
                    return driver.find_element(By.TAG_NAME, "body").text
                except StaleElementReferenceException:
                    return ""
                except WebDriverException as exc:
                    if "Node with given id does not belong to the document" not in exc.msg:
                        raise
                    return ""

            def rail_hidden():
                # The narrow rail slides off-canvas over a short transition.
                rail = "document.querySelector('[data-matter-rail]')"
                try:
                    wait.until(lambda d: d.execute_script(f"return {rail}.getBoundingClientRect().right <= 1"))
                except TimeoutException:
                    state = driver.execute_script(f"return {{width: innerWidth, body: document.body.className, "
                        f"right: {rail}.getBoundingClientRect().right, transform: getComputedStyle({rail}).transform}}")
                    raise AssertionError(f"Matter rail stayed on-screen at narrow width: {state}") from None
                assert driver.execute_script(f"return {rail}.getAttribute('aria-hidden') === 'true' && {rail}.inert")

            def go(path):
                old = driver.find_element(By.TAG_NAME, "html")
                driver.get(base + path)
                wait.until(lambda _: detached(old))
                ready()

            def click(selector, navigation=True):
                element = driver.find_element(By.CSS_SELECTOR, selector)
                driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"});', element)
                element.click()
                if navigation:
                    wait.until(lambda _: detached(element))
                    ready()

            preview_login(driver, base)
            go("/matters/new")
            driver.find_element(By.ID, "matter-name").send_keys("Generated email attachment review")
            click(".matter-form button[type=submit]")
            slug = urlparse(driver.current_url).path.split("/")[2]
            prefix = f"/matters/{slug}"
            matter = bench.matter(slug, ACTOR)
            click("[data-assistant-collapse]", False)
            collection_name = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic email-coverage collection")
            driver.find_element(By.CSS_SELECTOR, "[data-file-input]").send_keys(str(original))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]").text == "Upload 1 ready file")
            click("[data-upload-preflight-confirm]", False)
            wait.until(lambda _: len(bench.source_store(matter).documents) == 1
                and not any(bench.workspace.active_matter_work_counts(matter.matter_id).values()))
            click("[data-intake-receipt-open]")
            click('a[href*="/sources/"]')
            text = driver.find_element(By.TAG_NAME, "body").text
            assert "forwarded.eml" in text and "notes.txt" in text
            assert "Attachment contents were not processed or searched" in text
            # The reader keeps source coverage in its collapsed details panel.
            click("details.review-source-details > summary", False)
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "details.review-source-details").get_attribute("open") is not None)
            assert EMAIL_COVERAGE_NOTICE in driver.find_element(By.CSS_SELECTOR, "details.review-source-details").text
            store = bench.source_store(matter)
            document = next(iter(store.documents.values()))
            token = store.action_token(document)
            identity = document.document_id, document.version_id
            checked("Actual file selection, transfer and receipt open the attachment name/type inventory with explicit coverage")

            for width in (1440, 430):
                driver.set_window_size(width, 1000)
                if width < 901:
                    rail_hidden()
                notice = next((p for p in driver.find_elements(By.CSS_SELECTOR, "details.review-source-details p")
                    if p.text == EMAIL_COVERAGE_NOTICE), None)
                assert notice is not None
                driver.execute_script('arguments[0].scrollIntoView({block:"start",behavior:"instant"});', notice)
                assert notice.is_displayed()
                assert driver.execute_script("return document.documentElement.scrollWidth <= innerWidth + 2")
                driver.save_screenshot(str(output / f"synthetic-email-coverage-{width}.png"))
            driver.set_window_size(1440, 1000)
            click(".reader-pagination a")
            assert "ParentBodyCanary" in driver.find_element(By.CSS_SELECTOR, ".extracted-reader pre").text
            assert "AttachmentOnlyCanary" not in driver.find_element(By.CSS_SELECTOR, ".extracted-reader pre").text
            checked("Desktop and narrow review retain readable coverage and the exact parent-body passage")

            for canary in ("AttachmentOnlyCanary", "TextAttachmentCanary"):
                go(prefix + "?" + urlencode({"mode": "search", "q": canary}))
                assert not driver.find_elements(By.CSS_SELECTOR, ".search-result")
                assert "No matching record found" in driver.find_element(By.CSS_SELECTOR, ".empty-result").text
                hint = driver.find_element(By.CSS_SELECTOR, "[data-search-readiness-hint]")
                assert hint.is_displayed() and EMAIL_COVERAGE_NOTICE in hint.text
            go(prefix + "?" + urlencode({"mode": "search", "q": "ParentBodyCanary"}))
            click(".open-support-link")
            assert "ParentBodyCanary" in driver.find_element(By.ID, "support-pane").text
            click(".support-save-button")
            assert len(bench.workspace.all_notebook_items(matter.matter_id, ACTOR)) == 1
            checked("Attachment-only searches show no matches and coverage; parent search opens exact support and saves a case note")

            go(prefix)
            field = driver.find_element(By.ID, "matter-question")
            field.send_keys("What does ParentBodyCanary record?")
            conversation = bench.workspace.get_conversation(matter.matter_id)
            click(".ask-button", False)
            wait.until(lambda _: len(bench.workspace.messages(matter.matter_id, conversation.conversation_id)) == 2)
            # Each answer keeps its caveats in one collapsed "About this answer" panel.
            open_about = "document.querySelectorAll('[data-answer-about]').forEach(e => { e.open = true; })"
            wait.until(lambda d: d.execute_script(open_about + "; return true")
                and "Source coverage when this answer was created" in page_text())
            answer = bench.workspace.messages(matter.matter_id, conversation.conversation_id)[-1]
            assert answer.payload["source_coverage"]["notice"] == EMAIL_COVERAGE_NOTICE
            go(prefix + "?" + urlencode({"conversation": conversation.conversation_id}))
            driver.execute_script(open_about)
            assert EMAIL_COVERAGE_NOTICE in driver.find_element(By.TAG_NAME, "body").text
            click(".answer-completion-bar details summary", False)
            click('a[href*="/messages/"][href*="/export"][href*="markdown"]', False)
            markdown = wait.until(lambda _: next(downloads.glob("*.md"), None))
            assert EMAIL_COVERAGE_NOTICE in markdown.read_text()
            checked("Normal question submission, saved answer reload and answer download retain the attachment coverage snapshot")

            go(prefix + "?" + urlencode({"conversation": conversation.conversation_id}))
            driver.find_element(By.ID, "matter-question").send_keys("Investigate what ParentBodyCanary recorded.")
            click(".investigate-button", False)
            wait.until(lambda _: bench.workspace.research_jobs(matter.matter_id, ACTOR))
            research_id = bench.workspace.research_jobs(matter.matter_id, ACTOR)[0].job_id
            wait.until(lambda _: bench.workspace.research_job(matter.matter_id, ACTOR, research_id).state == "succeeded")
            go(prefix + "/research?" + urlencode({"job": research_id}))
            # The generated-text caution comes first; select the coverage caution itself.
            caution = next((element for element in driver.find_elements(By.CSS_SELECTOR, ".research-result .workflow-caution")
                if EMAIL_COVERAGE_NOTICE in element.text), None)
            assert caution is not None
            assert "did not check every source" in caution.text
            for width in (1440, 430):
                driver.set_window_size(width, 1000)
                if width < 901:
                    rail_hidden()
                driver.execute_script('arguments[0].scrollIntoView({block:"center",behavior:"instant"});', caution)
                assert caution.is_displayed()
                assert driver.execute_script("return document.documentElement.scrollWidth <= innerWidth + 2")
                driver.save_screenshot(str(output / f"synthetic-email-investigation-{width}.png"))
            driver.set_window_size(1440, 1000)
            click(".workflow-completion-bar details:has(a[data-download]) summary", False)
            before_research = set(downloads.glob("*.md"))
            click(f'.workflow-completion-bar a[href="{prefix}/research/{research_id}/export?format=markdown"]', False)
            research_file = wait.until(lambda _: next(iter(set(downloads.glob("*.md")) - before_research), None))
            assert EMAIL_COVERAGE_NOTICE in research_file.read_text()
            assert "did not check every source" in research_file.read_text()
            checked("Normal investigation retains attachment coverage beside its focused-search caution on desktop/narrow results and the downloaded evidence ledger")

            go(prefix + "?" + urlencode({"conversation": conversation.conversation_id}))
            main_window = driver.current_window_handle
            driver.execute_script("window.emailMixedCoverage = false; window.addEventListener('recordbench:readiness', event => { if (event.detail.attention_count === 1) window.emailMixedCoverage = true; });")
            damaged = root / "damaged.pdf"
            damaged.write_bytes(b"%PDF-1.4\nGenerated damaged document.\n%%EOF\n")
            driver.switch_to.new_window("tab")
            go(prefix + "/setup")
            collection_name = driver.find_element(By.CSS_SELECTOR, "[data-upload-collection-name]")
            collection_name.clear()
            collection_name.send_keys("Synthetic email-coverage collection")
            driver.find_element(By.CSS_SELECTOR, "[data-file-input]").send_keys(str(damaged))
            wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "[data-upload-preflight-confirm]").text == "Upload 1 ready file")
            click("[data-upload-preflight-confirm]", False)
            wait.until(lambda _: bench.workspace.matter_readiness(matter.matter_id).attention_count == 1
                and not any(bench.workspace.active_matter_work_counts(matter.matter_id).values()))
            driver.close()
            driver.switch_to.window(main_window)
            wait.until(lambda d: d.execute_script("return window.emailMixedCoverage === true"))
            href = driver.find_element(By.CSS_SELECTOR, "[data-conversation-coverage-action]").get_attribute("href")
            assert "status=attention" not in href and "view=list" in href
            click("[data-conversation-coverage-action]")
            library_text = driver.find_element(By.TAG_NAME, "body").text
            assert "generated.eml" in library_text and "damaged.pdf" in library_text
            click(f'a[href^="{prefix}/sources/{token}?browse="]')
            assert "forwarded.eml" in driver.find_element(By.TAG_NAME, "body").text
            assistant_href = driver.find_element(By.CSS_SELECTOR, "[data-assistant-coverage-action]").get_attribute("href")
            assert "status=attention" not in assistant_href and "view=list" in assistant_href
            checked("After a second tab adds a failed source, live coverage links still open both the ready email and affected source, with exact email review reachable")

            foreign_owner = "generated-email-foreign-owner"
            bench.workspace.upsert_principal("test", foreign_owner, "Generated foreign owner", foreign_owner,
                preferred_principal_id=foreign_owner)
            foreign = bench.create_matter("Generated foreign email canary", "", foreign_owner)
            go(f"/matters/{foreign.slug}/sources/{token}")
            assert "ParentBodyCanary" not in driver.find_element(By.TAG_NAME, "body").text
            assert (store.get(document.document_id).document_id, store.get(document.document_id).version_id) == identity
            checked("Foreign-matter source opening remains denied without changing the admitted source version")

            go(prefix + "/settings")
            before_bundle = set(downloads.iterdir())
            click(f'a[href="{prefix}/export"]', False)
            bundle = wait.until(lambda _: completed_zip(downloads, before_bundle))
            with zipfile.ZipFile(bundle) as archive:
                conversations = [name for name in archive.namelist() if name.startswith("conversations/") and name.endswith(".md")]
                assert conversations and any(EMAIL_COVERAGE_NOTICE in archive.read(name).decode() for name in conversations)
                investigations = [name for name in archive.namelist() if name.startswith("investigations/")]
                assert investigations and all(EMAIL_COVERAGE_NOTICE in archive.read(name).decode() for name in investigations)
                assert not any(name.endswith(".eml") for name in archive.namelist())
            exit_flow = BrowserCaseExit(driver, base, app, matter, downloads, preview_subject="taylor-morgan")
            exit_flow.prepare()
            exit_flow.confirm_name(matter.display_name)
            wait.until(lambda _: bench.workspace.matter_lifecycle(matter.matter_id).state == "deleted")
            assert original.read_bytes() == original_bytes
            checked("Final bundle includes saved coverage; owner closure removes temporary matter state and preserves the external email")
            report["passed"] = True
        except Exception:
            if driver is not None:
                driver.save_screenshot(str(output / "failure.png"))
                (output / "failure.html").write_text(driver.page_source)
            raise
        finally:
            (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
            if driver is not None:
                driver.quit()
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == "__main__":
    main()
